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

    dry_run = fields.Boolean(default=False)
    estimated_cost = fields.Float(compute='_compute_estimated_cost', readonly=True)
    estimated_count = fields.Integer(compute='_compute_estimated_cost', readonly=True)

    @api.depends('selection_mode', 'category_ids', 'product_ids', 'pipeline_steps')
    def _compute_estimated_cost(self):
        for rec in self:
            products = rec._resolve_products()
            rec.estimated_count = len(products)
            # ~$0.01/product for discovery with Haiku, $0 for normalize_only
            per = 0.0 if rec.pipeline_steps == 'normalize_only' else 0.01
            rec.estimated_cost = per * len(products)

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
        if self.dry_run:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Dry run'),
                    'message': _('%d products would be enriched (~$%.2f).')
                               % (len(products), self.estimated_cost),
                    'type': 'info',
                },
            }
        job = self.env['aipie.enrichment.job'].create({
            'name': _('Wizard %s') % fields.Datetime.now(),
            'pipeline_steps': self.pipeline_steps,
            'product_ids': [(6, 0, products.ids)],
            'pending_product_ids': [(6, 0, products.ids)],
            'state': 'queued',
            'estimated_cost_usd': self.estimated_cost,
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'aipie.enrichment.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'current',
        }
