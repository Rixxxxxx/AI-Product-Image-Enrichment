import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    laudie_manufacturer = fields.Char(
        string='Manufacturer (override)',
        help='Optional manual override. If empty, the module derives the manufacturer '
             'from the "Brand" product attribute, then falls back to the first word of '
             'the product name. Only fill this in when the attribute and the name both fail.',
    )
    laudie_effective_manufacturer = fields.Char(
        compute='_compute_effective_manufacturer',
        string='Manufacturer (resolved)',
        help='Read-only: the manufacturer value the AI pipeline will actually use.',
    )

    @api.depends('laudie_manufacturer', 'name',
                 'attribute_line_ids', 'attribute_line_ids.attribute_id',
                 'attribute_line_ids.value_ids')
    def _compute_effective_manufacturer(self):
        for rec in self:
            rec.laudie_effective_manufacturer = rec._effective_manufacturer()

    def _effective_manufacturer(self):
        """Resolution order:
            1. laudie_manufacturer manual override
            2. value of the configured Brand product attribute
            3. first word of the product name (last-resort fallback)
        """
        self.ensure_one()
        if self.laudie_manufacturer:
            return self.laudie_manufacturer.strip()

        # Read configured attribute name (default 'Brand')
        attr_name = self.env['ir.config_parameter'].sudo().get_param(
            'laudie_image_ai.laudie_brand_attribute_name', 'Brand'
        )
        if attr_name:
            attr_name_norm = attr_name.strip().lower()
            for line in self.attribute_line_ids:
                if (line.attribute_id.name or '').strip().lower() == attr_name_norm:
                    if line.value_ids:
                        return (line.value_ids[0].name or '').strip()

        # Fallback: first word of the name
        name = (self.name or '').strip()
        if name:
            first = name.split(None, 1)[0]
            return first
        return ''
    laudie_manufacturer_sku = fields.Char(
        string='Manufacturer SKU',
        help='Manufacturer SKU. Falls back to default_code if empty.',
    )
    laudie_enrichment_state = fields.Selection([
        ('not_enriched', 'Not Enriched'),
        ('searching', 'Searching'),
        ('candidates_found', 'Candidates Awaiting Review'),
        ('enriched', 'Enriched'),
        ('no_results', 'No Results Found'),
        ('needs_manual_main', 'Needs Manual Main Image'),
        ('error', 'Error'),
        ('skipped', 'Manually Skipped'),
    ], default='not_enriched', readonly=True, tracking=True, string='AI Enrichment Status',
       help='"Needs Manual Main Image" = AI found gallery shots but no studio-quality main; '
            'upload a studio shot manually before publishing.')
    laudie_enrichment_last_run = fields.Datetime(readonly=True, string='Last AI Run')
    laudie_enrichment_error = fields.Text(readonly=True)

    laudie_candidate_ids = fields.One2many(
        'laudie.product.image.candidate', 'product_id', string='Image Candidates',
    )
    laudie_candidate_count = fields.Integer(
        compute='_compute_candidate_count', string='# Candidates',
    )
    laudie_pending_candidate_count = fields.Integer(
        compute='_compute_candidate_count', string='# Pending',
    )

    laudie_main_image_normalized = fields.Boolean(
        readonly=True, string='Main Image Normalized',
    )
    laudie_main_image_already_white_bg = fields.Boolean(
        readonly=True, string='Main Already White-BG',
        help='Detected: original main image already had a white background, only trim/center/pad applied.',
    )
    laudie_image_count = fields.Integer(
        compute='_compute_image_count', store=True, string='Image Count',
    )
    # Binary(attachment=True) keeps bit-identical originals in the filestore (not the DB).
    # fields.Image would resize and recompress, which corrupts the backup we may need to revert to.
    laudie_original_main_image = fields.Binary(
        attachment=True, string='Original (Backup)',
        help='Backup of original main image bytes before normalization. Stored in the filestore. Used for Revert.',
    )

    laudie_normalization_signature = fields.Char(
        readonly=True, copy=False,
        help='Hash of (post-normalization image bytes + normalization settings). '
             'On re-run, if the current image and settings still match this hash, normalization is skipped.',
    )

    laudie_gallery_mode = fields.Selection([
        ('auto', 'Auto (use category default)'),
        ('yes', 'Yes — collect gallery images'),
        ('no', 'No — main image only'),
    ], default='auto', string='Collect gallery images?',
       help='Whether the AI pipeline should collect gallery images (angles, detail '
            'shots, in-use, lifestyle, accessory) in addition to the main image. '
            'Auto uses the category default. Use Yes/No to override per product.')

    laudie_gallery_enabled = fields.Boolean(
        compute='_compute_gallery_enabled',
        string='Gallery enabled (resolved)',
        help='Read-only: whether non-main candidates will actually be collected for this product.',
    )

    @api.depends('laudie_gallery_mode', 'categ_id', 'categ_id.laudie_enable_gallery')
    def _compute_gallery_enabled(self):
        for rec in self:
            if rec.laudie_gallery_mode == 'yes':
                rec.laudie_gallery_enabled = True
            elif rec.laudie_gallery_mode == 'no':
                rec.laudie_gallery_enabled = False
            else:
                rec.laudie_gallery_enabled = bool(rec.categ_id and rec.categ_id.laudie_enable_gallery)

    @api.depends('laudie_candidate_ids', 'laudie_candidate_ids.state')
    def _compute_candidate_count(self):
        for rec in self:
            rec.laudie_candidate_count = len(rec.laudie_candidate_ids)
            rec.laudie_pending_candidate_count = len(
                rec.laudie_candidate_ids.filtered(lambda c: c.state == 'pending')
            )

    @api.depends('image_1920', 'product_template_image_ids')
    def _compute_image_count(self):
        for rec in self:
            count = 1 if rec.image_1920 else 0
            count += len(rec.product_template_image_ids)
            rec.laudie_image_count = count

    # ---------- Actions ----------

    def action_laudie_find_images(self):
        """Queue an enrichment job (discover only) for these products."""
        self.ensure_one()
        job = self.env['laudie.enrichment.job'].create({
            'name': _('Discover: %s') % self.display_name,
            'pipeline_steps': 'discover_only',
            'product_ids': [(6, 0, self.ids)],
            'pending_product_ids': [(6, 0, self.ids)],
            'state': 'queued',
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'laudie.enrichment.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_laudie_review_candidates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Image Candidates'),
            'res_model': 'laudie.product.image.candidate',
            'view_mode': 'kanban,list,form',
            'domain': [('product_id', '=', self.id)],
            'context': {'default_product_id': self.id},
        }

    def action_laudie_normalize_main(self):
        """Run normalization synchronously on the current main image."""
        self.ensure_one()
        if not self.image_1920:
            raise UserError(_('Product has no main image to normalize.'))
        from ..services.enrichment_pipeline import normalize_existing_main_image
        config = self.env['res.config.settings'].sudo().get_laudie_config()
        normalize_existing_main_image(self, config, self.env)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Normalized'),
                'message': _('Main image normalized for %s') % self.display_name,
                'type': 'success',
            },
        }

    def action_laudie_revert_main(self):
        self.ensure_one()
        if not self.laudie_original_main_image:
            raise UserError(_('No backup available for this product.'))
        self.image_1920 = self.laudie_original_main_image
        self.laudie_main_image_normalized = False
        self.laudie_main_image_already_white_bg = False
        return True

    def action_laudie_skip(self):
        for rec in self:
            rec.laudie_enrichment_state = 'skipped'
        return True
