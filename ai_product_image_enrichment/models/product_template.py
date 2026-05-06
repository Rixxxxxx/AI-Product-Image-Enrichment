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
    aipie_manufacturer_model = fields.Char(
        string='Manufacturer Model (override)',
        help='The manufacturer\'s public model name (e.g. "AV4X", "TBL1620", "SC500-53B"). '
             'Optional — auto-extracted from the product name when possible. Only fill in '
             'when the model isn\'t in the name. NOT to be confused with default_code, which '
             'is your internal stock reference.',
    )
    aipie_effective_model = fields.Char(
        compute='_compute_effective_model',
        string='Manufacturer Model (resolved)',
        help='Read-only: the model identifier the AI pipeline will actually use. '
             'Resolution: manual override → tokens auto-extracted from product name → '
             'default_code as last resort. Empty = product will be skipped by AI enrichment.',
    )

    @api.depends('aipie_manufacturer_model', 'name', 'default_code')
    def _compute_effective_model(self):
        from ..services.enrichment_pipeline import _extract_likely_models
        for rec in self:
            models = _extract_likely_models(rec)
            rec.aipie_effective_model = ', '.join(models) if models else ''
    aipie_enrichment_state = fields.Selection([
        ('not_enriched', 'Not Enriched'),
        ('queued', 'Queued for Processing'),
        ('searching', 'Processing'),
        ('candidates_found', 'Candidates Awaiting Review'),
        ('enriched', 'Enriched'),
        ('no_results', 'No Results Found'),
        ('needs_manual_main', 'Needs Manual Main Image'),
        ('skipped_no_sku', 'Skipped — No Model Number'),
        ('error', 'Error'),
        ('skipped', 'Manually Skipped'),
    ], default='not_enriched', readonly=True, tracking=True, string='AI Enrichment Status',
       help='"Skipped — No Model Number" = product has no manufacturer model identifier and '
            'no model-number-like token in its name; AI search would return generic results. '
            'Populate aipie_manufacturer_model with the manufacturer\'s public model name to '
            'enable enrichment. '
            '"Needs Manual Main Image" = AI found gallery shots but no studio-quality main.')

    aipie_enrichment_outcome = fields.Selection([
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('no_results', 'No Results'),
        ('failure', 'Failure'),
        ('skipped', 'Skipped'),
    ], compute='_compute_enrichment_outcome', store=True,
       string='Outcome',
       help='High-level outcome category derived from the granular status. '
            'Use to filter / group products by enrichment result at a glance.')

    @api.depends('aipie_enrichment_state')
    def _compute_enrichment_outcome(self):
        mapping = {
            'not_enriched': 'pending',
            'queued': 'pending',
            'searching': 'pending',
            'candidates_found': 'success',
            'enriched': 'success',
            'no_results': 'no_results',
            'needs_manual_main': 'no_results',
            'skipped_no_sku': 'no_results',
            'error': 'failure',
            'skipped': 'skipped',
        }
        for rec in self:
            rec.aipie_enrichment_outcome = mapping.get(rec.aipie_enrichment_state, 'pending')
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

    aipie_ai_search_query = fields.Char(
        readonly=True, copy=False,
        string='AI Search Query',
        help='AI-optimized web-search query for image discovery. Generated by the '
             '"Optimize Search Queries" wizard, which sends a batch of product names '
             'to Claude and asks it to identify the manufacturer\'s public model and '
             'build the best search query. Stored permanently. Empty = pipeline falls '
             'back to regex-extracted query.',
    )

    aipie_normalization_signature = fields.Char(
        readonly=True, copy=False,
        help='Hash of (post-normalization image bytes + normalization settings). '
             'On re-run, if the current image and settings still match this hash, normalization is skipped.',
    )

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
        """Open the Enrich Products wizard pre-selected on this product.

        Same UX as the list-view 'Action → Enrich Products (AI)' bulk action so
        the operator can choose the toggle / pipeline_steps before the job is
        created. The wizard reads active_ids from context to pre-fill the
        product selection.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Enrich Products (AI)'),
            'res_model': 'aipie.enrich.products.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'product.template',
                'active_ids': self.ids,
                'active_id': self.id,
                'default_selection_mode': 'selected',
            },
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
