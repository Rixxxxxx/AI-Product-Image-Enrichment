from odoo import api, fields, models


PARAM_KEYS = [
    'aipie_anthropic_api_key',
    'aipie_anthropic_model',
    'aipie_search_provider',
    'aipie_search_api_key',
    'aipie_target_canvas_size',
    'aipie_padding_percent',
    'aipie_bg_color',
    'aipie_jpeg_quality',
    'aipie_output_format',
    'aipie_white_threshold',
    'aipie_white_bg_min_percent',
    'aipie_force_normalize_existing',
    'aipie_max_images_per_product',
    'aipie_min_image_width',
    'aipie_rembg_model',
    'aipie_overwrite_existing_main',
    'aipie_require_review',
    'aipie_min_confidence_score',
    'aipie_user_agent',
    'aipie_request_delay_seconds',
    'aipie_concurrent_workers',
    'aipie_monthly_ai_budget_usd',
    'aipie_alert_email',
    'aipie_keep_backup',
]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # API Credentials
    aipie_anthropic_api_key = fields.Char(
        string='Anthropic API Key',
        config_parameter='ai_product_image_enrichment.aipie_anthropic_api_key',
    )
    aipie_anthropic_model = fields.Selection([
        ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5 (recommended, cheap)'),
        ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
        ('claude-opus-4-7', 'Claude Opus 4.7 (expensive, do not use as default)'),
    ], string='Claude Model', default='claude-haiku-4-5-20251001',
       config_parameter='ai_product_image_enrichment.aipie_anthropic_model')

    aipie_search_provider = fields.Selection([
        ('brave', 'Brave Search API (recommended)'),
        ('serpapi', 'SerpAPI'),
        ('google_cse', 'Google Custom Search'),
        ('duckduckgo_html', 'DuckDuckGo HTML (no key, unreliable)'),
    ], string='Search Provider', default='brave',
       config_parameter='ai_product_image_enrichment.aipie_search_provider')
    aipie_search_api_key = fields.Char(
        string='Search API Key',
        config_parameter='ai_product_image_enrichment.aipie_search_api_key',
    )
    aipie_photoroom_api_key = fields.Char(
        string='Photoroom API Key',
        help='Recommended over local rembg. Photoroom handles background removal as a service '
             '(~$0.02/image) — eliminates ~400MB of dependencies and ~500MB RAM at inference. '
             'Leave empty to fall back to local rembg.',
        config_parameter='ai_product_image_enrichment.aipie_photoroom_api_key',
    )
    aipie_browserless_api_key = fields.Char(
        string='Browserless API Key (optional)',
        help='Headless screenshot service. Used as fallback when DOM parsing finds no images on '
             'JS-rendered (SPA) manufacturer pages. Leave empty to disable the screenshot fallback.',
        config_parameter='ai_product_image_enrichment.aipie_browserless_api_key',
    )
    aipie_browserless_endpoint = fields.Char(
        string='Browserless Endpoint',
        default='https://chrome.browserless.io',
        config_parameter='ai_product_image_enrichment.aipie_browserless_endpoint',
    )
    aipie_strict_white_main = fields.Boolean(
        string='Reject lifestyle shots as main image', default=True,
        help='When ON: only studio-quality source images are accepted as the main image. '
             'In-use / lifestyle / context shots (product on a real surface, with people, '
             'in an environment) are downgraded to gallery candidates and never auto-applied '
             'as main. Products with no studio source available go to "Needs Manual Main Image" '
             'state. Note: this controls SOURCE filtering — the FINAL output is always a '
             'transparent PNG regardless. Strongly recommended ON for clean shop grids.',
        config_parameter='ai_product_image_enrichment.aipie_strict_white_main',
    )
    aipie_recipe_cache_enabled = fields.Boolean(
        string='Self-learning Recipe Cache', default=True,
        help='After 5 successful AI classifications on a domain, distill a CSS-selector recipe '
             'and use it for future products on that domain (free, fast). AI cost asymptotes '
             'toward zero as the catalog grows.',
        config_parameter='ai_product_image_enrichment.aipie_recipe_cache_enabled',
    )
    aipie_brand_attribute_name = fields.Char(
        string='Brand Attribute Name', default='Brand',
        help='Name of the product attribute that holds the manufacturer / brand name. '
             'The AI pipeline reads this attribute to identify the brand. '
             'Falls back to the first word of the product name if the attribute is missing.',
        config_parameter='ai_product_image_enrichment.aipie_brand_attribute_name',
    )
    aipie_strict_brand_url_match = fields.Boolean(
        string='Only accept images from the brand domain', default=True,
        help='Reject any candidate image whose URL host does NOT contain the brand name. '
             'Guarantees images come from the actual manufacturer source (or their CDN), '
             'not from third-party aggregators or marketplaces. Strongly recommended.',
        config_parameter='ai_product_image_enrichment.aipie_strict_brand_url_match',
    )

    # Normalization
    aipie_target_canvas_size = fields.Integer(
        string='Target Canvas Size (px)', default=1920,
        help='Final image dimension in pixels (square). 1600 gives crisp PDP zoom while staying under Odoo 1920 max.',
        config_parameter='ai_product_image_enrichment.aipie_target_canvas_size',
    )
    aipie_padding_percent = fields.Integer(
        string='Padding (%)', default=8,
        help='Whitespace padding around product as % of canvas. THIS is what makes products look uniformly sized in the grid.',
        config_parameter='ai_product_image_enrichment.aipie_padding_percent',
    )
    aipie_bg_color = fields.Char(
        string='Background Color', default='#FFFFFF',
        config_parameter='ai_product_image_enrichment.aipie_bg_color',
    )
    aipie_jpeg_quality = fields.Integer(
        string='JPEG Quality', default=92,
        config_parameter='ai_product_image_enrichment.aipie_jpeg_quality',
    )
    aipie_output_format = fields.Selection([
        ('jpeg', 'JPEG (recommended for white-bg product shots)'),
        ('png', 'PNG'),
        ('auto', 'Auto'),
    ], string='Output Format', default='jpeg',
       config_parameter='ai_product_image_enrichment.aipie_output_format')
    aipie_white_threshold = fields.Integer(
        string='White Threshold (0-255)', default=245,
        help='Pixels with R, G, B all >= this value count as "white" for background detection.',
        config_parameter='ai_product_image_enrichment.aipie_white_threshold',
    )
    aipie_white_bg_min_percent = fields.Integer(
        string='White-BG Border Min %', default=85,
        help='If at least this % of border pixels are "white", image is considered to already have a white background and rembg is skipped.',
        config_parameter='ai_product_image_enrichment.aipie_white_bg_min_percent',
    )
    aipie_force_normalize_existing = fields.Boolean(
        string='Always Normalize (even white-BG images)', default=True,
        help='Trim/center/pad even on already-white-BG images. This is what fixes inconsistent framing across the existing catalog.',
        config_parameter='ai_product_image_enrichment.aipie_force_normalize_existing',
    )

    # Pipeline
    aipie_max_images_per_product = fields.Integer(
        string='Max Images / Product', default=4,
        config_parameter='ai_product_image_enrichment.aipie_max_images_per_product',
    )
    aipie_min_image_width = fields.Integer(
        string='Min Image Width (px)', default=600,
        config_parameter='ai_product_image_enrichment.aipie_min_image_width',
    )
    aipie_rembg_model = fields.Selection([
        ('u2net', 'u2net (fast, smaller)'),
        ('birefnet-general', 'birefnet-general (best quality)'),
        ('isnet-general-use', 'isnet-general-use'),
    ], string='rembg Model', default='birefnet-general',
       config_parameter='ai_product_image_enrichment.aipie_rembg_model')
    aipie_overwrite_existing_main = fields.Boolean(
        string='Overwrite Existing Main Images', default=False,
        config_parameter='ai_product_image_enrichment.aipie_overwrite_existing_main',
    )

    # Safety
    aipie_require_review = fields.Boolean(
        string='Require Review of Candidates', default=True,
        config_parameter='ai_product_image_enrichment.aipie_require_review',
    )
    aipie_min_confidence_score = fields.Float(
        string='Min Confidence Score', default=0.7,
        config_parameter='ai_product_image_enrichment.aipie_min_confidence_score',
    )
    aipie_user_agent = fields.Char(
        string='HTTP User-Agent', default='ProductImageBot/1.0',
        config_parameter='ai_product_image_enrichment.aipie_user_agent',
    )
    aipie_request_delay_seconds = fields.Float(
        string='Per-Domain Request Delay (s)', default=2.0,
        config_parameter='ai_product_image_enrichment.aipie_request_delay_seconds',
    )
    aipie_concurrent_workers = fields.Integer(
        string='Concurrent Workers', default=2,
        config_parameter='ai_product_image_enrichment.aipie_concurrent_workers',
    )

    # Cost
    aipie_monthly_ai_budget_usd = fields.Float(
        string='Monthly AI Budget (USD)', default=50.0,
        config_parameter='ai_product_image_enrichment.aipie_monthly_ai_budget_usd',
    )
    aipie_alert_email = fields.Char(
        string='Budget Alert Email',
        config_parameter='ai_product_image_enrichment.aipie_alert_email',
    )

    # Storage
    aipie_keep_backup = fields.Boolean(
        string='Keep Original Image Backup', default=True,
        help='Saves original main image to aipie_original_main_image before normalization. Doubles storage but enables Revert.',
        config_parameter='ai_product_image_enrichment.aipie_keep_backup',
    )

    def action_open_preview_normalization(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Preview Normalization',
            'res_model': 'aipie.preview.normalization.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_pre_warm_rembg(self):
        """Trigger model download so first real run isn't slow."""
        self.ensure_one()
        model = self.env['ir.config_parameter'].sudo().get_param(
            'ai_product_image_enrichment.aipie_rembg_model', 'birefnet-general'
        )
        try:
            from ..services.background_remover import BackgroundRemover
            BackgroundRemover.get_session(model)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'rembg ready',
                    'message': f'Model "{model}" loaded successfully.',
                    'type': 'success',
                },
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'rembg setup failed',
                    'message': f'{type(e).__name__}: {e}. Check that rembg+onnxruntime are installed in the Odoo venv.',
                    'type': 'danger',
                    'sticky': True,
                },
            }

    @api.model
    def get_aipie_config(self):
        """Helper used by pipeline code — returns a plain dict of typed config values."""
        get = self.env['ir.config_parameter'].sudo().get_param
        def _int(k, d):
            try: return int(get(f'ai_product_image_enrichment.{k}', d))
            except (ValueError, TypeError): return d
        def _float(k, d):
            try: return float(get(f'ai_product_image_enrichment.{k}', d))
            except (ValueError, TypeError): return d
        def _bool(k, d):
            v = get(f'ai_product_image_enrichment.{k}')
            if v is None or v is False: return d
            return str(v).lower() in ('1', 'true', 't', 'yes')
        return {
            'anthropic_api_key': get('ai_product_image_enrichment.aipie_anthropic_api_key', ''),
            'anthropic_model': get('ai_product_image_enrichment.aipie_anthropic_model', 'claude-haiku-4-5-20251001'),
            'search_provider': get('ai_product_image_enrichment.aipie_search_provider', 'brave'),
            'search_api_key': get('ai_product_image_enrichment.aipie_search_api_key', ''),
            'photoroom_api_key': get('ai_product_image_enrichment.aipie_photoroom_api_key', ''),
            'browserless_api_key': get('ai_product_image_enrichment.aipie_browserless_api_key', ''),
            'browserless_endpoint': get('ai_product_image_enrichment.aipie_browserless_endpoint', 'https://chrome.browserless.io'),
            'strict_white_main': _bool('aipie_strict_white_main', True),
            'recipe_cache_enabled': _bool('aipie_recipe_cache_enabled', True),
            'brand_attribute_name': get('ai_product_image_enrichment.aipie_brand_attribute_name', 'Brand'),
            'strict_brand_url_match': _bool('aipie_strict_brand_url_match', True),
            'target_canvas_size': _int('aipie_target_canvas_size', 1920),
            'padding_percent': _int('aipie_padding_percent', 8),
            'bg_color': get('ai_product_image_enrichment.aipie_bg_color', '#FFFFFF'),
            'jpeg_quality': _int('aipie_jpeg_quality', 92),
            'output_format': get('ai_product_image_enrichment.aipie_output_format', 'jpeg'),
            'white_threshold': _int('aipie_white_threshold', 245),
            'white_bg_min_percent': _int('aipie_white_bg_min_percent', 85),
            'force_normalize_existing': _bool('aipie_force_normalize_existing', True),
            'max_images_per_product': _int('aipie_max_images_per_product', 4),
            'min_image_width': _int('aipie_min_image_width', 600),
            'rembg_model': get('ai_product_image_enrichment.aipie_rembg_model', 'birefnet-general'),
            'overwrite_existing_main': _bool('aipie_overwrite_existing_main', False),
            'require_review': _bool('aipie_require_review', True),
            'min_confidence_score': _float('aipie_min_confidence_score', 0.7),
            'user_agent': get('ai_product_image_enrichment.aipie_user_agent', 'ProductImageBot/1.0'),
            'request_delay_seconds': _float('aipie_request_delay_seconds', 2.0),
            'concurrent_workers': _int('aipie_concurrent_workers', 2),
            'monthly_ai_budget_usd': _float('aipie_monthly_ai_budget_usd', 50.0),
            'alert_email': get('ai_product_image_enrichment.aipie_alert_email', ''),
            'keep_backup': _bool('aipie_keep_backup', True),
        }
