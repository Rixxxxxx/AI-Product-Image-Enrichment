import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


ROLE_PRIORITY = {
    'main': 0, 'angle': 1, 'detail': 2, 'in_use': 3,
    'lifestyle': 4, 'accessory': 5, 'uncertain': 9,
}


class ProductImageCandidate(models.Model):
    _name = 'aipie.product.image.candidate'
    _description = 'AI-Discovered Product Image Candidate'
    _order = 'product_id, role_priority, confidence desc'

    product_id = fields.Many2one(
        'product.template', required=True, ondelete='cascade', string='Product',
    )
    job_id = fields.Many2one('aipie.enrichment.job', ondelete='set null')

    source_url = fields.Char(required=True, string='Image URL')
    source_page_url = fields.Char(string='Source Page')

    role = fields.Selection([
        ('main', 'Main / Hero Shot'),
        ('angle', 'Alternate Angle'),
        ('detail', 'Detail / Close-up'),
        ('in_use', 'In Use / In Context'),
        ('lifestyle', 'Lifestyle / Marketing'),
        ('accessory', 'Accessory / Bundle'),
        ('uncertain', 'Uncertain'),
    ], default='uncertain')
    role_priority = fields.Integer(compute='_compute_role_priority', store=True)
    confidence = fields.Float()
    ai_reasoning = fields.Text()

    image_data = fields.Binary(attachment=True, string='Original')
    image_width = fields.Integer()
    image_height = fields.Integer()
    image_filesize_kb = fields.Integer()
    image_mimetype = fields.Char()

    has_white_background = fields.Boolean(readonly=True)
    background_white_percent = fields.Float(readonly=True)

    preview_normalized_image = fields.Binary(attachment=True, string='Normalized Preview')

    state = fields.Selection([
        ('pending', 'Pending Review'),
        ('approved', 'Approved (Awaiting Apply)'),
        ('applied', 'Applied'),
        ('rejected', 'Rejected'),
        ('failed', 'Download/Process Failed'),
    ], default='pending', tracking=True)

    rejection_reason = fields.Char()

    @api.depends('role')
    def _compute_role_priority(self):
        for rec in self:
            rec.role_priority = ROLE_PRIORITY.get(rec.role or 'uncertain', 9)

    # ---------- Actions ----------

    def action_approve(self):
        for rec in self:
            rec.state = 'approved'
        return True

    def action_reject(self):
        for rec in self:
            rec.state = 'rejected'
        return True

    def action_apply_to_product(self):
        from ..services.enrichment_pipeline import apply_candidate_to_product
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        for rec in self:
            try:
                apply_candidate_to_product(rec, config, self.env)
            except Exception as e:
                _logger.exception('Apply failed for candidate %s', rec.id)
                rec.state = 'failed'
                rec.rejection_reason = str(e)[:255]
        return True

    def action_regenerate_preview(self):
        from ..services.image_normalizer import ImageNormalizer
        from ..services.background_analyzer import BackgroundAnalyzer
        from ..services.photoroom import BackgroundRemovalDispatcher
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        analyzer = BackgroundAnalyzer(
            white_threshold=config['white_threshold'],
            min_white_percent=config['white_bg_min_percent'],
        )
        normalizer = ImageNormalizer()
        dispatcher = BackgroundRemovalDispatcher(
            photoroom_api_key=config.get('photoroom_api_key', ''),
            rembg_model=config['rembg_model'],
        )
        for rec in self:
            if not rec.image_data:
                continue
            raw = base64.b64decode(rec.image_data)
            has_white, white_pct, _info = analyzer.analyze(raw)
            rec.has_white_background = has_white
            rec.background_white_percent = white_pct
            try:
                if rec.role == 'main':
                    try:
                        bg_removed = dispatcher.remove(raw)
                    except Exception as bg_err:
                        _logger.info('Preview BG removal failed: %s', bg_err)
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
                rec.preview_normalized_image = base64.b64encode(preview)
            except Exception as e:
                _logger.exception('Preview generation failed')
                rec.rejection_reason = f'Preview failed: {e}'[:255]
        return True
