"""
Self-learning per-domain scraping recipe cache.

How it works:
  1. First N (default 5) successful AI classifications on a domain are pure-AI runs.
  2. After N successes, we ask Claude *once*: "given these N successful pages
     and the image URLs you correctly identified, produce CSS selectors that
     would have selected just the main + gallery images."
  3. The recipe is saved. Future products on that domain try the recipe first
     (free, fast). If it returns plausible candidates, we skip Claude entirely.
  4. If the recipe fails (too few candidates, or post-validation rejects them),
     we fall back to AI and decrement reliability. Three consecutive failures
     deactivate the recipe and trigger a rebuild.

Effect: AI cost asymptotes toward zero as the catalog grows. The AI teaches
the code, then steps aside.
"""

import json
import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

DEFAULT_BUILD_THRESHOLD = 5
MAX_CONSECUTIVE_FAILURES = 3


class ScrapingRecipe(models.Model):
    _name = 'laudie.scraping.recipe'
    _description = 'Learned per-domain scraping recipe'
    _order = 'success_count desc, write_date desc'

    domain = fields.Char(required=True, index=True)
    success_count = fields.Integer(default=0,
        help='Successful AI classifications on this domain (used as recipe builder evidence).')
    recipe_built = fields.Boolean(default=False)

    main_image_selector = fields.Char(string='Main image CSS selector')
    gallery_image_selector = fields.Char(string='Gallery CSS selector')
    image_url_attribute = fields.Char(default='src',
        help='Attribute to read the image URL from. Often src, sometimes data-src or data-lazy-src.')
    excluded_url_patterns = fields.Char(
        help='Comma-separated substrings to reject (e.g. "thumb,sprite,logo").')

    raw_recipe_json = fields.Text(help='Full JSON returned by Claude for the recipe build.')
    notes = fields.Text()

    last_built = fields.Datetime()
    last_validated = fields.Datetime()
    last_failure = fields.Datetime()
    consecutive_failures = fields.Integer(default=0)
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('uniq_domain', 'unique(domain)', 'One recipe per domain.'),
    ]

    # ---------- public API ----------

    @api.model
    def get_or_create_for_domain(self, domain):
        rec = self.search([('domain', '=', domain)], limit=1)
        if not rec:
            rec = self.create({'domain': domain})
        return rec

    def record_ai_success(self, page_url, picked_image_urls, env=None, classifier_factory=None):
        """Increment success counter; build recipe when threshold hit."""
        self.ensure_one()
        self.success_count += 1
        if (not self.recipe_built
                and self.success_count >= DEFAULT_BUILD_THRESHOLD
                and self.active
                and classifier_factory):
            try:
                self._build_recipe(env, classifier_factory)
            except Exception as e:
                _logger.warning('Recipe build failed for %s: %s', self.domain, e)

    def record_recipe_failure(self):
        self.ensure_one()
        self.consecutive_failures += 1
        self.last_failure = fields.Datetime.now()
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.active = False
            self.recipe_built = False
            _logger.info('Recipe deactivated for %s after %d failures.',
                         self.domain, self.consecutive_failures)

    def record_recipe_success(self):
        self.ensure_one()
        self.consecutive_failures = 0
        self.last_validated = fields.Datetime.now()

    def extract_candidates(self, soup, page_url):
        """Return list of dicts {url, role} extracted via the recipe's selectors.

        Caller treats this as a hint; should still validate downloaded images.
        Returns [] if recipe is inactive or yields nothing.
        """
        self.ensure_one()
        if not self.active or not self.recipe_built:
            return []
        from urllib.parse import urljoin

        attr = self.image_url_attribute or 'src'
        excluded = [p.strip().lower() for p in (self.excluded_url_patterns or '').split(',') if p.strip()]

        out = []
        if self.main_image_selector:
            for el in soup.select(self.main_image_selector)[:1]:
                src = el.get(attr) or el.get('src')
                if not src:
                    continue
                abs_url = urljoin(page_url, src)
                if any(e in abs_url.lower() for e in excluded):
                    continue
                out.append({'url': abs_url, 'role': 'main', 'confidence': 0.9,
                            'reasoning': 'recipe-extracted main'})
        if self.gallery_image_selector:
            seen = {c['url'] for c in out}
            for el in soup.select(self.gallery_image_selector):
                src = el.get(attr) or el.get('src')
                if not src:
                    continue
                abs_url = urljoin(page_url, src)
                if abs_url in seen:
                    continue
                if any(e in abs_url.lower() for e in excluded):
                    continue
                seen.add(abs_url)
                out.append({'url': abs_url, 'role': 'angle', 'confidence': 0.85,
                            'reasoning': 'recipe-extracted gallery'})
        return out

    # ---------- recipe building ----------

    def _build_recipe(self, env, classifier_factory):
        """Ask Claude to distill a CSS-selector recipe from N successful pages."""
        self.ensure_one()
        # Gather most recent N successful applied/approved candidates on this domain
        Candidate = env['laudie.product.image.candidate']
        cands = Candidate.search([
            ('source_page_url', 'ilike', self.domain),
            ('state', 'in', ('approved', 'applied')),
        ], limit=20, order='create_date desc')

        if len(cands) < DEFAULT_BUILD_THRESHOLD:
            return

        # Group by source_page_url
        by_page = {}
        for c in cands:
            by_page.setdefault(c.source_page_url, []).append(c)
        if len(by_page) < DEFAULT_BUILD_THRESHOLD:
            return

        examples = []
        for page_url, items in list(by_page.items())[:DEFAULT_BUILD_THRESHOLD]:
            examples.append({
                'page_url': page_url,
                'identified_images': [
                    {'url': c.source_url, 'role': c.role}
                    for c in items
                ],
            })

        prompt = (
            'You previously identified product images on these pages from the same domain.\n\n'
            f'Examples:\n{json.dumps(examples, indent=2)}\n\n'
            'Output STRICT JSON:\n'
            '{\n'
            '  "main_image_selector": "<CSS selector that picks the SINGLE main product image>",\n'
            '  "gallery_image_selector": "<CSS selector that picks gallery thumbnails or alternate views>",\n'
            '  "image_url_attribute": "<src | data-src | data-lazy-src>",\n'
            '  "excluded_url_patterns": "<comma-separated substrings like thumb,sprite,logo>",\n'
            '  "notes": "<reasoning>"\n'
            '}\n'
            'Be specific — use class names, IDs, or attribute selectors. Do not invent selectors that '
            'were not present in the example image URLs context. If you cannot determine a reliable '
            'recipe, set the selectors to null and explain in notes.'
        )

        classifier = classifier_factory()
        # Re-uses the SDK seam — directly call _call_claude with our prompt
        try:
            recipe = classifier._call_claude(prompt, product=None)  # type: ignore[attr-defined]
        except Exception as e:
            _logger.warning('Recipe build call failed for %s: %s', self.domain, e)
            return

        self.write({
            'main_image_selector': recipe.get('main_image_selector') or '',
            'gallery_image_selector': recipe.get('gallery_image_selector') or '',
            'image_url_attribute': recipe.get('image_url_attribute') or 'src',
            'excluded_url_patterns': recipe.get('excluded_url_patterns') or '',
            'notes': recipe.get('notes') or '',
            'raw_recipe_json': json.dumps(recipe, indent=2),
            'last_built': fields.Datetime.now(),
            'recipe_built': bool(recipe.get('main_image_selector') or recipe.get('gallery_image_selector')),
        })
        _logger.info('Recipe built for %s: main=%r gallery=%r',
                     self.domain, self.main_image_selector, self.gallery_image_selector)
