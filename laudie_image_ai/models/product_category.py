from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    laudie_enable_gallery = fields.Boolean(
        string='Enable AI gallery images by default',
        default=False,
        help='If on, AI discovery will collect non-main gallery images (angles, '
             'detail shots, in-use, lifestyle, accessory) for products in this '
             'category. Leave off for consumables (chemicals, paper products, '
             'garbage bags, mop heads) where a gallery adds noise without value. '
             'Turn on for durable equipment (machines, carts, branded tools).',
    )
