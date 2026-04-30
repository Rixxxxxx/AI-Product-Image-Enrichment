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


SYSTEM_PROMPT = """You are an expert at identifying product images on manufacturer e-commerce pages.

You will be given:
  - Product context: name, SKU, manufacturer, optional category
  - A list of <img> tags from a candidate manufacturer product page, each with:
    src URL, alt text, declared width/height, surrounding-text snippet

Your tasks:
1. Decide if this page actually represents the given product (page_is_correct_product).
   Use SKU and manufacturer name as primary evidence. Be conservative — if uncertain, say false.
2. For each image, classify its ROLE:
   - main: Hero/PDP shot. CRITICAL: only assign role=main if the image clearly has
     a CLEAN WHITE OR STUDIO BACKGROUND. URL/alt patterns that strongly suggest a
     studio shot: _white, _studio, _main, _primary, _pdp, _hero, _front, _on-white,
     _silo, _packshot. If the only candidate "main" image looks like a lifestyle or
     in-use shot (operator, warehouse floor, busy backdrop), DO NOT mark it as main —
     leave main empty. Better to skip than to apply a non-studio main.
   - angle: Alternate viewpoint of the same product, white/studio background
   - detail: Close-up of a control, label, or feature (background can be anything)
   - in_use: Product being used in a real environment (operator, floor, etc.)
   - lifestyle: Marketing/lifestyle imagery emphasizing context over product
   - accessory: A bundled accessory or attachment, not the main product
   - uncertain: Cannot tell — DO NOT include in `images`, list in `rejected` instead
3. REJECT logos, navigation icons, banners, hero marketing tiles, related-product
   thumbnails, review/rating widgets, social-media icons, payment-method icons.

Be conservative: false positives waste budget downloading bad images. A missing
main image is better than a bad one — the operator will fall back to manual upload.

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

    def __init__(self, api_key: str, model: str, env=None, job=None):
        self.api_key = api_key
        self.model = model
        self.env = env
        self.job = job

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
        bits = [
            f'name: {product.name or ""}',
            f'sku: {product.laudie_manufacturer_sku or product.default_code or ""}',
            f'manufacturer: {product._effective_manufacturer() or ""}',
        ]
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

    def _call_claude(self, user_msg: str, product=None):
        try:
            import anthropic
        except ImportError as e:
            raise AIImageClassifierError(f'anthropic SDK not installed: {e}')

        client = anthropic.Anthropic(api_key=self.api_key)
        t0 = time.time()
        in_tok = out_tok = 0
        err = None
        try:
            resp = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': user_msg}],
            )
            in_tok = getattr(resp.usage, 'input_tokens', 0) or 0
            out_tok = getattr(resp.usage, 'output_tokens', 0) or 0
            text = ''.join(getattr(b, 'text', '') for b in resp.content)
            return self._parse_json_strict(text)
        except Exception as e:
            err = _sanitize_anthropic_error(e)
            raise AIImageClassifierError(f'Claude call failed: {err}') from None
        finally:
            duration = int((time.time() - t0) * 1000)
            if self.env is not None:
                self.env['laudie.ai.usage.log'].sudo().log_usage(
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
                self.env['laudie.ai.usage.log'].sudo().log_usage(
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
