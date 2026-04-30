{
    'name': 'AI Product Image Enrichment',
    'version': '19.0.1.0.0',
    'summary': 'Find, background-remove, and uniformly normalize product images across multi-brand catalogs with AI.',
    'description': """
AI Product Image Enrichment
===========================

The complete catalog imagery platform for multi-brand Odoo shops.

Three things at once, in one module:

1. Discovers professional product images by reading manufacturer websites
   with Claude AI — no per-vendor scraping rules to maintain.
2. Removes backgrounds and uniformly normalizes every main image so the
   shop grid looks like one studio took every photograph.
3. Keeps lifestyle and in-use shots as gallery images on the product
   detail page — where context belongs, not on the thumbnail.

Background removal supports either Photoroom API (zero infrastructure) or
self-hosted rembg (free, requires Python deps in venv).

Built for distributors, resellers, B2B catalogs, and any Odoo shop carrying
products from many different manufacturers.

See README for installation, setup, recommended workflow, and troubleshooting.
""",
    'author': 'Your Company',
    'maintainer': 'Your Company',
    'website': 'https://your-website.example',
    'support': 'support@your-website.example',
    'category': 'Website/Website',
    'license': 'OPL-1',
    'depends': ['base', 'product', 'website_sale', 'mail'],
    'external_dependencies': {
        'python': [
            'requests',
            'beautifulsoup4',
            'lxml',
            'Pillow',
            'numpy',
            'anthropic',
            # rembg/onnxruntime are OPTIONAL — only needed if you skip Photoroom
            # and use the local background-removal path instead.
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
    'images': ['static/description/banner.png'],
    'application': True,
    'installable': True,
    'auto_install': False,
    'price': 149.00,
    'currency': 'USD',
}
