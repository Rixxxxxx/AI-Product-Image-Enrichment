import logging
import traceback

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)

# Chunk size per cron tick. CloudPepper-friendly: short bursts under 60s.
# Products processed per cron tick. Each product commits independently
# (see _process_chunk) so a mid-chunk timeout or crash only loses the
# in-flight product, not the ones already done. Throughput at chunk=5
# = ~5 products/min = 100 min for 500 products.
CRON_CHUNK_SIZE = 5


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

    images_with_white_bg_count = fields.Integer(
        readonly=True, string='Studio-Source Candidates',
        help='Candidate images whose source already had a clean studio background. '
             'These produce the cleanest transparent output.',
    )
    images_required_rembg_count = fields.Integer(
        readonly=True, string='Complex-Source Candidates',
        help='Candidate images whose source had a complex / non-studio background. '
             'Background removal still applied; final output is transparent PNG either way.',
    )
    no_result_count = fields.Integer(readonly=True, string='No-Result Count',
        help='Products processed without errors but where the AI found no usable images '
             '(no_results / needs_manual_main / skipped_no_sku).')

    estimated_cost_usd = fields.Float(readonly=True)
    actual_cost_usd = fields.Float(readonly=True)

    started_at = fields.Datetime(readonly=True)
    completed_at = fields.Datetime(readonly=True)

    log_ids = fields.One2many('aipie.enrichment.log', 'job_id')
    candidate_ids = fields.One2many('aipie.product.image.candidate', 'job_id')

    discover_main_image = fields.Boolean(
        default=False, string='Also discover main image',
        help='When ON, the AI also looks for a main image candidate. Use for products '
             'that do not yet have a usable main image. The found image will be '
             'background-removed and normalized just like the Normalize Main Images '
             'wizard does. When OFF (default), discovery is gallery-only.',
    )

    # Circuit-breaker fields
    cost_cap_usd = fields.Float(
        default=1.0, string='Cost cap (USD)',
        help='Pause this job automatically once its accumulated Anthropic spend '
             'exceeds this amount. A safety brake against runaway costs. Set 0 to disable.',
    )
    max_consecutive_failures = fields.Integer(
        default=5, string='Pause after N consecutive failures',
        help='Pause this job if N products in a row end with no_results / error / '
             'needs_manual_main. Likely indicates wrong settings or a broken provider. '
             'Set 0 to disable.',
    )
    consecutive_failure_count = fields.Integer(
        default=0, readonly=True, copy=False,
        help='Internal counter — resets on a successful product.',
    )
    pause_reason = fields.Char(readonly=True, copy=False)

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
                pending = rec.pending_product_ids
                rec.state = 'cancelled'
                rec._reset_orphaned_queued_products(pending)

    def action_finish_now(self):
        """Stop processing remaining products but keep everything already
        produced (candidates, applied images, logs). Use when a job is hung
        or you've decided what's been collected so far is good enough — you
        can immediately review/apply candidates without waiting for the rest.

        Difference from cancel: pending products are cleared (no replay) and
        the job lands in 'done', not 'cancelled' — so its candidates remain
        first-class artifacts in reporting.
        """
        for rec in self:
            if rec.state not in ('queued', 'running', 'paused'):
                continue
            pending = rec.pending_product_ids
            rec.write({
                'state': 'done',
                'completed_at': fields.Datetime.now(),
                'pending_product_ids': [(5, 0, 0)],
            })
            rec._reset_orphaned_queued_products(pending)

    def unlink(self):
        # When a job is deleted, products that were waiting on it must be
        # reset — otherwise they stay 'Queued for Processing' forever.
        for rec in self:
            if rec.pending_product_ids:
                rec._reset_orphaned_queued_products(rec.pending_product_ids)
        return super().unlink()

    def _reset_orphaned_queued_products(self, products):
        """For products whose state is still 'queued' AND aren't pending in any
        OTHER active job, reset to 'not_enriched' so the operator sees a clean
        state instead of stale 'Queued for Processing'.
        """
        if not products:
            return
        for product in products:
            if product.aipie_enrichment_state != 'queued':
                continue
            other_active = self.search([
                ('id', '!=', self.id),
                ('state', 'in', ('queued', 'running', 'paused')),
                ('pending_product_ids', 'in', product.id),
            ], limit=1)
            if not other_active:
                product.aipie_enrichment_state = 'not_enriched'

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
        """Process a chunk of pending products across queued jobs.

        Concurrency: relies on Odoo's built-in ir.cron row lock to serialize
        concurrent ticks of THIS cron. We deliberately do NOT use a Postgres
        advisory lock — when a worker is killed mid-run (Odoo time-limit
        exceeded), an advisory lock survives on the dead connection and blocks
        every subsequent tick until the connection drops. That manifested as
        'one log entry then silence for 5+ minutes'. Trusting the ir.cron
        framework's lock means a killed worker doesn't strand future ticks.

        Per-product commits (in _process_chunk) ensure work isn't lost if the
        cron framework rolls back this tick's outer transaction.
        """
        self._check_budget_and_pause()
        try:
            self.env.cr.commit()
        except Exception:
            pass

        jobs = self.search([('state', 'in', ('queued', 'running'))], order='create_date asc', limit=3)
        for job in jobs:
            try:
                job._process_chunk()
            except Exception:
                _logger.exception('Job %s tick failed', job.id)
                try:
                    self.env.cr.rollback()
                    job.failure_count += 1
                    job.message_post(body=f'<pre>{traceback.format_exc()[:2000]}</pre>')
                    self.env.cr.commit()
                except Exception:
                    pass

    def _process_chunk(self):
        """Process one chunk of products. COMMITS PER PRODUCT — each successful
        product's candidates and pending-list removal persist immediately, so
        a later product failing or the cron worker being killed never wipes
        out work we already paid Anthropic for.
        """
        self.ensure_one()
        if self.state == 'queued':
            self.write({'state': 'running', 'started_at': self.started_at or fields.Datetime.now()})
            self.env.cr.commit()  # lock in the running state immediately

        if self.state != 'running':
            return

        # Orphan recovery (asymmetric to prevent infinite replay):
        #   - state='queued': never attempted (pre-checkpoint commit didn't happen
        #     OR happened but worker died before enrich_product ran). Re-queue —
        #     we never got a chance to spend money on it.
        #   - state='searching': WAS attempted, enrich_product ran, but worker
        #     was killed mid-flight before the final state write committed. Do
        #     NOT re-queue: that was the ProGuard 20 replay loop. Mark 'error'
        #     so the product is visible as failed and can be re-queued manually.
        if not self.pending_product_ids:
            untried = self.product_ids.filtered(lambda p: p.aipie_enrichment_state == 'queued')
            killed_mid = self.product_ids.filtered(lambda p: p.aipie_enrichment_state == 'searching')
            if untried:
                _logger.info(
                    'Job %s: re-queuing %s untried orphan product(s)',
                    self.id, len(untried),
                )
                self.write({'pending_product_ids': [(4, p.id) for p in untried]})
                self.env.cr.commit()
            if killed_mid:
                _logger.warning(
                    'Job %s: marking %s mid-flight-killed product(s) as error '
                    '(worker SIGXCPU before final state write; not auto-retrying)',
                    self.id, len(killed_mid),
                )
                killed_mid.write({
                    'aipie_enrichment_state': 'error',
                    'aipie_enrichment_error': (
                        'Worker was killed mid-enrichment (likely Odoo --limit-time-real). '
                        'Manually re-queue from the product form to retry.'
                    ),
                })
                self.env.cr.commit()

        chunk = self.pending_product_ids[:CRON_CHUNK_SIZE]
        if not chunk:
            self.write({'state': 'done', 'completed_at': fields.Datetime.now()})
            self.env.cr.commit()
            return

        config = self.env['res.config.settings'].sudo().get_aipie_config()
        from ..services.enrichment_pipeline import (
            enrich_product, normalize_existing_main_image,
        )
        from ..services.ai_image_classifier import AIServiceUnavailable

        if self.cost_cap_usd and self._job_actual_cost() >= self.cost_cap_usd:
            self._pause_with_reason(
                f'Cost cap reached: spent ${self._job_actual_cost():.2f} of ${self.cost_cap_usd:.2f} cap. '
                f'Resume manually after reviewing.'
            )
            return

        # Per-iteration accumulators — written + committed per product.
        success = failure = no_result = white_bg = rembg = 0
        cost_total = 0.0
        local_consecutive = self.consecutive_failure_count

        NO_RESULT_STATES = ('no_results', 'needs_manual_main', 'skipped_no_sku')
        SUCCESS_STATES = ('candidates_found', 'enriched')

        # Chunk-level wall-clock budget. Production-observed worker kill is
        # ~55-60s real (not the 120s the message claims), so we cap the chunk
        # at 45s and let the next tick pick up the remainder. Per-product
        # budget (in enrich_product) is independently 50s — this is the OUTER
        # cap so a multi-product chunk can't blow past the worker's actual
        # wall-clock limit.
        import time as _time
        CHUNK_BUDGET_SECONDS = 45
        chunk_deadline = _time.time() + CHUNK_BUDGET_SECONDS

        for product in chunk:
            if _time.time() > chunk_deadline:
                _logger.info(
                    'Chunk wall-clock budget (%ss) reached after processing some '
                    'products; deferring %s remaining products to next cron tick.',
                    CHUNK_BUDGET_SECONDS,
                    len(chunk) - chunk.ids.index(product.id),
                )
                break
            # CIRCUIT BREAKER: consecutive failure cap (checked per product so we
            # bail mid-chunk if needed)
            if (self.max_consecutive_failures
                    and local_consecutive >= self.max_consecutive_failures):
                self._pause_with_reason(
                    f'{local_consecutive} consecutive product failures — likely a config/provider '
                    f'issue. Inspect Step Logs for the failed products and resume when fixed.'
                )
                break

            # PRE-PROCESSING CHECKPOINT (main cursor + commit).
            # Remove the product from pending_product_ids RIGHT NOW so even if
            # the cron worker is killed mid-Claude-call (Odoo --limit-time-real
            # timeout), the product cannot replay on the next tick. We pay the
            # Claude bill at most once per product per operator-initiated job.
            #
            # We commit on the MAIN cursor (not a side cursor): a side-cursor
            # write to the same row that the main cursor later updates triggers
            # psycopg2 SerializationFailure under SERIALIZABLE isolation, which
            # we hit at 15:51:24 (product_template) and 17:09:44 (this row).
            # Single-cursor avoids the cross-txn conflict entirely. Orphan
            # recovery (top of _process_chunk) re-queues products whose state
            # is still 'queued'/'searching' if a worker died after this commit
            # but before the post-processing commit.
            self.write({'pending_product_ids': [(3, product.id)]})
            try:
                self.env.cr.commit()
            except Exception:
                _logger.exception('Pre-processing commit failed for product %s', product.id)
                self.env.cr.rollback()

            product_failed = False
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
                    state = product.aipie_enrichment_state
                    if state in SUCCESS_STATES:
                        success += 1
                    elif state in NO_RESULT_STATES:
                        no_result += 1
                        product_failed = True
                    elif state == 'error':
                        failure += 1
                        product_failed = True
                    else:
                        # Unknown / transitional state — count as no_result to be safe
                        no_result += 1
                        product_failed = True
            except AIServiceUnavailable as e:
                # CIRCUIT BREAKER: API kill-switch. Pause every running job AND
                # log to the enrichment log via side cursor so the operator sees
                # WHY everything stopped (otherwise the Step Logs tab is silent
                # because the cap fires before any logging code in the pipeline).
                _logger.error('Anthropic API unavailable: %s — pausing every running job', e)
                from ..services.enrichment_pipeline import _log_now
                try:
                    _log_now(self.env, {
                        'job_id': self.id,
                        'product_id': product.id,
                        'step': 'classify',
                        'level': 'error',
                        'message': f'⛔ KILL-SWITCH FIRED: {e}\n\nThis pauses every running job. '
                                   f'Either wait for the trailing-hour window to roll off, raise the '
                                   f'cap in Settings → AI Images → Cost Control, or set it to 0 to '
                                   f'disable. Job state is now "paused" — review and click Resume '
                                   f'when the cap is sorted.',
                    })
                except Exception:
                    pass
                # Remove from pending via side cursor (so this product doesn't replay)
                try:
                    with self.env.registry.cursor() as side_cr:
                        side_env = self.env(cr=side_cr)
                        side_env['aipie.enrichment.job'].browse(self.id).write({
                            'pending_product_ids': [(3, product.id)],
                        })
                except Exception:
                    pass
                self._pause_all_running_jobs(reason=f'Anthropic API unavailable: {e}')
                try:
                    self.env.cr.commit()
                except Exception:
                    pass
                break
            except Exception as e:
                _logger.exception('Product %s failed', product.id)
                try:
                    product.write({
                        'aipie_enrichment_state': 'error',
                        'aipie_enrichment_error': str(e)[:5000],
                    })
                except Exception:
                    pass
                failure += 1
                product_failed = True

            if product_failed:
                local_consecutive += 1
            else:
                local_consecutive = 0

            # POST-PROCESSING: write all job counters on the main cursor only.
            # We previously mirrored processed_count/consecutive_failure_count via
            # a side cursor "for durability if main txn dies", but that write
            # collides with the main cursor's counter write under SERIALIZABLE
            # isolation and triggers SerializationFailure (the same bug we hit
            # earlier on product_template, now on aipie_enrichment_job). If the
            # main commit fails, the product is rolled back as a unit — no
            # divergence between counters and candidates.
            # actual_cost_usd is sourced from aipie_ai_usage_log (written via
            # side cursor inside the classifier) rather than the local cost_total
            # accumulator — enrich_product always returns 0.0 here because the
            # real spend lives in the usage log rows. This kept actual_cost_usd
            # stuck at $0.00 for every job.
            self.write({
                'processed_count': self.processed_count + 1,
                'consecutive_failure_count': local_consecutive,
                'success_count': self.success_count + success,
                'failure_count': self.failure_count + failure,
                'no_result_count': self.no_result_count + no_result,
                'images_with_white_bg_count': self.images_with_white_bg_count + white_bg,
                'images_required_rembg_count': self.images_required_rembg_count + rembg,
                'actual_cost_usd': self._job_actual_cost(),
            })
            try:
                self.env.cr.commit()
            except Exception:
                _logger.exception('Main-cursor commit failed for product %s', product.id)
                try:
                    self.env.cr.rollback()
                except Exception:
                    pass

            # Reset per-iteration counters since we just committed them
            success = failure = no_result = white_bg = rembg = 0
            cost_total = 0.0

        if self.state == 'running' and not self.pending_product_ids:
            self.write({'state': 'done', 'completed_at': fields.Datetime.now()})
            try:
                self.env.cr.commit()
            except Exception:
                pass
            self._bus_notify_complete()

    # ---------- Circuit-breaker helpers ----------

    def _job_actual_cost(self):
        """Sum of ai_usage_log.cost_usd for entries linked to this job."""
        self.ensure_one()
        self.env.cr.execute(
            'SELECT COALESCE(SUM(cost_usd), 0) FROM aipie_ai_usage_log WHERE job_id = %s',
            (self.id,),
        )
        return float(self.env.cr.fetchone()[0] or 0.0)

    def _pause_with_reason(self, reason):
        self.ensure_one()
        _logger.warning('Pausing job %s: %s', self.id, reason)
        self.write({'state': 'paused', 'pause_reason': reason[:255]})
        try:
            self.message_post(body=f'<p><b>Job paused</b>: {reason}</p>')
        except Exception:
            pass
        self._bus_notify_paused(reason)

    def _pause_all_running_jobs(self, reason):
        running = self.search([('state', 'in', ('queued', 'running'))])
        if not running:
            return
        running.write({'state': 'paused', 'pause_reason': reason[:255]})
        for j in running:
            try:
                j.message_post(body=f'<p><b>Job paused (global kill-switch)</b>: {reason}</p>')
                j._bus_notify_paused(reason)
            except Exception:
                pass

    def _bus_notify_paused(self, reason):
        self.ensure_one()
        try:
            target = self.create_uid.partner_id if self.create_uid else None
            if not target:
                return
            self.env['bus.bus']._sendone(
                target,
                'simple_notification',
                {
                    'type': 'danger',
                    'title': f'AI Enrichment paused: {self.name}',
                    'message': reason,
                    'sticky': True,
                },
            )
        except Exception as e:
            _logger.warning('Pause notification failed: %s', e)

    def _bus_notify_complete(self):
        """Send a real-time toast notification to the user who created this job.

        Uses Odoo's built-in WebSocket bus — zero polling overhead, instant
        delivery. Falls back silently if the bus model isn't available.
        """
        self.ensure_one()
        try:
            target = self.create_uid.partner_id if self.create_uid else None
            if not target:
                return
            ai_step_label = {
                'discover_only': 'AI discovery',
                'discover_apply': 'AI discovery + auto-apply',
                'normalize_only': 'Normalization',
                'full': 'Full pipeline',
            }.get(self.pipeline_steps, 'Enrichment')
            msg_lines = [
                f'{ai_step_label} finished.',
                f'Processed: {self.processed_count} • Success: {self.success_count} • Failed: {self.failure_count}',
            ]
            if self.actual_cost_usd:
                msg_lines.append(f'Cost: ${self.actual_cost_usd:.2f}')
            self.env['bus.bus']._sendone(
                target,
                'simple_notification',
                {
                    'type': 'success' if self.failure_count == 0 else 'warning',
                    'title': self.name or 'AI Enrichment Complete',
                    'message': '\n'.join(msg_lines),
                    'sticky': False,
                },
            )
        except Exception as e:
            _logger.warning('Bus notification for job %s failed: %s', self.id, e)

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
            'subject': f'[AI Image Enrichment] Budget at 80% (${spent:.2f}/${budget:.2f})',
            'body_html': f'<p>Monthly Anthropic spend has reached 80% of the configured budget.</p>',
            'email_to': email,
        }).send()

    def _notify_budget_exhausted(self, spent, budget):
        config = self.env['res.config.settings'].sudo().get_aipie_config()
        email = config.get('alert_email')
        if not email:
            return
        self.env['mail.mail'].sudo().create({
            'subject': f'[AI Image Enrichment] BUDGET EXHAUSTED — jobs paused (${spent:.2f}/${budget:.2f})',
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
