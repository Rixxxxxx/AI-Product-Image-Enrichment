from odoo import api, fields, models


# Approximate USD pricing per million tokens. Verify against
# https://docs.claude.com/en/docs/about-claude/pricing periodically.
PRICING = {
    'claude-haiku-4-5-20251001': {'input': 1.0, 'output': 5.0},
    'claude-sonnet-4-6':         {'input': 3.0, 'output': 15.0},
    'claude-opus-4-7':           {'input': 15.0, 'output': 75.0},
}


class AIUsageLog(models.Model):
    _name = 'laudie.ai.usage.log'
    _description = 'Anthropic API Usage Log'
    _order = 'create_date desc'

    create_date = fields.Datetime(readonly=True)
    model = fields.Char()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cost_usd = fields.Float(digits=(12, 6))
    product_id = fields.Many2one('product.template', ondelete='set null')
    job_id = fields.Many2one('laudie.enrichment.job', ondelete='set null')
    operation = fields.Char()
    duration_ms = fields.Integer()
    error = fields.Text()

    @api.model
    def log_usage(self, model, input_tokens, output_tokens,
                  product=None, job=None, operation=None, duration_ms=0, error=None):
        cost = self._estimate_cost(model, input_tokens, output_tokens)
        return self.create({
            'model': model,
            'input_tokens': input_tokens or 0,
            'output_tokens': output_tokens or 0,
            'cost_usd': cost,
            'product_id': product.id if product else False,
            'job_id': job.id if job else False,
            'operation': operation,
            'duration_ms': duration_ms,
            'error': error,
        })

    @api.model
    def _estimate_cost(self, model, in_tok, out_tok):
        p = PRICING.get(model, {'input': 1.0, 'output': 5.0})
        return (in_tok or 0) * p['input'] / 1_000_000 + (out_tok or 0) * p['output'] / 1_000_000

    @api.model
    def _month_to_date_cost(self):
        self.env.cr.execute("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM laudie_ai_usage_log
            WHERE date_trunc('month', create_date) = date_trunc('month', now())
        """)
        return float(self.env.cr.fetchone()[0] or 0.0)
