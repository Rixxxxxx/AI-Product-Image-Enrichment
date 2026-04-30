from odoo import _, api, fields, models


class ReviewCandidatesWizard(models.TransientModel):
    _name = 'laudie.review.candidates.wizard'
    _description = 'Bulk Candidate Review'

    job_id = fields.Many2one('laudie.enrichment.job')
    confidence_threshold = fields.Float(default=0.85)
    role_filter = fields.Selection([
        ('all', 'All Roles'),
        ('main', 'Main Only'),
        ('gallery', 'Gallery (non-main)'),
    ], default='main')

    def action_approve_high_confidence(self):
        self.ensure_one()
        domain = [('state', '=', 'pending'), ('confidence', '>=', self.confidence_threshold)]
        if self.job_id:
            domain.append(('job_id', '=', self.job_id.id))
        if self.role_filter == 'main':
            domain.append(('role', '=', 'main'))
        elif self.role_filter == 'gallery':
            domain.append(('role', '!=', 'main'))
        cands = self.env['laudie.product.image.candidate'].search(domain)
        cands.write({'state': 'approved'})
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Approved'),
                'message': _('%d candidates approved.') % len(cands),
                'type': 'success',
            },
        }

    def action_apply_approved(self):
        self.ensure_one()
        domain = [('state', '=', 'approved')]
        if self.job_id:
            domain.append(('job_id', '=', self.job_id.id))
        cands = self.env['laudie.product.image.candidate'].search(domain)
        cands.action_apply_to_product()
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Applied'),
                'message': _('%d candidates applied.') % len(cands),
                'type': 'success',
            },
        }
