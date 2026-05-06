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

    # Surface the product's current main image directly on the candidate so
    # the operator can sanity-check "is this candidate even the right product"
    # at a glance, without bouncing to the product form.
    product_current_main_image = fields.Image(
        related='product_id.image_1920', readonly=True, string='Product Current Main',
    )

    state = fields.Selection([
        ('pending', 'Pending Review'),
        ('applied', 'Approved'),
        ('rejected', 'Rejected'),
        ('failed', 'Failed'),
    ], default='pending', tracking=True)

    rejection_reason = fields.Char()

    @api.depends('role')
    def _compute_role_priority(self):
        for rec in self:
            rec.role_priority = ROLE_PRIORITY.get(rec.role or 'uncertain', 9)

    # ---------- Actions ----------

    def action_approve(self):
        """Approve = apply. Approving an image always means using it on the
        product; the previous separate "Approved (Awaiting Apply)" state was
        redundant friction."""
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

    # Kept as an alias for any external triggers; the new canonical action is
    # action_approve.
    def action_apply_to_product(self):
        return self.action_approve()

    def action_reject(self):
        for rec in self:
            rec.state = 'rejected'
        return True

    def action_set_as_main(self):
        """Promote this candidate to role='main' for its product. Any existing
        candidate on the same product currently flagged 'main' is demoted to
        'angle' so there's exactly one main per product."""
        for rec in self:
            other_mains = self.search([
                ('product_id', '=', rec.product_id.id),
                ('id', '!=', rec.id),
                ('role', '=', 'main'),
            ])
            if other_mains:
                other_mains.write({'role': 'angle'})
            rec.role = 'main'
        return True

    def action_approve_all_for_product(self):
        """Bulk: approve all PENDING candidates for the same product(s) as the
        current record(s). Useful when the operator wants to take everything
        the AI found without clicking each card."""
        product_ids = self.mapped('product_id').ids
        if not product_ids:
            return True
        siblings = self.search([
            ('product_id', 'in', product_ids),
            ('state', '=', 'pending'),
        ])
        return siblings.action_approve()

    def action_reject_all_for_product(self):
        product_ids = self.mapped('product_id').ids
        if not product_ids:
            return True
        siblings = self.search([
            ('product_id', 'in', product_ids),
            ('state', '=', 'pending'),
        ])
        return siblings.action_reject()

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
