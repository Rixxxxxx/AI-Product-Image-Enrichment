import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class NormalizeOnlyWizard(models.TransientModel):
    _name = 'aipie.normalize.only.wizard'
    _description = 'Normalize Existing Product Main Images'

    selection_mode = fields.Selection([
        ('selected', 'Selected Products'),
        ('all_with_images', 'All Products With Main Images'),
        ('not_yet_normalized', 'Not Yet Normalized'),
        ('category', 'By Category'),
    ], default='not_yet_normalized', required=True)

    category_ids = fields.Many2many('product.category')
    force_renormalize = fields.Boolean(
        string='Force Re-Normalize',
        help='Re-normalize even products already marked as normalized.',
    )

    estimated_count = fields.Integer(compute='_compute_breakdown', readonly=True)
    estimated_already_transparent = fields.Integer(
        compute='_compute_breakdown', readonly=True,
        string='Already transparent (skip BG removal)',
        help='Source images that already have a transparent background — no BG removal needed. '
             'Trim/center/pad still applies for uniform sizing.',
    )
    estimated_white_bg = fields.Integer(
        compute='_compute_breakdown', readonly=True,
        string='Studio shots (clean BG removal)',
        help='Source images on a clean white background. BG removal will produce '
             'transparent results with no halo artifacts.',
    )
    estimated_complex = fields.Integer(
        compute='_compute_breakdown', readonly=True,
        string='Complex sources (BG removal applied)',
        help='Source images with non-white / busy backgrounds. BG removal handles them, '
             'but edges may have minor artifacts depending on source quality.',
    )
    breakdown_sample_size = fields.Integer(default=20,
        help='Sample size for the source-quality estimate. Larger = more accurate, slower to compute.')

    @api.depends('selection_mode', 'category_ids', 'force_renormalize', 'breakdown_sample_size')
    def _compute_breakdown(self):
        for rec in self:
            products = rec._resolve_products()
            rec.estimated_count = len(products)

            sample = products[:max(1, rec.breakdown_sample_size)]
            counts = {'transparent': 0, 'white': 0, 'complex': 0}
            from ..services.background_analyzer import BackgroundAnalyzer
            config = self.env['res.config.settings'].sudo().get_aipie_config()
            analyzer = BackgroundAnalyzer(
                white_threshold=config['white_threshold'],
                min_white_percent=config['white_bg_min_percent'],
            )
            for p in sample:
                if not p.image_1920:
                    continue
                try:
                    raw = base64.b64decode(p.image_1920)
                    _has_white, _pct, info = analyzer.analyze(raw)
                    state = info.get('source_state', 'complex')
                    counts[state] = counts.get(state, 0) + 1
                except Exception:
                    pass
            sample_n = max(1, sum(counts.values()))
            total = rec.estimated_count
            rec.estimated_already_transparent = int(total * counts['transparent'] / sample_n)
            rec.estimated_white_bg = int(total * counts['white'] / sample_n)
            rec.estimated_complex = total - rec.estimated_already_transparent - rec.estimated_white_bg

    def _resolve_products(self):
        Product = self.env['product.template']
        domain_base = [('image_1920', '!=', False)]
        if self.selection_mode == 'selected':
            ctx_ids = self.env.context.get('active_ids') or []
            return Product.browse(ctx_ids).exists().filtered(lambda p: p.image_1920)
        if self.selection_mode == 'all_with_images':
            return Product.search(domain_base)
        if self.selection_mode == 'not_yet_normalized':
            domain = domain_base + [('aipie_main_image_normalized', '=', False)]
            return Product.search(domain)
        if self.selection_mode == 'category':
            if not self.category_ids:
                return Product.browse([])
            return Product.search(domain_base + [('categ_id', 'child_of', self.category_ids.ids)])
        return Product.browse([])

    def action_run(self):
        self.ensure_one()
        products = self._resolve_products()
        if not self.force_renormalize:
            products = products.filtered(lambda p: not p.aipie_main_image_normalized)
        if not products:
            raise UserError(_('No products matched selection.'))
        job = self.env['aipie.enrichment.job'].create({
            'name': _('Normalize %s') % fields.Datetime.now(),
            'pipeline_steps': 'normalize_only',
            'product_ids': [(6, 0, products.ids)],
            'pending_product_ids': [(6, 0, products.ids)],
            'state': 'queued',
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'aipie.enrichment.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'current',
        }
