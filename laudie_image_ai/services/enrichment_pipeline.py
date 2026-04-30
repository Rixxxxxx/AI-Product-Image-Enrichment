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
from urllib.parse import urlparse

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
        if product.image_1920 and not product.laudie_original_main_image:
            product.laudie_original_main_image = product.image_1920


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


def _validate_image(image_bytes, config):
    if not image_bytes or len(image_bytes) < 4 * 1024:
        return False, 'too small'
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(image_bytes))
        img.verify()
        img = Image.open(io.BytesIO(image_bytes))
        if img.width < config['min_image_width']:
            return False, f'width {img.width} < min {config["min_image_width"]}'
        return True, None
    except Exception as e:
        return False, f'invalid image: {e}'


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
    if product.laudie_normalization_signature and product.laudie_normalization_signature == sig:
        return {'skipped_unchanged': True}

    analyzer = _get_bg_analyzer(config)
    has_white, white_pct, info = analyzer.analyze(raw)

    # Vision disambiguation for borderline numpy results
    if not has_white and _ambiguous_white_bg(white_pct, 70, 90) and config.get('anthropic_api_key'):
        from .ai_image_classifier import AIImageClassifier
        cls = AIImageClassifier(config['anthropic_api_key'], config['anthropic_model'], env=env)
        if cls.vision_is_studio_shot(raw):
            has_white = True

    _backup_main(product, config)

    normalizer = _get_normalizer()

    # Main images are ALWAYS transparent PNG. Even if the source already has a white
    # background, we run BG removal to get a clean alpha channel (white-fill fallback
    # leaves halo artifacts on antialiased edges).
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
    product.laudie_main_image_already_white_bg = has_white

    product.image_1920 = base64.b64encode(normalized)
    product.laudie_main_image_normalized = True
    product.laudie_normalization_signature = _output_signature(normalized, config)
    if product.laudie_enrichment_state == 'not_enriched':
        product.laudie_enrichment_state = 'enriched'

    return {'was_white_bg': has_white, 'white_percent': white_pct}


# ---------- discovery + apply ----------

def _discover_pages(product, config):
    """Sitemap first, search second. Returns list of candidate page URLs."""
    from .sitemap_provider import SitemapProvider
    from .search_provider import SearchProvider

    pages = []

    sku = (product.laudie_manufacturer_sku or product.default_code or '').strip()
    manufacturer = (product._effective_manufacturer() or '').strip()

    # Try sitemap if we have manufacturer hint
    if manufacturer and sku:
        sm = SitemapProvider(user_agent=config['user_agent'])
        # Best-effort: try common host shapes
        guesses = [manufacturer.lower().replace(' ', '') + '.com',
                   manufacturer.lower().replace(' ', '-') + '.com']
        for host in guesses:
            urls = sm.find_pages(host, sku)
            if urls:
                pages.extend(urls)
                break

    # Search fallback
    if len(pages) < 3:
        searcher = SearchProvider(
            provider=config['search_provider'],
            api_key=config['search_api_key'],
            user_agent=config['user_agent'],
        )
        for r in searcher.search_product_page(product):
            if r.url not in pages:
                pages.append(r.url)
            if len(pages) >= 6:
                break

    return pages[:5]


def _classifier_factory(config, env, job):
    from .ai_image_classifier import AIImageClassifier
    return lambda: AIImageClassifier(
        api_key=config['anthropic_api_key'],
        model=config['anthropic_model'],
        env=env, job=job,
    )


def _try_recipe(env, soup, page_url, config):
    """Returns (used_recipe: bool, classification_dict_or_None)."""
    if not config.get('recipe_cache_enabled', True):
        return False, None
    domain = urlparse(page_url).netloc
    recipe = env['laudie.scraping.recipe'].sudo().search([('domain', '=', domain)], limit=1)
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

    product.laudie_enrichment_state = 'searching'
    product.laudie_enrichment_last_run = odoo_fields.Datetime.now()
    # No commit here. The cron framework commits at chunk boundary; committing
    # mid-pipeline would strand the product in 'searching' on worker kill.

    fetcher = PageFetcher(
        user_agent=config['user_agent'],
        request_delay_seconds=config['request_delay_seconds'],
    )
    cls_factory = _classifier_factory(config, env, job)

    page_urls = _discover_pages(product, config)
    if not page_urls:
        product.laudie_enrichment_state = 'no_results'
        return cost, stats

    chosen_classification = None
    chosen_page_url = None
    via_recipe_id = None

    for url in page_urls:
        html, soup = fetcher.fetch(url)
        if not soup:
            continue

        # Recipe path (free)
        used_recipe, recipe_result = _try_recipe(env, soup, url, config)
        if used_recipe:
            chosen_classification = recipe_result
            chosen_page_url = url
            via_recipe_id = recipe_result.get('via_recipe')
            stats['recipe_hit'] += 1
            break

        # Decide whether to render via Browserless: DOM has too few <img>
        img_count = len(soup.find_all('img'))
        if img_count < 5:
            rendered = _maybe_screenshot_render(fetcher, url, config)
            if rendered is not None:
                soup = rendered
                stats['screenshot_used'] += 1

        # AI path
        try:
            classifier = cls_factory()
            result = classifier.classify(product, url, soup)
        except AIImageClassifierError as e:
            env['laudie.enrichment.log'].sudo().create({
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'classify',
                'level': 'error',
                'message': str(e)[:5000],
            })
            continue

        if (result.get('page_is_correct_product')
                and float(result.get('product_match_confidence', 0)) >= 0.5):
            chosen_classification = result
            chosen_page_url = url
            stats['recipe_miss'] += 1
            break

    if not chosen_classification:
        product.laudie_enrichment_state = 'no_results'
        return cost, stats

    analyzer = _get_bg_analyzer(config)
    normalizer = _get_normalizer()
    min_conf = config['min_confidence_score']
    max_imgs = config['max_images_per_product']

    # If strict_white_main and there's no main candidate, warn — but still create gallery candidates
    has_main_candidate = any(
        (img.get('role') == 'main' and float(img.get('confidence', 0)) >= min_conf)
        for img in chosen_classification.get('images', [])
    )

    created_count = 0
    main_created = False
    brand = product._effective_manufacturer() or ''
    collect_gallery = bool(product.laudie_gallery_enabled)
    strict_brand_setting = config.get('strict_brand_url_match', True)
    # Strict brand-host filter applies only when the chosen PAGE is on the brand domain.
    # If we had to fall back to a non-brand page (e.g. the brand site was blocked by
    # robots.txt or anti-bot), images on that fallback page are accepted regardless.
    page_is_on_brand = bool(chosen_page_url and brand and _url_host_contains_brand(chosen_page_url, brand))
    apply_strict_brand_filter = strict_brand_setting and brand and page_is_on_brand
    if strict_brand_setting and brand and not page_is_on_brand:
        env['laudie.enrichment.log'].sudo().create({
            'job_id': job.id if job else False,
            'product_id': product.id,
            'step': 'classify',
            'level': 'info',
            'message': f'Brand site unreachable or empty for "{brand}" — accepting images from fallback source: {chosen_page_url}',
        })

    for img_info in chosen_classification.get('images', []):
        if created_count >= max_imgs:
            break
        if float(img_info.get('confidence', 0)) < min_conf:
            continue

        url = img_info.get('url')
        if not url:
            continue

        role = img_info.get('role') or 'uncertain'

        # Skip gallery candidates when this product/category isn't gallery-enabled.
        if role != 'main' and not collect_gallery:
            continue

        # Strict brand-source enforcement: image URL host must contain the brand keyword.
        # Only enforced when we're on a brand-domain page.
        if apply_strict_brand_filter and not _url_host_contains_brand(url, brand):
            env['laudie.enrichment.log'].sudo().create({
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'classify',
                'level': 'warning',
                'message': f'Rejected (off-brand image host on brand page): {url}',
            })
            continue

        raw, mimetype = fetcher.download_image(url)
        if not raw:
            continue
        ok, err = _validate_image(raw, config)
        if not ok:
            env['laudie.enrichment.log'].sudo().create({
                'job_id': job.id if job else False,
                'product_id': product.id,
                'step': 'download_image',
                'level': 'warning',
                'message': f'{url}: {err}',
            })
            continue

        has_white, white_pct, _info = analyzer.analyze(raw)

        # Strict mode: a "main" candidate that isn't white-BG is downgraded
        if config.get('strict_white_main', True) and role == 'main' and not has_white:
            # Vision disambiguation
            try:
                if cls_factory().vision_is_studio_shot(raw):
                    has_white = True
                else:
                    role = 'in_use'  # downgrade — gallery only, never auto-applied as main
            except Exception:
                role = 'in_use'

        # If a main was downgraded to a gallery role and gallery is disabled, skip
        if role != 'main' and not collect_gallery:
            continue

        if role == 'main':
            main_created = True

        if has_white:
            stats['white_bg'] += 1
        else:
            stats['rembg'] += 1

        # Preview: main candidates show the actual transparent normalized result so
        # the reviewer sees what will land. Gallery candidates show their raw bytes
        # (gallery images are kept as-is — backgrounds intact).
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

        env['laudie.product.image.candidate'].sudo().create({
            'product_id': product.id,
            'job_id': job.id if job else False,
            'source_url': url,
            'source_page_url': chosen_page_url,
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
        created_count += 1

    if created_count == 0:
        product.laudie_enrichment_state = 'no_results'
        return cost, stats

    # Recipe learning: this AI run was successful — feed it back
    if config.get('recipe_cache_enabled', True) and not via_recipe_id and chosen_page_url:
        domain = urlparse(chosen_page_url).netloc
        recipe = env['laudie.scraping.recipe'].sudo().get_or_create_for_domain(domain)
        successful_urls = [
            img['url'] for img in chosen_classification.get('images', [])
            if float(img.get('confidence', 0)) >= min_conf
        ]
        recipe.record_ai_success(
            chosen_page_url, successful_urls,
            env=env, classifier_factory=cls_factory,
        )

    if config.get('strict_white_main', True) and not main_created:
        product.laudie_enrichment_state = 'needs_manual_main'
    else:
        product.laudie_enrichment_state = 'candidates_found'

    # Auto-apply path
    if job and job.pipeline_steps in ('discover_apply', 'full'):
        threshold = max(min_conf, 0.85)
        cands = product.laudie_candidate_ids.filtered(
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
            product.laudie_enrichment_state = 'enriched'

    return cost, stats


def apply_candidate_to_product(candidate, config, env):
    """Promote a reviewed candidate to the product. Runs background removal if needed."""
    product = candidate.product_id
    if not candidate.image_data:
        raise ValueError('Candidate has no image data')

    raw = base64.b64decode(candidate.image_data)
    normalizer = _get_normalizer()

    if candidate.role == 'main':
        if (product.image_1920
                and not config.get('overwrite_existing_main', False)
                and product.laudie_main_image_normalized):
            raise ValueError('Main image already normalized — enable overwrite_existing_main to replace')

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
        product.laudie_main_image_already_white_bg = candidate.has_white_background

        product.image_1920 = base64.b64encode(normalized)
        product.laudie_main_image_normalized = True
        product.laudie_normalization_signature = _output_signature(normalized, config)
        product.laudie_enrichment_state = 'enriched'

    else:
        env['product.image'].sudo().create({
            'name': f'{product.name} - {candidate.role}',
            'image_1920': candidate.image_data,
            'product_tmpl_id': product.id,
            'sequence': ROLE_TO_SEQUENCE.get(candidate.role, 99),
        })

    candidate.state = 'applied'
