from odoo import _, api, fields, models
from odoo.exceptions import UserError


class EnrichProductsWizard(models.TransientModel):
    _name = 'aipie.enrich.products.wizard'
    _description = 'Enrich Products with AI'

    selection_mode = fields.Selection([
        ('selected', 'Selected Products (from list)'),
        ('missing_images', 'Products Missing a Main Image'),
        ('single_image_only', 'Products With Only 1 Image'),
        ('category', 'By Category'),
        ('all', 'All Published Products'),
    ], default='selected', required=True)

    category_ids = fields.Many2many('product.category', string='Categories')
    product_ids = fields.Many2many('product.template')

    pipeline_steps = fields.Selection([
        ('discover_only', 'Discover Candidates Only'),
        ('discover_apply', 'Discover + Auto-Apply (high confidence)'),
        ('normalize_only', 'Normalize Existing Main Images Only'),
        ('full', 'Full Pipeline'),
    ], default='discover_only', required=True)

    discover_main_image = fields.Boolean(
        default=False, string='Also discover main image',
        help='Default OFF: discovery finds gallery images only (angles, details, in-use, '
             'lifestyle, accessory). Use the Normalize Main Images wizard separately for '
             'existing main images. Turn ON when you want the AI to also propose a main '
             'image candidate — useful for products that do not yet have one. The found '
             'image will be background-removed and normalized regardless of source quality.',
    )
    estimated_cost_low = fields.Float(compute='_compute_estimated_cost', readonly=True,
        string='Estimated Cost (best case)',
        help='Best case: ~$0.01/product when the first candidate page is the right one '
             '(typical for products with a clean manufacturer SKU and live brand sitemap).')
    estimated_cost_high = fields.Float(compute='_compute_estimated_cost', readonly=True,
        string='Estimated Cost (worst case)',
        help='Worst case: ~$0.05/product if all 5 candidate pages need to be classified '
             'before a match is found (or none match).')
    estimated_cost_range = fields.Char(compute='_compute_estimated_cost', readonly=True,
        string='Estimated Cost',
        help='Range based on Claude Haiku classification calls. Cost scales with '
             'pages-classified per product, NOT images found. One call may return many images.')
    estimated_count = fields.Integer(compute='_compute_estimated_cost', readonly=True)
    cost_cap_usd = fields.Float(
        string='Cost Cap (USD)', compute='_compute_estimated_cost',
        readonly=False, store=False,
        help='Hard pause threshold for THIS job. The cron pauses the job once this '
             'job\'s total Anthropic spend reaches this cap. Pre-filled to 1.5× the '
             'worst-case estimate as a safety margin — adjust if you expect higher cost.')

    @api.depends('selection_mode', 'category_ids', 'product_ids', 'pipeline_steps')
    def _compute_estimated_cost(self):
        for rec in self:
            products = rec._resolve_products()
            n = len(products)
            rec.estimated_count = n
            if rec.pipeline_steps == 'normalize_only':
                rec.estimated_cost_low = 0.0
                rec.estimated_cost_high = 0.0
                rec.estimated_cost_range = '$0.00 (no AI calls)'
            else:
                # ~$0.01 per Claude Haiku classification call. Best case: 1 call per
                # product (sitemap hit). Worst case: 5 (page_urls cap).
                rec.estimated_cost_low = 0.01 * n
                rec.estimated_cost_high = 0.05 * n
                rec.estimated_cost_range = '$%.2f – $%.2f' % (rec.estimated_cost_low, rec.estimated_cost_high)
            # Default cap = 1.5× worst case, with a $1 floor for tiny jobs.
            # Operator can override before clicking Run.
            rec.cost_cap_usd = max(1.0, round(rec.estimated_cost_high * 1.5, 2))

    def _resolve_products(self):
        Product = self.env['product.template']
        if self.selection_mode == 'selected':
            ctx_ids = self.env.context.get('active_ids') or []
            return Product.browse(ctx_ids).exists() | self.product_ids
        if self.selection_mode == 'missing_images':
            return Product.search([('image_1920', '=', False), ('is_published', '=', True)])
        if self.selection_mode == 'single_image_only':
            # Heuristic: products whose aipie_image_count is 1 (computed)
            # Compute is non-stored via dependency, so fall back to a search
            all_pub = Product.search([('is_published', '=', True), ('image_1920', '!=', False)])
            return all_pub.filtered(lambda p: p.aipie_image_count <= 1)
        if self.selection_mode == 'category':
            if not self.category_ids:
                return Product.browse([])
            return Product.search([('categ_id', 'child_of', self.category_ids.ids)])
        return Product.search([('is_published', '=', True)])

    def action_run(self):
        self.ensure_one()
        products = self._resolve_products()
        if not products:
            raise UserError(_('No products matched selection.'))
        job = self.env['aipie.enrichment.job'].create({
            'name': _('Wizard %s') % fields.Datetime.now(),
            'pipeline_steps': self.pipeline_steps,
            'product_ids': [(6, 0, products.ids)],
            'pending_product_ids': [(6, 0, products.ids)],
            'state': 'queued',
            'estimated_cost_usd': self.estimated_cost_high,
            'cost_cap_usd': self.cost_cap_usd,
            'discover_main_image': self.discover_main_image,
        })
        # Mark products as queued so the operator doesn't see stale 'no_results'
        # / 'enriched' / 'error' state from a previous run while waiting for cron.
        if self.pipeline_steps != 'normalize_only':
            products.write({'aipie_enrichment_state': 'queued'})
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'aipie.enrichment.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'current',
        }
