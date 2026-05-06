from odoo import api, fields, models


# Approximate USD pricing per million tokens. Verify against
# https://docs.claude.com/en/docs/about-claude/pricing periodically.
PRICING = {
    'claude-haiku-4-5-20251001': {'input': 1.0, 'output': 5.0},
    'claude-sonnet-4-6':         {'input': 3.0, 'output': 15.0},
    'claude-opus-4-7':           {'input': 15.0, 'output': 75.0},
}


class AIUsageLog(models.Model):
    _name = 'aipie.ai.usage.log'
    _description = 'Anthropic API Usage Log'
    _order = 'create_date desc'

    create_date = fields.Datetime(readonly=True)
    model = fields.Char()
    input_tokens = fields.Integer()
    output_tokens = fields.Integer()
    cost_usd = fields.Float(digits=(12, 6))
    product_id = fields.Many2one('product.template', ondelete='set null')
    job_id = fields.Many2one('aipie.enrichment.job', ondelete='set null')
    operation = fields.Char()
    duration_ms = fields.Integer()
    error = fields.Text()

    @api.model
    def log_usage(self, model, input_tokens, output_tokens,
                  product=None, job=None, operation=None, duration_ms=0, error=None):
        """Persist a Claude API call's cost via SEPARATE cursor.

        Critical: the cost-cap and monthly-budget circuit breakers query this
        table to decide whether to pause jobs. If we wrote on the main cursor
        and the cron tick rolled back, the call's cost would vanish and the
        breakers would think no money had been spent — letting the same
        Claude call replay forever. Writing through a fresh cursor guarantees
        the cost record persists no matter what happens to the surrounding
        transaction.
        """
        cost = self._estimate_cost(model, input_tokens, output_tokens)
        vals = {
            'model': model,
            'input_tokens': input_tokens or 0,
            'output_tokens': output_tokens or 0,
            'cost_usd': cost,
            'product_id': product.id if product else False,
            'job_id': job.id if job else False,
            'operation': operation,
            'duration_ms': duration_ms,
            'error': error,
        }
        try:
            with self.env.registry.cursor() as side_cr:
                side_env = self.env(cr=side_cr)
                side_env['aipie.ai.usage.log'].sudo().create(vals)
        except Exception as e:
            # Last-resort fallback: write on main cursor. Better to risk losing
            # the entry than to crash the pipeline.
            import logging
            logging.getLogger(__name__).warning('Side-cursor usage log failed (%s); falling back to main', e)
            try:
                self.create(vals)
            except Exception:
                pass

    @api.model
    def _estimate_cost(self, model, in_tok, out_tok):
        p = PRICING.get(model, {'input': 1.0, 'output': 5.0})
        return (in_tok or 0) * p['input'] / 1_000_000 + (out_tok or 0) * p['output'] / 1_000_000

    @api.model
    def _month_to_date_cost(self):
        self.env.cr.execute("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM aipie_ai_usage_log
            WHERE date_trunc('month', create_date) = date_trunc('month', now())
        """)
        return float(self.env.cr.fetchone()[0] or 0.0)

    @api.model
    def _last_hour_cost(self):
        """Sum of cost_usd over the trailing 60 minutes. Used by the hard
        per-call kill-switch independent of any job/cron state.
        """
        self.env.cr.execute("""
            SELECT COALESCE(SUM(cost_usd), 0)
            FROM aipie_ai_usage_log
            WHERE create_date > now() - interval '1 hour'
        """)
        return float(self.env.cr.fetchone()[0] or 0.0)
