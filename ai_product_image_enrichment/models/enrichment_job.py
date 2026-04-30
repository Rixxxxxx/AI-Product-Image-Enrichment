import logging
import traceback

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Chunk size per cron tick. CloudPepper-friendly: short bursts under 60s.
CRON_CHUNK_SIZE = 5
ADVISORY_LOCK_KEY = 9173401  # arbitrary 32-bit int unique to this module


class EnrichmentJob(models.Model):
    _name = 'aipie.enrichment.job'
    _description = 'Image Enrichment Batch Job'
    _inherit = ['mail.thread']
    _order = 'create_date desc'

    name = fields.Char(default=lambda s: _('Enrichment %s') % fields.Datetime.now(), tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('queued', 'Queued'),
        ('running', 'Running'),
        ('paused', 'Paused (Budget/Manual)'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ], default='draft', tracking=True)

    pipeline_steps = fields.Selection([
        ('discover_only', 'Discover Candidates Only (Manual Review)'),
        ('discover_apply', 'Discover + Auto-Apply High Confidence'),
        ('normalize_only', 'Normalize Existing Main Images Only'),
        ('full', 'Full Pipeline'),
    ], required=True, default='discover_only')

    product_ids = fields.Many2many('product.template')
    pending_product_ids = fields.Many2many(
        'product.template', 'aipie_job_pending_rel', 'job_id', 'product_id',
        copy=False,
    )
    total_count = fields.Integer(compute='_compute_counts')
    processed_count = fields.Integer(readonly=True)
    success_count = fields.Integer(readonly=True)
    failure_count = fields.Integer(readonly=True)

    images_with_white_bg_count = fields.Integer(readonly=True)
    images_required_rembg_count = fields.Integer(readonly=True)

    estimated_cost_usd = fields.Float(readonly=True)
    actual_cost_usd = fields.Float(readonly=True)

    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)

    log_ids = fields.One2many('aipie.enrichment.log', 'job_id')
    candidate_ids = fields.One2many('aipie.product.image.candidate', 'job_id')

    dry_run = fields.Boolean(default=False)

    @api.depends('product_ids')
    def _compute_counts(self):
        for rec in self:
            rec.total_count = len(rec.product_ids)

    # ---------- Lifecycle ----------

    def action_queue(self):
        for rec in self:
            if rec.state in ('draft', 'paused'):
                rec.write({
                    'state': 'queued',
                    'pending_product_ids': [(6, 0, rec.product_ids.ids)],
                })
        return True

    def action_pause(self):
        for rec in self:
            if rec.state in ('queued', 'running'):
                rec.state = 'paused'

    def action_cancel(self):
        for rec in self:
            if rec.state in ('draft', 'queued', 'running', 'paused'):
                rec.state = 'cancelled'

    def action_open_candidates(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Candidates'),
            'res_model': 'aipie.product.image.candidate',
            'view_mode': 'kanban,list,form',
            'domain': [('job_id', '=', self.id)],
        }

    # ---------- Cron entrypoint ----------

    @api.model
    def _cron_run_jobs(self):
        """Process a small chunk of pending products across queued jobs.

        Concurrency: we use a *transaction-level* advisory lock
        (`pg_try_advisory_xact_lock`) which auto-releases on commit OR rollback.
        That means we cannot do per-product commits inside the chunk — the whole
        chunk is one atomic unit. The cron framework commits at the end of this
        method on success, or rolls back on raised exception. Since the chunk is
        small (CRON_CHUNK_SIZE=5) the worst case on crash is replaying ≤5 products.
        """
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s)', (ADVISORY_LOCK_KEY,))
        got_lock = self.env.cr.fetchone()[0]
        if not got_lock:
            _logger.info('ai_product_image_enrichment cron: another worker holds the xact lock, skipping this tick.')
            return

        # Budget check before any work
        self._check_budget_and_pause()

        jobs = self.search([('state', 'in', ('queued', 'running'))], order='create_date asc', limit=3)
        for job in jobs:
            try:
                job._process_chunk()
            except Exception:
                _logger.exception('Job %s tick failed', job.id)
                # Increment failure stat in a side cursor so the message persists even
                # though the main txn will commit normally for the rest of the chunk.
                try:
                    job.failure_count += 1
                    job.message_post(body=f'<pre>{traceback.format_exc()[:2000]}</pre>')
                except Exception:
                    pass

    def _process_chunk(self):
        """Process one chunk of products inside the cron's transaction.

        IMPORTANT: do not commit() inside this method. The xact-level advisory
        lock would be released on the first commit, allowing concurrent ticks
        to collide. The cron framework commits on method return.
        """
        self.ensure_one()
        if self.state == 'queued':
            self.write({'state': 'running', 'started_at': self.started_at or fields.Datetime.now()})

        if self.state != 'running':
            return

        chunk = self.pending_product_ids[:CRON_CHUNK_SIZE]
        if not chunk:
            self.write({'state': 'done', 'completed_at': fields.Datetime.now()})
            return

        config = self.env['res.config.settings'].sudo().get_aipie_config()
        from ..services.enrichment_pipeline import (
            enrich_product, normalize_existing_main_image,
        )

        # Counter accumulators — written once at the end so per-product errors don't
        # interleave dirty cache state with successful writes.
        success = failure = white_bg = rembg = 0
        cost_total = 0.0
        processed_ids = []

        for product in chunk:
            try:
                if self.pipeline_steps == 'normalize_only':
                    if product.image_1920:
                        info = normalize_existing_main_image(product, config, self.env)
                        if info and info.get('was_white_bg'):
                            white_bg += 1
                        else:
                            rembg += 1
                    success += 1
                else:
                    cost, stats = enrich_product(product, self, config, self.env)
                    cost_total += cost
                    white_bg += stats.get('white_bg', 0)
                    rembg += stats.get('rembg', 0)
                    success += 1
            except Exception as e:
                _logger.exception('Product %s failed', product.id)
                # Record error on the product. If the product itself got deleted mid-batch
                # this could fail — guard it.
                try:
                    product.write({
                        'aipie_enrichment_state': 'error',
                        'aipie_enrichment_error': str(e)[:5000],
                    })
                except Exception:
                    pass
                failure += 1
            finally:
                processed_ids.append(product.id)

        # Single batched write — avoids stale-cache lost-update bugs that plague
        # `+=` increments after intermediate commits.
        self.write({
            'success_count': self.success_count + success,
            'failure_count': self.failure_count + failure,
            'processed_count': self.processed_count + len(processed_ids),
            'images_with_white_bg_count': self.images_with_white_bg_count + white_bg,
            'images_required_rembg_count': self.images_required_rembg_count + rembg,
            'actual_cost_usd': self.actual_cost_usd + cost_total,
            'pending_product_ids': [(3, pid) for pid in processed_ids],
        })

        if not self.pending_product_ids:
            self.write({'state': 'done', 'completed_at': fields.Datetime.now()})

    # ---------- Budget ----------

    @api.model
    def _check_budget_and_pause(self):
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        budget = config['monthly_ai_budget_usd']
        if not budget:
            return
        spent = self.env['aipie.ai.usage.log']._month_to_date_cost()
        if spent >= budget:
            running = self.search([('state', 'in', ('queued', 'running'))])
            if running:
                running.write({'state': 'paused'})
                _logger.warning('Monthly AI budget exhausted ($%.2f / $%.2f). Paused %d jobs.',
                                spent, budget, len(running))
                self._notify_budget_exhausted(spent, budget)
        elif spent >= 0.8 * budget:
            self._notify_budget_warning(spent, budget)

    def _notify_budget_warning(self, spent, budget):
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        email = config.get('alert_email')
        if not email:
            return
        self.env['mail.mail'].sudo().create({
            'subject': f'[AI Image] Budget at 80% (${spent:.2f}/${budget:.2f})',
            'body_html': f'<p>Monthly Anthropic spend has reached 80% of the configured budget.</p>',
            'email_to': email,
        }).send()

    def _notify_budget_exhausted(self, spent, budget):
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        email = config.get('alert_email')
        if not email:
            return
        self.env['mail.mail'].sudo().create({
            'subject': f'[AI Image] BUDGET EXHAUSTED — jobs paused (${spent:.2f}/${budget:.2f})',
            'body_html': '<p>All running enrichment jobs have been paused. Resume manually after raising the budget.</p>',
            'email_to': email,
        }).send()

    # ---------- XML-RPC API ----------

    @api.model
    def aipie_enrich_by_skus(self, skus, pipeline='discover_only'):
        products = self.env['product.template'].search([('default_code', 'in', skus)])
        if not products:
            return False
        job = self.create({
            'name': _('API Enrichment %s') % fields.Datetime.now(),
            'pipeline_steps': pipeline,
            'product_ids': [(6, 0, products.ids)],
            'state': 'queued',
            'pending_product_ids': [(6, 0, products.ids)],
        })
        return job.id

    @api.model
    def aipie_normalize_existing_images(self, product_ids=None):
        domain = [('image_1920', '!=', False)]
        if product_ids:
            domain.append(('id', 'in', product_ids))
        products = self.env['product.template'].search(domain)
        if not products:
            return False
        job = self.create({
            'name': _('API Normalize %s') % fields.Datetime.now(),
            'pipeline_steps': 'normalize_only',
            'product_ids': [(6, 0, products.ids)],
            'state': 'queued',
            'pending_product_ids': [(6, 0, products.ids)],
        })
        return job.id

    @api.model
    def aipie_get_pending_candidates_for_product(self, product_id):
        cands = self.env['aipie.product.image.candidate'].search([
            ('product_id', '=', product_id),
            ('state', '=', 'pending'),
        ])
        return cands.read(['source_url', 'role', 'confidence', 'ai_reasoning',
                           'has_white_background', 'image_width', 'image_height'])
