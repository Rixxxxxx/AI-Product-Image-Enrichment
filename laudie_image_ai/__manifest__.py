{
    'name': 'Laudie — AI Product Image Enrichment',
    'version': '19.0.1.0.0',
    'summary': 'AI-driven product image discovery, background handling, and uniform normalization for a clean shop grid.',
    'description': """
Laudie — AI Product Image Enrichment
====================================

AI-driven, manufacturer-agnostic image enrichment for Odoo 19 product catalogs.

Pipeline:
  1. Search the open web for the manufacturer's product page (no per-vendor scraping rules).
  2. Use Claude (Anthropic API) to read the page and classify product images by role.
  3. Detect images that already have a clean white background; skip rembg when not needed.
  4. Normalize every main image to a uniform white canvas with consistent padding so the
     shop grid looks visually coherent across hundreds of products.

Built for CloudPepper-hosted Odoo 19 Community. No queue_job dependency, ir.cron only.
""",
    'author': 'Groupe Laudie',
    'website': 'https://laudie.ca',
    'category': 'Website/Website',
    'license': 'LGPL-3',
    'depends': ['base', 'product', 'website_sale', 'mail'],
    'external_dependencies': {
        'python': [
            'requests',
            'beautifulsoup4',
            'lxml',
            'Pillow',
            'numpy',
            # rembg/onnxruntime are now OPTIONAL — used only when no Photoroom API key is configured
            'anthropic',
        ],
    },
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml',
        'views/res_config_settings_views.xml',
        'views/product_template_views.xml',
        'views/product_category_views.xml',
        'views/enrichment_job_views.xml',
        'views/candidate_views.xml',
        'views/log_views.xml',
        'views/scraping_recipe_views.xml',
        'views/menu_views.xml',
        'wizards/enrich_products_wizard_views.xml',
        'wizards/normalize_only_wizard_views.xml',
        'wizards/review_candidates_wizard_views.xml',
        'wizards/preview_normalization_wizard_views.xml',
    ],
    'application': True,
    'installable': True,
    'auto_install': False,
}
