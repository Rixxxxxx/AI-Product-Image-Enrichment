import base64
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    aipie_manufacturer = fields.Char(
        string='Manufacturer (override)',
        help='Optional manual override. If empty, the module derives the manufacturer '
             'from the "Brand" product attribute, then falls back to the first word of '
             'the product name. Only fill this in when the attribute and the name both fail.',
    )
    aipie_effective_manufacturer = fields.Char(
        compute='_compute_effective_manufacturer',
        string='Manufacturer (resolved)',
        help='Read-only: the manufacturer value the AI pipeline will actually use.',
    )

    @api.depends('aipie_manufacturer', 'name',
                 'attribute_line_ids', 'attribute_line_ids.attribute_id',
                 'attribute_line_ids.value_ids')
    def _compute_effective_manufacturer(self):
        for rec in self:
            rec.aipie_effective_manufacturer = rec._effective_manufacturer()

    def _effective_manufacturer(self):
        """Resolution order:
            1. aipie_manufacturer manual override
            2. value of the configured Brand product attribute
            3. first word of the product name (last-resort fallback)
        """
        self.ensure_one()
        if self.aipie_manufacturer:
            return self.aipie_manufacturer.strip()

        # Read configured attribute name (default 'Brand')
        attr_name = self.env['ir.config_parameter'].sudo().get_param(
            'ai_product_image_enrichment.aipie_brand_attribute_name', 'Brand'
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
    aipie_manufacturer_sku = fields.Char(
        string='Manufacturer SKU',
        help='Manufacturer SKU. Falls back to default_code if empty.',
    )
    aipie_enrichment_state = fields.Selection([
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
    aipie_enrichment_last_run = fields.Datetime(readonly=True, string='Last AI Run')
    aipie_enrichment_error = fields.Text(readonly=True)

    aipie_candidate_ids = fields.One2many(
        'aipie.product.image.candidate', 'product_id', string='Image Candidates',
    )
    aipie_candidate_count = fields.Integer(
        compute='_compute_candidate_count', string='# Candidates',
    )
    aipie_pending_candidate_count = fields.Integer(
        compute='_compute_candidate_count', string='# Pending',
    )

    aipie_main_image_normalized = fields.Boolean(
        readonly=True, string='Main Image Normalized',
    )
    aipie_main_image_already_white_bg = fields.Boolean(
        readonly=True, string='Source Was Studio-Quality',
        help='Detected: the original main image source was already a clean studio shot. '
             'Final output is always a transparent PNG either way; this just indicates the cleanest '
             'possible result was achievable for this product.',
    )
    aipie_image_count = fields.Integer(
        compute='_compute_image_count', store=True, string='Image Count',
    )
    # Binary(attachment=True) keeps bit-identical originals in the filestore (not the DB).
    # fields.Image would resize and recompress, which corrupts the backup we may need to revert to.
    aipie_original_main_image = fields.Binary(
        attachment=True, string='Original (Backup)',
        help='Backup of original main image bytes before normalization. Stored in the filestore. Used for Revert.',
    )

    aipie_normalization_signature = fields.Char(
        readonly=True, copy=False,
        help='Hash of (post-normalization image bytes + normalization settings). '
             'On re-run, if the current image and settings still match this hash, normalization is skipped.',
    )

    aipie_gallery_mode = fields.Selection([
        ('auto', 'Auto (use category default)'),
        ('yes', 'Yes — collect gallery images'),
        ('no', 'No — main image only'),
    ], default='auto', string='Collect gallery images?',
       help='Whether the AI pipeline should collect gallery images (angles, detail '
            'shots, in-use, lifestyle, accessory) in addition to the main image. '
            'Auto uses the category default. Use Yes/No to override per product.')

    aipie_gallery_enabled = fields.Boolean(
        compute='_compute_gallery_enabled',
        string='Gallery enabled (resolved)',
        help='Read-only: whether non-main candidates will actually be collected for this product.',
    )

    @api.depends('aipie_gallery_mode', 'categ_id', 'categ_id.aipie_enable_gallery')
    def _compute_gallery_enabled(self):
        for rec in self:
            if rec.aipie_gallery_mode == 'yes':
                rec.aipie_gallery_enabled = True
            elif rec.aipie_gallery_mode == 'no':
                rec.aipie_gallery_enabled = False
            else:
                rec.aipie_gallery_enabled = bool(rec.categ_id and rec.categ_id.aipie_enable_gallery)

    @api.depends('aipie_candidate_ids', 'aipie_candidate_ids.state')
    def _compute_candidate_count(self):
        for rec in self:
            rec.aipie_candidate_count = len(rec.aipie_candidate_ids)
            rec.aipie_pending_candidate_count = len(
                rec.aipie_candidate_ids.filtered(lambda c: c.state == 'pending')
            )

    @api.depends('image_1920', 'product_template_image_ids')
    def _compute_image_count(self):
        for rec in self:
            count = 1 if rec.image_1920 else 0
            count += len(rec.product_template_image_ids)
            rec.aipie_image_count = count

    # ---------- Actions ----------

    def action_aipie_find_images(self):
        """Queue an enrichment job (discover only) for these products."""
        self.ensure_one()
        job = self.env['aipie.enrichment.job'].create({
            'name': _('Discover: %s') % self.display_name,
            'pipeline_steps': 'discover_only',
            'product_ids': [(6, 0, self.ids)],
            'pending_product_ids': [(6, 0, self.ids)],
            'state': 'queued',
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'aipie.enrichment.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'current',
        }

    def action_aipie_review_candidates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Image Candidates'),
            'res_model': 'aipie.product.image.candidate',
            'view_mode': 'kanban,list,form',
            'domain': [('product_id', '=', self.id)],
            'context': {'default_product_id': self.id},
        }

    def action_aipie_normalize_main(self):
        """Run normalization synchronously on the current main image."""
        self.ensure_one()
        if not self.image_1920:
            raise UserError(_('Product has no main image to normalize.'))
        from ..services.enrichment_pipeline import normalize_existing_main_image
        config = self.env['res.config.settings'].sudo().get_aipie_config()
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

    def action_aipie_revert_main(self):
        self.ensure_one()
        if not self.aipie_original_main_image:
            raise UserError(_('No backup available for this product.'))
        self.image_1920 = self.aipie_original_main_image
        self.aipie_main_image_normalized = False
        self.aipie_main_image_already_white_bg = False
        return True

    def action_aipie_skip(self):
        for rec in self:
            rec.aipie_enrichment_state = 'skipped'
        return True
