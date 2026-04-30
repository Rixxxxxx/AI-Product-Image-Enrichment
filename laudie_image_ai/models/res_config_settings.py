from odoo import api, fields, models


PARAM_KEYS = [
    'laudie_anthropic_api_key',
    'laudie_anthropic_model',
    'laudie_search_provider',
    'laudie_search_api_key',
    'laudie_target_canvas_size',
    'laudie_padding_percent',
    'laudie_bg_color',
    'laudie_jpeg_quality',
    'laudie_output_format',
    'laudie_white_threshold',
    'laudie_white_bg_min_percent',
    'laudie_force_normalize_existing',
    'laudie_max_images_per_product',
    'laudie_min_image_width',
    'laudie_rembg_model',
    'laudie_overwrite_existing_main',
    'laudie_require_review',
    'laudie_min_confidence_score',
    'laudie_user_agent',
    'laudie_request_delay_seconds',
    'laudie_concurrent_workers',
    'laudie_monthly_ai_budget_usd',
    'laudie_alert_email',
    'laudie_keep_backup',
]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # API Credentials
    laudie_anthropic_api_key = fields.Char(
        string='Anthropic API Key',
        config_parameter='laudie_image_ai.laudie_anthropic_api_key',
    )
    laudie_anthropic_model = fields.Selection([
        ('claude-haiku-4-5-20251001', 'Claude Haiku 4.5 (recommended, cheap)'),
        ('claude-sonnet-4-6', 'Claude Sonnet 4.6'),
        ('claude-opus-4-7', 'Claude Opus 4.7 (expensive, do not use as default)'),
    ], string='Claude Model', default='claude-haiku-4-5-20251001',
       config_parameter='laudie_image_ai.laudie_anthropic_model')

    laudie_search_provider = fields.Selection([
        ('brave', 'Brave Search API (recommended)'),
        ('serpapi', 'SerpAPI'),
        ('google_cse', 'Google Custom Search'),
        ('duckduckgo_html', 'DuckDuckGo HTML (no key, unreliable)'),
    ], string='Search Provider', default='brave',
       config_parameter='laudie_image_ai.laudie_search_provider')
    laudie_search_api_key = fields.Char(
        string='Search API Key',
        config_parameter='laudie_image_ai.laudie_search_api_key',
    )
    laudie_photoroom_api_key = fields.Char(
        string='Photoroom API Key',
        help='Recommended over local rembg. Photoroom handles background removal as a service '
             '(~$0.02/image) — eliminates ~400MB of dependencies and ~500MB RAM at inference. '
             'Leave empty to fall back to local rembg.',
        config_parameter='laudie_image_ai.laudie_photoroom_api_key',
    )
    laudie_browserless_api_key = fields.Char(
        string='Browserless API Key (optional)',
        help='Headless screenshot service. Used as fallback when DOM parsing finds no images on '
             'JS-rendered (SPA) manufacturer pages. Leave empty to disable the screenshot fallback.',
        config_parameter='laudie_image_ai.laudie_browserless_api_key',
    )
    laudie_browserless_endpoint = fields.Char(
        string='Browserless Endpoint',
        default='https://chrome.browserless.io',
        config_parameter='laudie_image_ai.laudie_browserless_endpoint',
    )
    laudie_strict_white_main = fields.Boolean(
        string='Reject lifestyle shots as main image', default=True,
        help='When ON: only studio-quality source images are accepted as the main image. '
             'In-use / lifestyle / context shots (product on a real surface, with people, '
             'in an environment) are downgraded to gallery candidates and never auto-applied '
             'as main. Products with no studio source available go to "Needs Manual Main Image" '
             'state. Note: this controls SOURCE filtering — the FINAL output is always a '
             'transparent PNG regardless. Strongly recommended ON for clean shop grids.',
        config_parameter='laudie_image_ai.laudie_strict_white_main',
    )
    laudie_recipe_cache_enabled = fields.Boolean(
        string='Self-learning Recipe Cache', default=True,
        help='After 5 successful AI classifications on a domain, distill a CSS-selector recipe '
             'and use it for future products on that domain (free, fast). AI cost asymptotes '
             'toward zero as the catalog grows.',
        config_parameter='laudie_image_ai.laudie_recipe_cache_enabled',
    )
    laudie_brand_attribute_name = fields.Char(
        string='Brand Attribute Name', default='Brand',
        help='Name of the product attribute that holds the manufacturer / brand name. '
             'The AI pipeline reads this attribute to identify the brand. '
             'Falls back to the first word of the product name if the attribute is missing.',
        config_parameter='laudie_image_ai.laudie_brand_attribute_name',
    )
    laudie_strict_brand_url_match = fields.Boolean(
        string='Only accept images from the brand domain', default=True,
        help='Reject any candidate image whose URL host does NOT contain the brand name. '
             'Guarantees images come from the actual manufacturer source (or their CDN), '
             'not from third-party aggregators or marketplaces. Strongly recommended.',
        config_parameter='laudie_image_ai.laudie_strict_brand_url_match',
    )

    # Normalization
    laudie_target_canvas_size = fields.Integer(
        string='Target Canvas Size (px)', default=1920,
        help='Final image dimension in pixels (square). 1600 gives crisp PDP zoom while staying under Odoo 1920 max.',
        config_parameter='laudie_image_ai.laudie_target_canvas_size',
    )
    laudie_padding_percent = fields.Integer(
        string='Padding (%)', default=8,
        help='Whitespace padding around product as % of canvas. THIS is what makes products look uniformly sized in the grid.',
        config_parameter='laudie_image_ai.laudie_padding_percent',
    )
    laudie_bg_color = fields.Char(
        string='Background Color', default='#FFFFFF',
        config_parameter='laudie_image_ai.laudie_bg_color',
    )
    laudie_jpeg_quality = fields.Integer(
        string='JPEG Quality', default=92,
        config_parameter='laudie_image_ai.laudie_jpeg_quality',
    )
    laudie_output_format = fields.Selection([
        ('jpeg', 'JPEG (recommended for white-bg product shots)'),
        ('png', 'PNG'),
        ('auto', 'Auto'),
    ], string='Output Format', default='jpeg',
       config_parameter='laudie_image_ai.laudie_output_format')
    laudie_white_threshold = fields.Integer(
        string='White Threshold (0-255)', default=245,
        help='Pixels with R, G, B all >= this value count as "white" for background detection.',
        config_parameter='laudie_image_ai.laudie_white_threshold',
    )
    laudie_white_bg_min_percent = fields.Integer(
        string='White-BG Border Min %', default=85,
        help='If at least this % of border pixels are "white", image is considered to already have a white background and rembg is skipped.',
        config_parameter='laudie_image_ai.laudie_white_bg_min_percent',
    )
    laudie_force_normalize_existing = fields.Boolean(
        string='Always Normalize (even white-BG images)', default=True,
        help='Trim/center/pad even on already-white-BG images. This is what fixes inconsistent framing across the existing catalog.',
        config_parameter='laudie_image_ai.laudie_force_normalize_existing',
    )

    # Pipeline
    laudie_max_images_per_product = fields.Integer(
        string='Max Images / Product', default=4,
        config_parameter='laudie_image_ai.laudie_max_images_per_product',
    )
    laudie_min_image_width = fields.Integer(
        string='Min Image Width (px)', default=600,
        config_parameter='laudie_image_ai.laudie_min_image_width',
    )
    laudie_rembg_model = fields.Selection([
        ('u2net', 'u2net (fast, smaller)'),
        ('birefnet-general', 'birefnet-general (best quality)'),
        ('isnet-general-use', 'isnet-general-use'),
    ], string='rembg Model', default='birefnet-general',
       config_parameter='laudie_image_ai.laudie_rembg_model')
    laudie_overwrite_existing_main = fields.Boolean(
        string='Overwrite Existing Main Images', default=False,
        config_parameter='laudie_image_ai.laudie_overwrite_existing_main',
    )

    # Safety
    laudie_require_review = fields.Boolean(
        string='Require Review of Candidates', default=True,
        config_parameter='laudie_image_ai.laudie_require_review',
    )
    laudie_min_confidence_score = fields.Float(
        string='Min Confidence Score', default=0.7,
        config_parameter='laudie_image_ai.laudie_min_confidence_score',
    )
    laudie_user_agent = fields.Char(
        string='HTTP User-Agent', default='LaudieImageBot/1.0 (+https://laudie.ca/contact)',
        config_parameter='laudie_image_ai.laudie_user_agent',
    )
    laudie_request_delay_seconds = fields.Float(
        string='Per-Domain Request Delay (s)', default=2.0,
        config_parameter='laudie_image_ai.laudie_request_delay_seconds',
    )
    laudie_concurrent_workers = fields.Integer(
        string='Concurrent Workers', default=2,
        config_parameter='laudie_image_ai.laudie_concurrent_workers',
    )

    # Cost
    laudie_monthly_ai_budget_usd = fields.Float(
        string='Monthly AI Budget (USD)', default=50.0,
        config_parameter='laudie_image_ai.laudie_monthly_ai_budget_usd',
    )
    laudie_alert_email = fields.Char(
        string='Budget Alert Email',
        config_parameter='laudie_image_ai.laudie_alert_email',
    )

    # Storage
    laudie_keep_backup = fields.Boolean(
        string='Keep Original Image Backup', default=True,
        help='Saves original main image to laudie_original_main_image before normalization. Doubles storage but enables Revert.',
        config_parameter='laudie_image_ai.laudie_keep_backup',
    )

    def action_open_preview_normalization(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Preview Normalization',
            'res_model': 'laudie.preview.normalization.wizard',
            'view_mode': 'form',
            'target': 'new',
        }

    def action_pre_warm_rembg(self):
        """Trigger model download so first real run isn't slow."""
        self.ensure_one()
        model = self.env['ir.config_parameter'].sudo().get_param(
            'laudie_image_ai.laudie_rembg_model', 'birefnet-general'
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
    def get_laudie_config(self):
        """Helper used by pipeline code — returns a plain dict of typed config values."""
        get = self.env['ir.config_parameter'].sudo().get_param
        def _int(k, d):
            try: return int(get(f'laudie_image_ai.{k}', d))
            except (ValueError, TypeError): return d
        def _float(k, d):
            try: return float(get(f'laudie_image_ai.{k}', d))
            except (ValueError, TypeError): return d
        def _bool(k, d):
            v = get(f'laudie_image_ai.{k}')
            if v is None or v is False: return d
            return str(v).lower() in ('1', 'true', 't', 'yes')
        return {
            'anthropic_api_key': get('laudie_image_ai.laudie_anthropic_api_key', ''),
            'anthropic_model': get('laudie_image_ai.laudie_anthropic_model', 'claude-haiku-4-5-20251001'),
            'search_provider': get('laudie_image_ai.laudie_search_provider', 'brave'),
            'search_api_key': get('laudie_image_ai.laudie_search_api_key', ''),
            'photoroom_api_key': get('laudie_image_ai.laudie_photoroom_api_key', ''),
            'browserless_api_key': get('laudie_image_ai.laudie_browserless_api_key', ''),
            'browserless_endpoint': get('laudie_image_ai.laudie_browserless_endpoint', 'https://chrome.browserless.io'),
            'strict_white_main': _bool('laudie_strict_white_main', True),
            'recipe_cache_enabled': _bool('laudie_recipe_cache_enabled', True),
            'brand_attribute_name': get('laudie_image_ai.laudie_brand_attribute_name', 'Brand'),
            'strict_brand_url_match': _bool('laudie_strict_brand_url_match', True),
            'target_canvas_size': _int('laudie_target_canvas_size', 1920),
            'padding_percent': _int('laudie_padding_percent', 8),
            'bg_color': get('laudie_image_ai.laudie_bg_color', '#FFFFFF'),
            'jpeg_quality': _int('laudie_jpeg_quality', 92),
            'output_format': get('laudie_image_ai.laudie_output_format', 'jpeg'),
            'white_threshold': _int('laudie_white_threshold', 245),
            'white_bg_min_percent': _int('laudie_white_bg_min_percent', 85),
            'force_normalize_existing': _bool('laudie_force_normalize_existing', True),
            'max_images_per_product': _int('laudie_max_images_per_product', 4),
            'min_image_width': _int('laudie_min_image_width', 600),
            'rembg_model': get('laudie_image_ai.laudie_rembg_model', 'birefnet-general'),
            'overwrite_existing_main': _bool('laudie_overwrite_existing_main', False),
            'require_review': _bool('laudie_require_review', True),
            'min_confidence_score': _float('laudie_min_confidence_score', 0.7),
            'user_agent': get('laudie_image_ai.laudie_user_agent', 'LaudieImageBot/1.0 (+https://laudie.ca/contact)'),
            'request_delay_seconds': _float('laudie_request_delay_seconds', 2.0),
            'concurrent_workers': _int('laudie_concurrent_workers', 2),
            'monthly_ai_budget_usd': _float('laudie_monthly_ai_budget_usd', 50.0),
            'alert_email': get('laudie_image_ai.laudie_alert_email', ''),
            'keep_backup': _bool('laudie_keep_backup', True),
        }
