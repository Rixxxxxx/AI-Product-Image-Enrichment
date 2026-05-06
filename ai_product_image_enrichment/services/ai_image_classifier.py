"""
Claude-based image classifier.

Two-pass design:
  Pass A — page match: cheap yes/no whether the page actually represents this product.
  Pass B — image classification: only run on the page that matched, classify each <img>
           tag by role (main/angle/detail/in_use/lifestyle/accessory/uncertain).

We pre-filter <img> tags to those with width/height >=200 OR meaningful alt/surrounding
text before sending to Claude — large pages can have 50+ images, most of which are
icons/banners/related-product thumbs that waste tokens.
"""

import json
import logging
import re
import time
from urllib.parse import urljoin

_logger = logging.getLogger(__name__)


SYSTEM_PROMPT_GALLERY_ONLY = """You are an expert at identifying GALLERY product images on manufacturer e-commerce pages.

IMPORTANT SCOPE: You are NOT looking for a main / hero / PDP shot. The operator
already has a main image for every product. Your job is to find SUPPLEMENTARY
gallery images that go alongside the existing main on the product detail page.
Never assign role=main to any image.

You will be given:
  - Product context: name, SKU, manufacturer, optional category
  - A list of <img> tags from a candidate manufacturer product page, each with:
    src URL, alt text, declared width/height, surrounding-text snippet

Your tasks:
1. Decide if this page actually represents the given product (page_is_correct_product).
   Use SKU and manufacturer name as primary evidence. Be conservative — if uncertain, say false.
2. For each image, classify its ROLE (one of):
   - angle: Alternate viewpoint of the same product (back, side, top, three-quarter)
   - detail: Close-up of a control, label, attachment point, or feature
   - in_use: Product being used in a real environment (operator, floor, room)
   - lifestyle: Marketing imagery emphasizing context over product
   - accessory: A bundled accessory or attachment, not the main product
   - uncertain: Cannot tell — DO NOT include in `images`, list in `rejected` instead
3. REJECT logos, navigation icons, banners, hero marketing tiles, related-product
   thumbnails, review/rating widgets, social-media icons, payment-method icons.
4. If you see what is clearly the product's primary studio shot (typically the first
   large hero image), classify it as `angle` — NOT main. We don't replace the
   operator's existing main image.

Be conservative: false positives waste budget downloading bad images.
"""


SYSTEM_PROMPT_INCLUDE_MAIN = """You are an expert at identifying product images on manufacturer e-commerce pages.

The operator wants you to find BOTH a main image AND supplementary gallery images.

You will be given:
  - Product context: name, SKU, manufacturer, optional category
  - A list of <img> tags from a candidate manufacturer product page, each with:
    src URL, alt text, declared width/height, surrounding-text snippet

Your tasks:
1. Decide if this page actually represents the given product (page_is_correct_product).
   Use SKU and manufacturer name as primary evidence. Be conservative — if uncertain, say false.
2. For each image, classify its ROLE (one of):
   - main: The primary studio / PDP shot of the product. ANY background is fine
     (white, grey, transparent, even mild context) — the pipeline will run
     background removal and normalize it. Pick the one that best shows the
     product in full, isolated, frontal or three-quarter view.
   - angle: Alternate viewpoint of the same product
   - detail: Close-up of a control, label, attachment point, or feature
   - in_use: Product being used in a real environment (operator, floor, room)
   - lifestyle: Marketing imagery emphasizing context over product
   - accessory: A bundled accessory or attachment, not the main product
   - uncertain: Cannot tell — DO NOT include in `images`, list in `rejected` instead
3. REJECT logos, navigation icons, banners, hero marketing tiles, related-product
   thumbnails, review/rating widgets, social-media icons, payment-method icons.
4. Choose AT MOST ONE main image. If multiple equally-qualified candidates exist,
   pick the one with the cleanest background and best framing.

Be conservative: false positives waste budget downloading bad images.

Return STRICT JSON only — no prose, no markdown fences:
{
  "page_is_correct_product": bool,
  "product_match_confidence": float (0..1),
  "match_reasoning": str,
  "images": [
    {"url": str, "role": "main|angle|detail|in_use|lifestyle|accessory",
     "confidence": float (0..1), "reasoning": str}
  ],
  "rejected": [{"url": str, "reason": str}]
}
"""


class AIImageClassifierError(Exception):
    pass


class AIServiceUnavailable(Exception):
    """Anthropic API is unavailable / rejecting calls in a way that retrying won't help.

    Raised on credit-balance-too-low, authentication failure, or sustained
    rate-limit. The cron catches this, pauses ALL running jobs, and notifies
    the operator. No more Claude calls happen until the operator re-queues.
    """
    pass


def _sanitize_anthropic_error(e: Exception) -> str:
    """Strip API keys / Authorization headers / request bodies from SDK errors.

    The Anthropic SDK includes the full HTTPX request (with Authorization header
    containing the API key) in its repr. We log only the type name + status code +
    a short generic message — never the raw str(e).
    """
    msg = type(e).__name__
    status = getattr(e, 'status_code', None)
    if status:
        msg += f' (status {status})'
    body = getattr(e, 'message', None) or getattr(e, 'body', None)
    if isinstance(body, dict):
        body = body.get('error', {}).get('message') if isinstance(body.get('error'), dict) else None
    if isinstance(body, str):
        msg += f': {body[:200]}'
    return msg


class AIImageClassifier:

    def __init__(self, api_key: str, model: str, env=None, job=None, include_main: bool = False):
        self.api_key = api_key
        self.model = model
        self.env = env
        self.job = job
        self.include_main = include_main

    # ---------- public ----------

    def classify(self, product, page_url, soup):
        if not self.api_key:
            raise AIImageClassifierError('Anthropic API key not configured')

        images_payload = self._extract_images_payload(soup, page_url)
        if not images_payload:
            return {
                'page_is_correct_product': False,
                'product_match_confidence': 0.0,
                'match_reasoning': 'no candidate images on page',
                'images': [],
                'rejected': [],
            }

        product_block = self._product_context_block(product)
        user_msg = (
            f'PRODUCT CONTEXT:\n{product_block}\n\n'
            f'PAGE URL: {page_url}\n\n'
            f'IMAGES ({len(images_payload)} candidates):\n'
            + '\n'.join(self._format_img(i) for i in images_payload)
        )

        return self._call_claude(user_msg, product=product)

    # ---------- prep ----------

    @staticmethod
    def _product_context_block(product):
        from .enrichment_pipeline import _extract_likely_models
        skus = _extract_likely_models(product)
        primary_sku = skus[0] if skus else ''
        alt_skus = ', '.join(skus[1:]) if len(skus) > 1 else ''
        bits = [
            f'name: {product.name or ""}',
            f'sku: {primary_sku}',
            f'manufacturer: {product._effective_manufacturer() or ""}',
        ]
        if alt_skus:
            bits.append(f'alternative_models_to_consider: {alt_skus}')
        cat = product.categ_id.name if product.categ_id else ''
        if cat:
            bits.append(f'category: {cat}')
        return '\n'.join(bits)

    @staticmethod
    def _format_img(i):
        return (f'- url={i["url"]} | alt="{i["alt"][:100]}" | '
                f'w={i.get("width","?")} h={i.get("height","?")} | '
                f'context="{i["context"][:160]}"')

    def _extract_images_payload(self, soup, page_url):
        """Pre-filter <img> tags to plausible product photos. Cuts Claude tokens by ~70%."""
        out = []
        seen = set()
        for img in soup.find_all('img'):
            src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
            if not src:
                continue
            abs_url = urljoin(page_url, src)
            if abs_url in seen:
                continue
            seen.add(abs_url)

            alt = (img.get('alt') or '').strip()
            try:
                w = int(img.get('width') or 0)
            except (ValueError, TypeError):
                w = 0
            try:
                h = int(img.get('height') or 0)
            except (ValueError, TypeError):
                h = 0

            # Pull a small text neighborhood for ambiguity resolution
            parent = img.parent
            ctx = ''
            for _ in range(3):
                if parent is None:
                    break
                txt = parent.get_text(' ', strip=True) if parent else ''
                if txt and len(txt) > len(ctx):
                    ctx = txt[:300]
                if len(ctx) >= 100:
                    break
                parent = parent.parent

            # Heuristic prefilter: skip obvious junk to save tokens
            lower = abs_url.lower()
            if any(j in lower for j in (
                'sprite', 'icon', 'logo', 'flag-', 'placeholder', 'spinner',
                'social', 'twitter', 'facebook', 'instagram', '/avatar',
            )):
                continue
            if w and h and w < 200 and h < 200 and not alt:
                continue
            if any(lower.endswith(ext) for ext in ('.svg',)):
                continue

            out.append({
                'url': abs_url,
                'alt': alt,
                'width': w,
                'height': h,
                'context': ctx,
            })

        # Cap to keep prompt size reasonable
        return out[:60]

    # ---------- API ----------

    # Tool schema forces Claude to return structured JSON instead of free-form
    # markdown prose. Was the source of every "Claude returned non-JSON" error.
    _CLASSIFY_TOOL = {
        'name': 'submit_classification',
        'description': 'Submit your classification of product images on this page.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'page_is_correct_product': {
                    'type': 'boolean',
                    'description': 'True if this page represents the given product.',
                },
                'product_match_confidence': {
                    'type': 'number',
                    'description': 'Confidence (0..1) that this page is the right product.',
                },
                'match_reasoning': {
                    'type': 'string',
                    'description': 'One-sentence reason for the page-match decision.',
                },
                'images': {
                    'type': 'array',
                    'description': 'Images you classified as product photos worth keeping.',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'url': {'type': 'string'},
                            'role': {
                                'type': 'string',
                                'enum': ['main', 'angle', 'detail', 'in_use', 'lifestyle', 'accessory'],
                            },
                            'confidence': {'type': 'number'},
                            'reasoning': {'type': 'string'},
                        },
                        'required': ['url', 'role', 'confidence'],
                    },
                },
                'rejected': {
                    'type': 'array',
                    'description': 'Images you explicitly rejected (logos, banners, unrelated).',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'url': {'type': 'string'},
                            'reason': {'type': 'string'},
                        },
                        'required': ['url', 'reason'],
                    },
                },
            },
            'required': ['page_is_correct_product', 'product_match_confidence', 'images', 'rejected'],
        },
    }

    def _call_claude(self, user_msg: str, product=None):
        # HARD KILL-SWITCH: refuse every Claude call once trailing-hour spend
        # exceeds the configured cap. Independent of jobs, cron, locks. Reads
        # from aipie_ai_usage_log (which writes via separate cursor and
        # therefore reflects real spend even if the surrounding txn rolled back).
        if self.env is not None:
            try:
                cap = float(self.env['ir.config_parameter'].sudo().get_param(
                    'ai_product_image_enrichment.aipie_hourly_ai_cap_usd', '5.0'))
            except (ValueError, TypeError):
                cap = 5.0
            if cap > 0:
                spent = self.env['aipie.ai.usage.log']._last_hour_cost()
                if spent >= cap:
                    raise AIServiceUnavailable(
                        f'Hourly cap reached: ${spent:.2f} spent in the last 60 min '
                        f'(cap ${cap:.2f}). Refusing further Claude calls until usage drops.'
                    )

        try:
            import anthropic
        except ImportError as e:
            raise AIImageClassifierError(f'anthropic SDK not installed: {e}')

        client = anthropic.Anthropic(api_key=self.api_key)
        t0 = time.time()
        in_tok = out_tok = 0
        err = None
        try:
            # Forced tool use guarantees Claude returns a tool_use block whose
            # `input` field is already a typed dict. No JSON-parsing fragility.
            resp = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=(SYSTEM_PROMPT_INCLUDE_MAIN if self.include_main else SYSTEM_PROMPT_GALLERY_ONLY),
                tools=[self._CLASSIFY_TOOL],
                tool_choice={'type': 'tool', 'name': 'submit_classification'},
                messages=[{'role': 'user', 'content': user_msg}],
            )
            in_tok = getattr(resp.usage, 'input_tokens', 0) or 0
            out_tok = getattr(resp.usage, 'output_tokens', 0) or 0
            for block in resp.content:
                if getattr(block, 'type', None) == 'tool_use' and getattr(block, 'name', '') == 'submit_classification':
                    return dict(block.input or {})
            # Fallback: maybe forced tool-use was ignored — try parsing text
            text = ''.join(getattr(b, 'text', '') for b in resp.content)
            return self._parse_json_strict(text)
        except AIImageClassifierError:
            # Already a typed error (e.g. from _parse_json_strict). Re-raise
            # as-is so the original message survives instead of getting
            # double-wrapped into 'Claude call failed: AIImageClassifierError'.
            raise
        except Exception as e:
            err = _sanitize_anthropic_error(e)
            err_l = err.lower()
            status = getattr(e, 'status_code', None)
            if (status in (401, 402, 403, 429)
                    or 'credit balance' in err_l
                    or 'insufficient' in err_l
                    or 'authentication' in err_l
                    or 'invalid_api_key' in err_l
                    or 'rate_limit' in err_l):
                raise AIServiceUnavailable(err) from None
            raise AIImageClassifierError(f'Claude call failed: {err}') from None
        finally:
            duration = int((time.time() - t0) * 1000)
            if self.env is not None:
                self.env['aipie.ai.usage.log'].sudo().log_usage(
                    model=self.model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    product=product,
                    job=self.job,
                    operation='classify_images',
                    duration_ms=duration,
                    error=err,
                )

    # ---------- vision-based BG detection (used for ambiguous heuristic cases) ----------

    def vision_is_studio_shot(self, image_bytes: bytes, max_dim: int = 256) -> bool:
        """Ask Claude to classify an image as studio vs context. Used when the
        numpy heuristic is borderline (border whiteness 75-90%). One token cost,
        eliminates false positives.
        """
        if not self.api_key:
            return False
        # Downsample to keep token cost trivial
        try:
            from PIL import Image
            import io as _io
            img = Image.open(_io.BytesIO(image_bytes)).convert('RGB')
            img.thumbnail((max_dim, max_dim))
            buf = _io.BytesIO()
            img.save(buf, format='JPEG', quality=80)
            small = buf.getvalue()
        except Exception:
            return False

        try:
            import anthropic
            import base64 as _b64
        except ImportError:
            return False

        client = anthropic.Anthropic(api_key=self.api_key)
        t0 = time.time()
        in_tok = out_tok = 0
        err = None
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=20,
                system='You classify product images. Answer with a single word: STUDIO or CONTEXT.',
                messages=[{
                    'role': 'user',
                    'content': [
                        {'type': 'image', 'source': {
                            'type': 'base64', 'media_type': 'image/jpeg',
                            'data': _b64.b64encode(small).decode('ascii'),
                        }},
                        {'type': 'text', 'text': (
                            'STUDIO = clean white/grey/seamless background, product isolated. '
                            'CONTEXT = product in a real environment (floor, room, operator, outdoor). '
                            'Answer with one word only.'
                        )},
                    ],
                }],
            )
            in_tok = getattr(resp.usage, 'input_tokens', 0) or 0
            out_tok = getattr(resp.usage, 'output_tokens', 0) or 0
            text = ''.join(getattr(b, 'text', '') for b in resp.content).strip().upper()
            return text.startswith('STUDIO')
        except Exception as e:
            err = _sanitize_anthropic_error(e)
            return False
        finally:
            duration = int((time.time() - t0) * 1000)
            if self.env is not None:
                self.env['aipie.ai.usage.log'].sudo().log_usage(
                    model=self.model, input_tokens=in_tok, output_tokens=out_tok,
                    job=self.job, operation='vision_studio_check',
                    duration_ms=duration, error=err,
                )

    @staticmethod
    def _parse_json_strict(text: str) -> dict:
        """Claude usually returns clean JSON when asked. Strip code fences just in case."""
        text = text.strip()
        # Remove ```json ... ``` if present
        m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.S)
        if m:
            text = m.group(1)
        # Find first { ... last } if there's surrounding prose
        if not text.startswith('{'):
            m2 = re.search(r'\{.*\}', text, re.S)
            if m2:
                text = m2.group(0)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise AIImageClassifierError(f'Claude returned non-JSON: {e}\n---\n{text[:500]}')
