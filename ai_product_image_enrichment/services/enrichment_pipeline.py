"""
Pipeline orchestrator. Imports services lazily so test/installation never fails on
hosts that lack rembg or anthropic.

Two main entry points:
  * enrich_product(product, job, config, env) — discover + (optionally) apply
  * normalize_existing_main_image(product, config, env) — pure normalization, no AI
  * apply_candidate_to_product(candidate, config, env) — promote a reviewed candidate

Discovery flow:
  1. Sitemap lookup (free, polite)
  2. Search API fallback (Brave by default)
  3. Per page:
       a. If domain has an active recipe → try recipe extraction first (free)
       b. If recipe yields nothing usable → fall back to AI classification
       c. If DOM parsing yields too few images → optional screenshot+vision fallback
  4. Background analysis: numpy heuristic, vision-disambiguation on borderline cases
  5. Background removal: Photoroom by default, rembg fallback
  6. Normalization with hash-based skip-if-unchanged
"""

import base64
import hashlib
import io
import json
import logging
import re
import time
from urllib.parse import urlparse

# Each product gets a wall-clock budget. CloudPepper / production observed kills
# the cron worker around 55-60s real time (despite the message saying "after 120s")
# — likely accumulating wall-clock across cron loops within the worker process.
# We bail out at 50s so the graceful timeout in enrich_product fires BEFORE
# SIGXCPU. Without this, the product's final-state write is rolled back, the
# product stays at 'searching', and orphan recovery re-queues it forever (the
# replay loop we hit on the ProGuard 20).
PER_PRODUCT_BUDGET_SECONDS = 45

_logger = logging.getLogger(__name__)

ROLE_TO_SEQUENCE = {
    'main': 0, 'angle': 10, 'detail': 20,
    'in_use': 30, 'lifestyle': 40, 'accessory': 50,
}

SIGNATURE_KEYS = ('target_canvas_size', 'padding_percent', 'bg_color',
                  'output_format', 'jpeg_quality', 'white_threshold')


# ---------- helpers ----------

def _settings_signature(settings: dict) -> str:
    s = json.dumps({k: settings[k] for k in SIGNATURE_KEYS if k in settings}, sort_keys=True)
    return hashlib.sha256(s.encode()).hexdigest()


def _output_signature(output_bytes: bytes, settings: dict) -> str:
    h = hashlib.sha256(output_bytes)
    h.update(_settings_signature(settings).encode())
    return h.hexdigest()


def _backup_main(product, config):
    if config.get('keep_backup', True):
        if product.image_1920 and not product.aipie_original_main_image:
            product.aipie_original_main_image = product.image_1920


def _get_normalizer():
    from .image_normalizer import ImageNormalizer
    return ImageNormalizer()


def _get_bg_analyzer(config):
    from .background_analyzer import BackgroundAnalyzer
    return BackgroundAnalyzer(
        white_threshold=config['white_threshold'],
        min_white_percent=config['white_bg_min_percent'],
    )


def _get_bg_dispatcher(config):
    from .photoroom import BackgroundRemovalDispatcher
    return BackgroundRemovalDispatcher(
        photoroom_api_key=config.get('photoroom_api_key', ''),
        rembg_model=config['rembg_model'],
    )


def _read_webp_dimensions(data: bytes):
    """Extract (width, height) from WebP bytes by parsing the RIFF/VP8 header.
    Works without libwebp / Pillow WebP support. Returns (None, None) if not WebP
    or the header is malformed.

    WebP can come in three flavours:
      VP8   — lossy
      VP8L  — lossless
      VP8X  — extended (alpha, animation, ICC profile)
    """
    try:
        if len(data) < 30 or data[:4] != b'RIFF' or data[8:12] != b'WEBP':
            return None, None
        chunk_type = data[12:16]
        if chunk_type == b'VP8X':
            w = (data[24] | (data[25] << 8) | (data[26] << 16)) + 1
            h = (data[27] | (data[28] << 8) | (data[29] << 16)) + 1
            return w, h
        if chunk_type == b'VP8 ':
            # Width is bytes 26-27 (14-bit LE), height bytes 28-29
            if len(data) < 30:
                return None, None
            w = (data[26] | (data[27] << 8)) & 0x3FFF
            h = (data[28] | (data[29] << 8)) & 0x3FFF
            return w, h
        if chunk_type == b'VP8L':
            if len(data) < 25:
                return None, None
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            w = (b0 | ((b1 & 0x3F) << 8)) + 1
            h = ((b1 >> 6) | (b2 << 2) | ((b3 & 0x0F) << 10)) + 1
            return w, h
    except Exception:
        pass
    return None, None


def _phash(image_bytes: bytes, size: int = 16) -> bytes:
    """Tiny perceptual hash. Resize to size×size grayscale and return raw pixel bytes.
    Two visually-similar images (different resolutions, light recompression) will
    produce hashes that are close pixel-by-pixel.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        # Palette ('P') images with byte-encoded transparency emit a Pillow
        # deprecation warning when converted directly to 'L'. Route through
        # RGBA first to silence it without changing the hash output.
        if img.mode == 'P' and 'transparency' in img.info:
            img = img.convert('RGBA')
        img = img.convert('L').resize((size, size), Image.LANCZOS)
        return img.tobytes()
    except Exception:
        return b''


def _images_similar(a: bytes, b: bytes, max_avg_diff: int = 10) -> bool:
    """Returns True if the two images look visually similar.

    Threshold is average per-pixel grayscale difference on a 16x16 thumbnail.
    Default max_avg_diff=10 catches identical images and very-near-duplicates
    while tolerating mild compression / resize / format differences. Lower the
    threshold for stricter matching.
    """
    ha = _phash(a)
    hb = _phash(b)
    if not ha or not hb or len(ha) != len(hb):
        return False
    diff = sum(abs(x - y) for x, y in zip(ha, hb))
    return (diff / len(ha)) < max_avg_diff


def _validate_image(image_bytes, config):
    if not image_bytes or len(image_bytes) < 4 * 1024:
        return False, f'too small ({len(image_bytes) if image_bytes else 0} bytes)'
    head = image_bytes[:16]
    if head.startswith((b'<!', b'<html', b'<HTML', b'<?xml', b'{')):
        return False, f'response is not an image (first bytes: {head[:32]!r})'
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
        img = Image.open(io.BytesIO(image_bytes))
        if img.width < config['min_image_width']:
            return False, f'width {img.width} < min {config["min_image_width"]}'
        return True, None
    except Exception as pil_err:
        # PIL failed. If the file is a known format we can size-check manually
        # (e.g. WebP without libwebp), accept it — Photoroom handles WebP natively
        # downstream, and gallery images get stored raw without PIL touching them.
        w, h = _read_webp_dimensions(image_bytes)
        if w is not None:
            if w < config['min_image_width']:
                return False, f'width {w} < min {config["min_image_width"]} (WebP, PIL decode unavailable)'
            return True, None
        return False, (f'PIL cannot decode (first bytes: {head[:32]!r}): {pil_err}. '
                       f'If many WebP failures: install libwebp on server (sudo apt install libwebp-dev) '
                       f'and reinstall Pillow.')


_SHOPIFY_SIZE_RE = re.compile(r'_(\d+x\d+|\d+x|x\d+)(?=\.[a-zA-Z]{2,5}(?:[?#]|$))')
_BIGCOMMERCE_STENCIL_RE = re.compile(r'/stencil/\d+x\d+/')
# WordPress media library auto-resizes: `name-300x219.jpg`. Anchor on dash so we
# don't strip a legitimately-named file like `model-3x4-bracket.jpg`.
_WORDPRESS_SIZE_RE = re.compile(r'-(\d{2,4})x(\d{2,4})(?=\.[a-zA-Z]{2,5}(?:[?#]|$))')
# Cloudinary: any segment that's a comma-separated list of single-letter
# transforms, e.g. /upload/c_fill,w_500,h_300/v123/foo.jpg → strip the segment.
_CLOUDINARY_TRANSFORM_RE = re.compile(
    r'/upload/(?:[a-z]_[^/]+(?:,[a-z]_[^/]+)*)/'
)
# Wix static images: /v1/fit/w_500,h_500/ or /v1/fill/w_500,h_500,...
_WIX_TRANSFORM_RE = re.compile(r'/v1/(fit|fill)/w_\d+,h_\d+(?:,[^/]*)?/')


def _upscale_cdn_url(url: str) -> str:
    """Bump common CDN thumbnail params/path-suffixes so we fetch the full-size image.

    Patterns handled (each gated by a host/path signature so we don't mangle
    unrelated URLs that happen to look similar):

    * Shopify CDN — `_500x500.jpg` filename suffix on `/cdn/shop/` paths.
    * BigCommerce — `/stencil/500x659/` path segment.
    * WordPress — `-300x219.jpg` filename suffix on `/wp-content/uploads/` paths.
    * Cloudinary — `/upload/<transforms>/` segment on `res.cloudinary.com`.
    * Wix — `/v1/fit/w_500,h_500/` segment on `static.wixstatic.com`.
    * Squarespace — `?format=500w` query param.
    * Generic query params (Shopify ?width=, Imgix, Sirv, Photon, Cloudinary
      query mode, etc.) — width-like params bumped to 2000, height-like
      dropped so aspect ratio is preserved.

    Returns the original URL if no recognized resize hint is found.
    """
    from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
    try:
        parts = urlparse(url)
        host = (parts.netloc or '').lower()
        new_path = parts.path

        # Shopify path-suffix sizing.
        if '/cdn/shop/' in new_path:
            new_path = _SHOPIFY_SIZE_RE.sub('', new_path)

        # BigCommerce stencil sizing.
        if '/stencil/' in new_path:
            new_path = _BIGCOMMERCE_STENCIL_RE.sub('/stencil/2048x2048/', new_path)

        # WordPress media library auto-resizes (also covers WooCommerce).
        # Only apply to /wp-content/uploads/ to avoid stripping legitimate
        # `-100x100`-style identifiers from other URLs.
        if '/wp-content/uploads/' in new_path:
            new_path = _WORDPRESS_SIZE_RE.sub('', new_path)

        # Cloudinary transform segments — strip to fetch the un-transformed
        # original (the version below the transform segment is the source).
        if 'cloudinary.com' in host:
            new_path = _CLOUDINARY_TRANSFORM_RE.sub('/upload/', new_path)

        # Wix static.
        if 'wixstatic.com' in host:
            new_path = _WIX_TRANSFORM_RE.sub('/v1/fit/w_2000,h_2000/', new_path)

        # Query-param sizing (any CDN).
        new_query = parts.query
        if parts.query:
            params = parse_qs(parts.query, keep_blank_values=True)
            modified = False
            for key in ('width', 'w', 'size', 'maxwidth', 'max_width', 'sz', 'fit'):
                if key in params:
                    params[key] = ['2000']
                    modified = True
            for key in ('height', 'h', 'maxheight', 'max_height'):
                if key in params:
                    del params[key]
                    modified = True
            # Squarespace: ?format=500w → ?format=2500w
            if 'format' in params:
                new_format = []
                for v in params['format']:
                    m = re.match(r'^(\d+)w$', v or '')
                    if m:
                        new_format.append('2500w')
                        modified = True
                    else:
                        new_format.append(v)
                params['format'] = new_format
            if modified:
                new_query = urlencode(params, doseq=True)

        if new_path == parts.path and new_query == parts.query:
            return url
        return urlunparse(parts._replace(path=new_path, query=new_query))
    except Exception:
        return url


def _ambiguous_white_bg(white_percent: float, low: int, high: int) -> bool:
    """Heuristic borderline zone — vision check earns its keep here."""
    return low <= white_percent < high


def _norm_alnum(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())


def _url_host_contains_brand(url: str, brand: str) -> bool:
    """True if the URL's host string contains the brand keyword (alphanumerics-only,
    case-insensitive). Used to reject candidate images that aren't hosted on the
    manufacturer's own domain or CDN.
    """
    if not (url and brand):
        return False
    host = urlparse(url).netloc
    return bool(_norm_alnum(brand)) and _norm_alnum(brand) in _norm_alnum(host)


_SKU_TOKEN_RE = re.compile(r'\b[A-Z0-9][A-Z0-9\-]{2,15}\b', re.IGNORECASE)
# Common stop-words that look SKU-ish but aren't (units, generic specs, etc.)
_SKU_STOPWORDS = {
    'AC', 'DC', 'USB', 'LED', 'LCD', 'OEM', 'OEM-', 'PRO', 'MAX', 'PLUS',
    'XL', 'XXL', 'KIT', 'SET', 'NEW', 'INC', 'LTD', 'LTÉE',
    '110V', '220V', '240V', '12V', '24V', 'VOLT',
    'INCH', 'INCHES', 'FOOT', 'FEET',
    'AGM', 'GEL', 'NICD', 'NIMH',  # battery types
    'HEPA', 'ULPA',  # filter types
    'IPX', 'IP54', 'IP65', 'IP67',  # ingress-protection ratings
}


def _model_diagnostic(product):
    """Human-readable summary of how the SKU is being resolved for this product.

    Surfaced in failure logs so the operator can spot products where the SKU
    is missing or looks like an internal-only code that won't match what the
    manufacturer uses publicly. Returns a short multi-clause string.
    """
    explicit = (product.aipie_manufacturer_model or '').strip()
    default = (product.default_code or '').strip()
    name = (product.name or '').strip()
    cleaned_name = _strip_internal_refs(name, default)

    default_alnum = re.sub(r'[^A-Z0-9]', '', default.upper())
    name_tokens = []
    if cleaned_name:
        for tok in _SKU_TOKEN_RE.findall(cleaned_name):
            if not _is_model_token(tok):
                continue
            if default_alnum and re.sub(r'[^A-Z0-9]', '', tok.upper()) == default_alnum:
                continue
            name_tokens.append(tok)

    parts = []
    if explicit:
        parts.append(f"explicit override: '{explicit}'")
    if name_tokens:
        parts.append(f"auto-extracted from name: {name_tokens}")
    elif name:
        parts.append("name has no model-number-like tokens")

    has_real_sku = bool(explicit or name_tokens)
    if not has_real_sku:
        # Check the descriptive-name path
        brand = (product._effective_manufacturer() or '').strip().lower()
        descriptive_words = []
        if name and brand:
            words = [w for w in re.split(r'\s+', name) if w.lower() != brand]
            descriptive_words = [w for w in words
                                 if len(w) >= 4 and w.lower() not in _NAME_STOPWORDS]
        if len(descriptive_words) >= 2:
            parts.append(
                f"using descriptive name fallback (no model number, but {len(descriptive_words)} "
                f"distinctive words after brand: {descriptive_words[:5]}). "
                "Search query will use brand + product name."
            )
        elif default:
            parts.append(
                f"falling back to default_code: '{default}' "
                "⚠ usually your INTERNAL reference, not the manufacturer's public model. "
                "If discovery fails, populate aipie_manufacturer_model with the public model name."
            )
        else:
            parts.append(
                "⚠ NO model identifier available at all. Populate aipie_manufacturer_model manually."
            )
    elif default:
        parts.append(f"(default_code '{default}' is NOT used since we have a model identifier)")
    return ' | '.join(parts)


def _log_now(env, vals=None, **kwargs):
    """Write a aipie.enrichment.log row through a SEPARATE database cursor,
    so it commits immediately and appears in the operator's Logs tab in
    real time — not buffered until the cron chunk completes.

    Necessary because the cron uses a transaction-level advisory lock and
    can't commit per-product. Without this, operators only see all the logs
    at once when the chunk finishes (or never if the chunk rolls back).

    Falls back to the in-transaction create if opening a separate cursor
    fails for any reason — never silently drop a log entry.
    """
    final = dict(vals or {})
    final.update(kwargs)
    try:
        with env.registry.cursor() as new_cr:
            new_env = env(cr=new_cr)
            new_env['aipie.enrichment.log'].sudo().create(final)
    except Exception as e:
        _logger.warning('Live log write failed (%s) — falling back to in-txn write', e)
        try:
            env['aipie.enrichment.log'].sudo().create(final)
        except Exception:
            pass


# Generic descriptors that don't disambiguate a product on their own. Used when
# checking whether a name without a model number still has enough "distinctive"
# content to make a search worthwhile.
_NAME_STOPWORDS = {
    'and', 'for', 'with', 'the', 'pro', 'plus', 'max', 'mini', 'kit', 'set', 'pack',
    'large', 'small', 'medium', 'big', 'mini',
    'new', 'used', 'oem', 'aftermarket',
    'corded', 'cordless', 'manual', 'electric', 'battery', 'lithium',
    'replacement', 'spare', 'parts', 'accessory', 'accessories',
}


def _has_real_model_identifier(product):
    """True iff there's enough specificity to expect AI search to find the right product.

    Three signals (any one is enough):
      1. Explicit aipie_manufacturer_model override is set
      2. Product name contains a model-number-like token (letters + digits, ≥4 chars)
      3. Brand is resolved AND name has ≥2 distinctive words beyond the brand
         (a "distinctive" word = ≥4 chars and not in the generic stopword list).
         Catches products like "Dustbane Doodle Scrub Corded" where the model
         is descriptive but not alphanumeric.

    Returns False only for genuinely ambiguous products like "Generic Mop Bucket"
    where AI search would return random results and waste budget.
    """
    if (product.aipie_manufacturer_model or '').strip():
        return True
    name = (product.name or '').strip()
    if not name:
        return False
    default = (product.default_code or '').strip()
    default_alnum = re.sub(r'[^A-Z0-9]', '', default.upper())
    cleaned_name = _strip_internal_refs(name, default)

    # Signal 2: model-number-like token (not a default_code echo)
    for tok in _SKU_TOKEN_RE.findall(cleaned_name):
        if not _is_model_token(tok):
            continue
        if default_alnum and re.sub(r'[^A-Z0-9]', '', tok.upper()) == default_alnum:
            continue
        return True

    # Signal 3: descriptive name + resolvable brand
    brand = (product._effective_manufacturer() or '').strip().lower()
    if not brand:
        return False
    words = [w for w in re.split(r'\s+', cleaned_name) if w.lower() != brand]
    distinctive = [w for w in words
                   if len(w) >= 4 and w.lower() not in _NAME_STOPWORDS]
    return len(distinctive) >= 2


def _is_model_token(tok: str) -> bool:
    """True if a token looks like a manufacturer model identifier."""
    if len(tok) < 3 or tok.upper() in _SKU_STOPWORDS:
        return False
    has_alpha = any(c.isalpha() for c in tok)
    has_digit = any(c.isdigit() for c in tok)
    # Letters + digits = classic alphanumeric model (TBL1620, SC500-53B)
    if has_alpha and has_digit:
        return True
    # Pure number ≥3 digits = catalog-number-style model (664, 5680, 9087311)
    # Excluding year-like values to avoid false positives.
    if has_digit and not has_alpha and len(tok) >= 3:
        try:
            n = int(tok)
            if 1900 <= n <= 2099:
                return False  # year, not model
        except ValueError:
            pass
        return True
    return False


def _strip_internal_refs(name: str, default_code: str = '') -> str:
    """Remove internal SKU markers from a product name before model-extraction.

    Stripped:
      - '#XXXXXX' tags (e.g. '#19680-C-AGM') — almost always internal references
      - The default_code if it appears verbatim (with or without separators)
    """
    if not name:
        return ''
    # Drop everything after a # on a token boundary
    cleaned = re.sub(r'\s*#\S+', '', name)
    # Drop default_code if it appears (case-insensitive, accounting for hyphens)
    if default_code:
        dc = re.escape(default_code.strip())
        cleaned = re.sub(rf'\b{dc}\b', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _extract_likely_models(product):
    """Return ordered list of likely manufacturer model identifiers for this product.

    What's useful for search: the manufacturer's PUBLIC model name (e.g. 'AV4X',
    'TBL1620', 'SC500-53B'). That's what appears on product pages, in URLs, and
    in marketing copy.

    What's NOT useful: long internal catalog / order-system codes (e.g. '8025160',
    '9087311020'). Including them in the search query adds noise and rarely
    helps find product pages.

    Resolution order:
      1. aipie_manufacturer_model (explicit override) — highest priority
      2. Model-number-like tokens extracted from product.name (alphanumeric,
         ≥4 chars, contains both letters AND digits). E.g. "Nacecare ... AV4X"
         → ['AV4X'].
      3. default_code — used ONLY if nothing came from steps 1 or 2. It's
         typically the operator's internal reference, not a manufacturer model.

    All resolved sources are deduped — search/sitemap/AI use the full list.
    Operators don't need to populate aipie_manufacturer_model manually unless
    the model number isn't in the product name.
    """
    skus = []
    seen = set()

    def _add(s):
        v = (s or '').strip()
        if v and v.upper() not in seen:
            seen.add(v.upper())
            skus.append(v)

    if product.aipie_manufacturer_model:
        _add(product.aipie_manufacturer_model)

    default = (product.default_code or '').strip()
    cleaned_name = _strip_internal_refs(product.name or '', default)
    if cleaned_name:
        default_alnum = re.sub(r'[^A-Z0-9]', '', default.upper())
        for tok in _SKU_TOKEN_RE.findall(cleaned_name):
            if not _is_model_token(tok):
                continue
            # Skip if this is just the default_code echoed in the name
            if default_alnum and re.sub(r'[^A-Z0-9]', '', tok.upper()) == default_alnum:
                continue
            _add(tok)

    # default_code is a LAST-RESORT fallback. It's usually the internal reference
    # (catalog / order number), not the manufacturer's public model. Only include
    # if we have nothing else.
    if not skus and default:
        _add(default)

    return skus


# ---------- normalize-only ----------

def normalize_existing_main_image(product, config, env):
    """Pure Pillow/numpy + maybe Photoroom/rembg. No web access required.

    Skips work entirely if the stored signature still matches current image+settings.
    """
    if not product.image_1920:
        return None

    raw = base64.b64decode(product.image_1920)

    # Hash-based skip
    sig = _output_signature(raw, config)
    if product.aipie_normalization_signature and product.aipie_normalization_signature == sig:
        return {'skipped_unchanged': True}

    analyzer = _get_bg_analyzer(config)
    try:
        has_white, white_pct, info = analyzer.analyze(raw)
        source_state = info.get('source_state', 'complex')
    except Exception as analyze_err:
        # PIL might not support the source format (e.g. WebP without libwebp).
        # Treat as 'complex' so the BG dispatcher (Photoroom) handles it downstream.
        _logger.info('analyzer.analyze() failed (treating as complex): %s', analyze_err)
        has_white, white_pct = False, 0.0
        source_state = 'complex'

    # Vision disambiguation for borderline numpy results (only meaningful for opaque sources)
    if source_state == 'complex' and _ambiguous_white_bg(white_pct, 70, 90) and config.get('anthropic_api_key'):
        from .ai_image_classifier import AIImageClassifier
        cls = AIImageClassifier(config['anthropic_api_key'], config['anthropic_model'], env=env)
        if cls.vision_is_studio_shot(raw):
            source_state = 'white'
            has_white = True

    _backup_main(product, config)

    normalizer = _get_normalizer()

    # If source is already transparent: skip BG removal entirely; just normalize.
    # Otherwise: run BG removal to produce a transparent PNG, then normalize.
    if source_state == 'transparent':
        transparent = raw
    else:
        try:
            transparent = _get_bg_dispatcher(config).remove(raw)
        except Exception as e:
            _logger.warning('BG removal failed for product %s: %s — falling back to white-to-alpha approx', product.id, e)
            transparent = raw  # normalizer will synthesize alpha from white pixels

    normalized = normalizer.normalize(
        transparent,
        target_size=config['target_canvas_size'],
        padding_percent=config['padding_percent'],
        bg_color=config['bg_color'],
        white_threshold=config['white_threshold'],
        transparent_canvas=True,
    )
    product.aipie_main_image_already_white_bg = has_white

    product.image_1920 = base64.b64encode(normalized)
    product.aipie_main_image_normalized = True
    product.aipie_normalization_signature = _output_signature(normalized, config)
    # Deliberately do NOT touch aipie_enrichment_state — normalization is not
    # enrichment (no AI image discovery happened, no candidates were found).
    # aipie_main_image_normalized is the canonical truth source for "this main
    # has been normalized to the uniform canvas". Keep enrichment_state pure
    # so it only ever reflects AI enrichment outcomes.

    return {
        'was_white_bg': has_white,
        'white_percent': white_pct,
        'source_state': source_state,
    }


# ---------- discovery + apply ----------

def _discover_pages(product, config):
    """Sitemap first, search second. Returns list of candidate page URLs."""
    from .sitemap_provider import SitemapProvider
    from .search_provider import SearchProvider

    pages = []

    skus = _extract_likely_models(product)
    manufacturer = (product._effective_manufacturer() or '').strip()

    # Try sitemap if we have manufacturer hint and at least one SKU candidate
    if manufacturer and skus:
        sm = SitemapProvider(user_agent=config['user_agent'])
        guesses = [manufacturer.lower().replace(' ', '') + '.com',
                   manufacturer.lower().replace(' ', '-') + '.com']
        for host in guesses:
            # Try each candidate SKU; first that yields URLs wins
            for sku in skus:
                urls = sm.find_pages(host, sku)
                if urls:
                    pages.extend(urls)
                    break
            if pages:
                break

    # Always also run search — even when sitemap found brand-domain pages.
    # Reason: brand-domain pages may all be blocked by robots.txt, and we need
    # partner / reseller pages as fallback. Sitemap results stay first in the
    # list so brand-domain is still preferred.
    searcher = SearchProvider(
        provider=config['search_provider'],
        api_key=config['search_api_key'],
        user_agent=config['user_agent'],
    )
    for r in searcher.search_product_page(product):
        if r.url not in pages:
            pages.append(r.url)
        if len(pages) >= 5:
            break

    return pages[:5]


def _classifier_factory(config, env, job):
    from .ai_image_classifier import AIImageClassifier
    include_main = bool(job and getattr(job, 'discover_main_image', False))
    return lambda: AIImageClassifier(
        api_key=config['anthropic_api_key'],
        model=config['anthropic_model'],
        env=env, job=job, include_main=include_main,
    )


def _try_recipe(env, soup, page_url, config):
    """Returns (used_recipe: bool, classification_dict_or_None)."""
    if not config.get('recipe_cache_enabled', True):
        return False, None
    domain = urlparse(page_url).netloc
    recipe = env['aipie.scraping.recipe'].sudo().search([('domain', '=', domain)], limit=1)
    if not recipe or not recipe.recipe_built or not recipe.active:
        return False, None
    try:
        images = recipe.extract_candidates(soup, page_url)
    except Exception as e:
        # Malformed selector (Claude can return broken CSS). Fall back to AI
        # classification and mark the recipe failure so it gets rebuilt.
        _logger.warning('Recipe extraction failed on %s: %s', domain, e)
        try:
            recipe.record_recipe_failure()
        except Exception:
            pass
        return False, None
    if not images:
        return False, None
    return True, {
        'page_is_correct_product': True,
        'product_match_confidence': 0.9,
        'match_reasoning': f'recipe-cache hit on {domain}',
        'images': images,
        'rejected': [],
        'via_recipe': recipe.id,
    }


def _create_candidates_from_classification(
    product, job, classification, page_url, config, env,
    fetcher, cls_factory, stats, max_to_create=None,
):
    """Attempt to download + validate + record image candidates from a single matched page.

    Returns (created_count, main_created, image_outcomes_list).
    image_outcomes_list is a list of short strings explaining each image's fate
    (e.g. 'too small', 'off-brand host', 'created as main') — used to surface
    diagnostic info when a page matched but yielded zero usable candidates.
    """
    analyzer = _get_bg_analyzer(config)
    normalizer = _get_normalizer()
    min_conf = config['min_confidence_score']
    max_imgs = config['max_images_per_product']
    brand = product._effective_manufacturer() or ''
    strict_brand_setting = config.get('strict_brand_url_match', True)
    page_is_on_brand = bool(page_url and brand and _url_host_contains_brand(page_url, brand))
    apply_strict_brand_filter = strict_brand_setting and brand and page_is_on_brand
    if strict_brand_setting and brand and not page_is_on_brand:
        _log_now(env, {
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'classify',
            'level': 'info',
            'message': f'Chosen page is NOT on brand domain "{brand}" (brand site unreachable, blocked, or yielded no usable images). Accepting images from fallback source: {page_url}',
        })

    created_count = 0
    gallery_created = 0
    main_created = False
    outcomes = []
    # Cap is GALLERY only — main is allowed in addition (max 1 main per product anyway).
    # max_to_create from caller represents remaining gallery slots across pages.
    gallery_cap = max_to_create if max_to_create is not None else max_imgs

    # Pre-hash the existing main image once for dedupe — skip any candidate that
    # is visually the same as the product's current main.
    existing_main_bytes = None
    if product.image_1920:
        try:
            existing_main_bytes = base64.b64decode(product.image_1920)
        except Exception:
            existing_main_bytes = None

    for img_info in classification.get('images', []):
        role_check = img_info.get('role') or ''
        # Stop only when the GALLERY cap is hit; mains are always allowed.
        if role_check != 'main' and gallery_created >= gallery_cap:
            break
        if float(img_info.get('confidence', 0)) < min_conf:
            outcomes.append(f'low-confidence ({img_info.get("confidence", 0):.2f})')
            continue

        url = img_info.get('url')
        if not url:
            continue

        # Skip URLs we know yield nothing useful: YouTube video thumbnails
        # (img.youtube.com/vi/<id>/0.jpg) are 480px poster frames, never the
        # actual product image. Vimeo's i.vimeocdn.com follows the same pattern.
        url_host_lower = urlparse(url).netloc.lower()
        if url_host_lower in ('img.youtube.com', 'i.ytimg.com', 'i.vimeocdn.com'):
            outcomes.append(f'{img_info.get("role") or "uncertain"}: video-thumbnail host (skipped)')
            continue

        role = img_info.get('role') or 'uncertain'

        if apply_strict_brand_filter and not _url_host_contains_brand(url, brand):
            _log_now(env, {
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'classify',
                'level': 'warning',
                'message': f'Rejected (off-brand image host on brand page): {url}',
            })
            outcomes.append(f'{role}: off-brand host')
            continue

        # Bump CDN thumbnail params (e.g. ?width=100) to full-size before downloading.
        # If the upscaled URL fails, fall back to the original.
        upscaled = _upscale_cdn_url(url)
        raw, mimetype = fetcher.download_image(upscaled)
        if not raw and upscaled != url:
            raw, mimetype = fetcher.download_image(url)
        if not raw:
            outcomes.append(f'{role}: download failed')
            continue
        ok, err = _validate_image(raw, config)
        if not ok:
            _log_now(env, {
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'download_image',
                'level': 'warning',
                'message': f'{url}: {err}',
            })
            outcomes.append(f'{role}: {err}')
            continue

        # Dedupe gallery candidates against the existing main image so we don't
        # store a near-duplicate of what the customer already sees as the hero shot.
        if role != 'main' and existing_main_bytes and _images_similar(raw, existing_main_bytes):
            outcomes.append(f'{role}: visually identical to existing main image')
            continue

        # Background analysis can crash on formats PIL doesn't support (e.g. WebP
        # without libwebp). Default to 'complex' so Photoroom handles it downstream
        # — the actual BG removal call accepts these formats fine.
        try:
            has_white, white_pct, _info = analyzer.analyze(raw)
        except Exception as analyze_err:
            _logger.info('analyzer.analyze() failed for %s (treating as complex): %s', url, analyze_err)
            has_white, white_pct = False, 0.0

        # Defensive: if discover_main_image is OFF on the job but Claude returned
        # a 'main' anyway (against the prompt), demote to 'angle' so it still
        # contributes to the gallery without overwriting image_1920 on apply.
        job_wants_main = bool(job and getattr(job, 'discover_main_image', False))
        if role == 'main' and not job_wants_main:
            role = 'angle'

        if role == 'main':
            main_created = True

        if has_white:
            stats['white_bg'] += 1
        else:
            stats['rembg'] += 1

        try:
            if role == 'main':
                try:
                    bg_removed = _get_bg_dispatcher(config).remove(raw)
                except Exception as bg_err:
                    _logger.info('Preview BG removal failed (approximating): %s', bg_err)
                    bg_removed = raw
                preview = normalizer.normalize(
                    bg_removed,
                    target_size=config['target_canvas_size'],
                    padding_percent=config['padding_percent'],
                    bg_color=config['bg_color'],
                    white_threshold=config['white_threshold'],
                    transparent_canvas=True,
                )
            else:
                preview = raw
            preview_b64 = base64.b64encode(preview)
        except Exception as e:
            _logger.warning('Preview gen failed: %s', e)
            preview_b64 = False

        try:
            from PIL import Image as _PI
            im = _PI.open(io.BytesIO(raw))
            w, h = im.size
        except Exception:
            w = h = 0

        env['aipie.product.image.candidate'].sudo().create({
            'product_id': product.id,
            'job_id': job.id if job else False,
            'source_url': url,
            'source_page_url': page_url,
            'role': role,
            'confidence': float(img_info.get('confidence', 0)),
            'ai_reasoning': img_info.get('reasoning') or '',
            'image_data': base64.b64encode(raw),
            'image_width': w,
            'image_height': h,
            'image_filesize_kb': int(len(raw) / 1024),
            'image_mimetype': mimetype or '',
            'has_white_background': has_white,
            'background_white_percent': white_pct,
            'preview_normalized_image': preview_b64,
            'state': 'pending',
        })
        outcomes.append(f'{role}: created')
        created_count += 1
        if role != 'main':
            gallery_created += 1

    return created_count, main_created, outcomes


def _maybe_screenshot_render(fetcher, page_url, config):
    """If DOM has very few images, try Browserless to get rendered HTML."""
    if not config.get('browserless_api_key'):
        return None
    from .screenshot_provider import BrowserlessClient, ScreenshotError
    try:
        client = BrowserlessClient(
            api_key=config['browserless_api_key'],
            endpoint=config.get('browserless_endpoint') or 'https://chrome.browserless.io',
        )
        rendered_html = client.get_rendered_html(page_url)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(rendered_html, 'lxml')
        for tag in soup(['script', 'style', 'noscript', 'iframe']):
            tag.decompose()
        return soup
    except ScreenshotError as e:
        _logger.info('Browserless render failed for %s: %s', page_url, e)
        return None


def enrich_product(product, job, config, env):
    """Discovery pipeline. Returns (cost_usd_consumed_locally, stats_dict)."""
    from odoo import fields as odoo_fields
    from .ai_image_classifier import AIImageClassifier, AIImageClassifierError
    from .page_fetcher import PageFetcher

    stats = {'white_bg': 0, 'rembg': 0, 'recipe_hit': 0, 'recipe_miss': 0,
             'screenshot_used': 0}
    cost = 0.0

    deadline = time.time() + PER_PRODUCT_BUDGET_SECONDS

    product.aipie_enrichment_last_run = odoo_fields.Datetime.now()

    # Early skip: products without an extractable model number waste API budget
    # because brand + generic name searches return random results.
    if not _has_real_model_identifier(product):
        _log_now(env, {
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'search',
            'level': 'info',
            'message': (
                'Skipped — no manufacturer model number available. Searching by brand + '
                'generic product name yields irrelevant results (would waste Claude API budget). '
                'To enable enrichment for this product, populate aipie_manufacturer_model '
                'with the manufacturer\'s public model name (e.g. "AV4X" or "TBL1620"). '
                f'\n\nModel diagnostic: {_model_diagnostic(product)}'
            ),
        })
        product.aipie_enrichment_state = 'skipped_no_sku'
        return cost, stats

    product.aipie_enrichment_state = 'searching'

    fetcher = PageFetcher(
        user_agent=config['user_agent'],
        request_delay_seconds=config['request_delay_seconds'],
    )
    cls_factory = _classifier_factory(config, env, job)

    page_urls = _discover_pages(product, config)
    if not page_urls:
        # Log this so the operator can see WHY there were no results
        brand = (product._effective_manufacturer() or '').strip()
        skus = _extract_likely_models(product)
        reason_bits = []
        if not config.get('search_api_key') and config.get('search_provider') in ('brave', 'serpapi', 'google_cse'):
            reason_bits.append(f'Search provider "{config["search_provider"]}" has NO API key configured')
        if not brand:
            reason_bits.append('brand could not be resolved (no Brand attribute, no override, no first-word fallback)')
        if not skus:
            reason_bits.append('no manufacturer model resolved (see Model diagnostic below)')
        reason = '; '.join(reason_bits) if reason_bits else (
            f'Search query "{brand} {" ".join(skus)} {product.name}" returned no candidate pages — '
            f'check that at least one of the SKU candidates matches what the manufacturer uses publicly.'
        )
        _log_now(env, {
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'search',
            'level': 'warning',
            'message': f'No candidate pages discovered. {reason}\n\nModel diagnostic: {_model_diagnostic(product)}',
        })
        product.aipie_enrichment_state = 'no_results'
        return cost, stats

    chosen_classification = None
    chosen_page_url = None
    via_recipe_id = None

    page_outcomes = []  # (url, outcome) tuples
    blocked_hosts = set()  # hosts where robots.txt disallowed — silently skip subsequent URLs

    # Pre-count URLs per host so we can log "host X blocked, skipping N remaining URLs" once
    host_counts = {}
    for u in page_urls:
        h = urlparse(u).netloc
        host_counts[h] = host_counts.get(h, 0) + 1

    # max_images_per_product = the GALLERY cap. Main is allowed in addition
    # (max one main per product). With discover_main on and max=4, you get up to
    # 1 main + 4 gallery = 5 candidates total.
    gallery_target = config.get('max_images_per_product', 4)
    total_created = 0      # main + gallery combined (for stats / outcomes)
    total_gallery = 0      # gallery only (for cap check)
    total_main_created = False

    timed_out = False
    for url in page_urls:
        if time.time() > deadline:
            timed_out = True
            _log_now(env, {
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'fetch_page',
                'level': 'warning',
                'message': (
                    f'Per-product time budget ({PER_PRODUCT_BUDGET_SECONDS}s) exhausted '
                    f'before processing all candidate pages. Processed {len(page_outcomes)}/'
                    f'{len(page_urls)}. Bailing out to avoid worker SIGXCPU.'
                ),
            })
            break
        if total_gallery >= gallery_target and total_main_created:
            break  # Got our main + full gallery — done
        if total_gallery >= gallery_target and not (job and getattr(job, 'discover_main_image', False)):
            break  # Gallery full and we're not looking for main

        host = urlparse(url).netloc
        if host in blocked_hosts:
            # Silently skip — already logged the block reason for this host
            continue

        html, soup, fetch_reason = fetcher.fetch(url)
        if not soup:
            if fetch_reason and 'robots.txt' in fetch_reason:
                # Mark host blocked and log ONCE with remaining-URL count
                blocked_hosts.add(host)
                remaining = host_counts.get(host, 1) - 1
                suffix = f' (skipping {remaining} other URL(s) from this host)' if remaining > 0 else ''
                page_outcomes.append((url, f'host {host} blocked by robots.txt{suffix}'))
            else:
                page_outcomes.append((url, f'fetch failed — {fetch_reason}'))
            continue

        result = None
        is_recipe = False

        used_recipe, recipe_result = _try_recipe(env, soup, url, config)
        if used_recipe:
            result = recipe_result
            is_recipe = True
        else:
            img_count = len(soup.find_all('img'))
            if img_count < 5:
                rendered = _maybe_screenshot_render(fetcher, url, config)
                if rendered is not None:
                    soup = rendered
                    stats['screenshot_used'] += 1

            try:
                classifier = cls_factory()
                result = classifier.classify(product, url, soup)
            except AIImageClassifierError as e:
                _log_now(env, {
                    'job_id': job.id if job else False,
                    'product_id': product.id,
                    'step': 'classify',
                    'level': 'error',
                    'message': str(e)[:5000],
                })
                page_outcomes.append((url, f'classify error — {e}'))
                continue

            match_conf = float(result.get('product_match_confidence', 0))
            if not (result.get('page_is_correct_product') and match_conf >= 0.5):
                reasoning = (result.get('match_reasoning') or '')[:200]
                page_outcomes.append((
                    url,
                    f'AI says not the right product (confidence {match_conf:.2f}). {reasoning}'
                ))
                continue

        # Page matches — try to extract candidates, capped to remaining GALLERY slots.
        # Main candidates are always allowed (only 1 per product anyway).
        remaining_gallery = max(0, gallery_target - total_gallery)
        page_created, page_main_made, image_outcomes = _create_candidates_from_classification(
            product=product, job=job, classification=result, page_url=url,
            config=config, env=env, fetcher=fetcher, cls_factory=cls_factory,
            stats=stats, max_to_create=remaining_gallery,
        )

        if page_created > 0:
            total_created += page_created
            # Increment gallery counter only by the non-main portion
            if page_main_made:
                total_gallery += (page_created - 1)
            else:
                total_gallery += page_created
            total_main_created = total_main_created or page_main_made
            # Remember first matching page (for recipe learning + state mgmt)
            if not chosen_classification:
                chosen_classification = result
                chosen_page_url = url
                if is_recipe:
                    via_recipe_id = result.get('via_recipe')
                    stats['recipe_hit'] += 1
                else:
                    stats['recipe_miss'] += 1
            page_outcomes.append((
                url,
                f'{page_created} candidate(s) created from this page ({", ".join(image_outcomes)})'
            ))
        else:
            page_outcomes.append((
                url,
                f'matched but yielded 0 usable images ({"; ".join(image_outcomes) or "no images"})'
            ))

    # Surface accumulated outcomes whether we succeeded or not
    if total_created > 0:
        details = '\n'.join(f'  • {u}\n    → {r}' for u, r in page_outcomes)
        _log_now(env, {
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'classify',
            'level': 'info',
            'message': f'Collected {total_created} candidate(s) ({total_gallery} gallery / target {gallery_target}, main: {"yes" if total_main_created else "no"}) from:\n{details}'[:5000],
        })

    if total_created == 0:
        # Log every page outcome so the operator sees exactly what happened
        if page_outcomes:
            details = '\n'.join(f'  • {u}\n    → {r}' for u, r in page_outcomes)
            msg = f'No matching page found across {len(page_outcomes)} candidate(s):\n{details}'
        else:
            msg = ('Search/sitemap returned candidate pages but none could be processed. '
                   'Check Brave key validity and brand-domain reachability.')
        msg += f'\n\nModel diagnostic: {_model_diagnostic(product)}'
        _log_now(env, {
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'fetch_page',
            'level': 'warning',
            'message': msg[:5000],
        })
        # If we ran out of time mid-loop, surface as 'error' so the operator can
        # retry — 'no_results' would imply a final answer, but we never finished.
        product.aipie_enrichment_state = 'error' if timed_out else 'no_results'
        if timed_out:
            product.aipie_enrichment_error = (
                f'Time budget ({PER_PRODUCT_BUDGET_SECONDS}s) exhausted before any '
                f'candidate page could be classified. Likely cause: slow upstream sites. '
                f'Re-run the job to retry this product.'
            )
        return cost, stats

    min_conf = config['min_confidence_score']

    # Recipe learning: this AI run yielded ≥1 candidate from a brand-domain page — feed it back
    if (config.get('recipe_cache_enabled', True)
            and not via_recipe_id
            and chosen_page_url
            and chosen_classification):
        domain = urlparse(chosen_page_url).netloc
        recipe = env['aipie.scraping.recipe'].sudo().get_or_create_for_domain(domain)
        successful_urls = [
            img['url'] for img in chosen_classification.get('images', [])
            if float(img.get('confidence', 0)) >= min_conf
        ]
        recipe.record_ai_success(
            chosen_page_url, successful_urls,
            env=env, classifier_factory=cls_factory,
        )

    # Only flag 'needs_manual_main' when the operator explicitly asked for main
    # discovery and we still couldn't find one. Default (gallery-only) runs always
    # land in 'candidates_found'.
    if job and getattr(job, 'discover_main_image', False) and not total_main_created:
        product.aipie_enrichment_state = 'needs_manual_main'
    else:
        product.aipie_enrichment_state = 'candidates_found'

    # Auto-apply path
    if job and job.pipeline_steps in ('discover_apply', 'full'):
        threshold = max(min_conf, 0.85)
        cands = product.aipie_candidate_ids.filtered(
            lambda c: c.state == 'pending' and c.confidence >= threshold
        )
        for c in cands:
            try:
                apply_candidate_to_product(c, config, env)
            except Exception as e:
                _logger.exception('Auto-apply failed for candidate %s', c.id)
                c.state = 'failed'
                c.rejection_reason = str(e)[:255]
        if any(c.state == 'applied' and c.role == 'main' for c in cands):
            product.aipie_enrichment_state = 'enriched'

    return cost, stats


def apply_candidate_to_product(candidate, config, env):
    """Promote a reviewed candidate to the product. Runs background removal if needed."""
    product = candidate.product_id
    if not candidate.image_data:
        raise ValueError('Candidate has no image data')

    raw = base64.b64decode(candidate.image_data)
    normalizer = _get_normalizer()

    if candidate.role == 'main':
        # Clicking Apply on a main candidate IS the operator's explicit consent
        # to overwrite the existing main image. The previous main is preserved
        # via _backup_main → aipie_original_main_image, so Revert is always
        # available. No global gate.
        _backup_main(product, config)

        # Main images are always transparent PNG, regardless of source background.
        try:
            transparent = _get_bg_dispatcher(config).remove(raw)
        except Exception as e:
            _logger.warning('BG removal failed during apply, using raw bytes (will halo): %s', e)
            transparent = raw

        normalized = normalizer.normalize(
            transparent,
            target_size=config['target_canvas_size'],
            padding_percent=config['padding_percent'],
            bg_color=config['bg_color'],
            white_threshold=config['white_threshold'],
            transparent_canvas=True,
        )
        product.aipie_main_image_already_white_bg = candidate.has_white_background

        product.image_1920 = base64.b64encode(normalized)
        product.aipie_main_image_normalized = True
        product.aipie_normalization_signature = _output_signature(normalized, config)
        product.aipie_enrichment_state = 'enriched'

    else:
        env['product.image'].sudo().create({
            'name': f'{product.name} - {candidate.role}',
            'image_1920': candidate.image_data,
            'product_tmpl_id': product.id,
            'sequence': ROLE_TO_SEQUENCE.get(candidate.role, 99),
        })

    candidate.state = 'applied'
