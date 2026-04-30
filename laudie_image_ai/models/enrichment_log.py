from odoo import fields, models


class EnrichmentLog(models.Model):
    _name = 'laudie.enrichment.log'
    _description = 'Enrichment Step Log'
    _order = 'create_date desc'

    job_id = fields.Many2one('laudie.enrichment.job', ondelete='cascade')
    product_id = fields.Many2one('product.template', ondelete='set null')
    step = fields.Selection([
        ('search', 'Search'),
        ('fetch_page', 'Fetch Page'),
        ('classify', 'AI Classify'),
        ('download_image', 'Download Image'),
        ('analyze_bg', 'Analyze Background'),
        ('rembg', 'Remove Background'),
        ('normalize', 'Normalize'),
        ('apply', 'Apply to Product'),
        ('error', 'Error'),
    ], required=True)
    level = fields.Selection([
        ('info', 'Info'),
        ('warning', 'Warning'),
        ('error', 'Error'),
    ], default='info')
    message = fields.Text()
    duration_ms = fields.Integer()
    payload = fields.Text(help='Optional JSON-stringified detail.')
