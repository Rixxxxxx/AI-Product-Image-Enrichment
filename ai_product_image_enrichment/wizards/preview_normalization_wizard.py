"""
Scratchpad to test normalization settings before processing the catalog.

Upload a sample image, tweak target_size / padding / threshold, see the result instantly.
"""

import base64
import io
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class PreviewNormalizationWizard(models.TransientModel):
    _name = 'aipie.preview.normalization.wizard'
    _description = 'Preview Normalization'

    sample_image = fields.Binary(string='Sample Image', required=True)
    sample_filename = fields.Char()

    target_canvas_size = fields.Integer(default=1600)
    padding_percent = fields.Integer(default=8)
    bg_color = fields.Char(default='#FFFFFF')
    output_format = fields.Selection([
        ('jpeg', 'JPEG'), ('png', 'PNG'),
    ], default='jpeg')
    jpeg_quality = fields.Integer(default=92)
    white_threshold = fields.Integer(default=245)
    white_bg_min_percent = fields.Integer(default=85)

    detected_white_bg = fields.Boolean(readonly=True)
    detected_white_pct = fields.Float(readonly=True, string='Border White %')
    detected_top_corner_pct = fields.Float(readonly=True, string='Top Corner White %')
    detected_bottom_corner_pct = fields.Float(readonly=True, string='Bottom Corner White %')

    normalized_preview = fields.Binary(readonly=True, string='Normalized Output (transparent PNG)')
    transparency_check_preview = fields.Binary(
        readonly=True, string='Transparency Check',
        help='Same output composited on a checkerboard so transparent areas are visually obvious.',
    )
    notes = fields.Text(readonly=True)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        cfg = self.env['res.config.settings'].sudo().get_aipie_config()
        res.update({
            'target_canvas_size': cfg['target_canvas_size'],
            'padding_percent': cfg['padding_percent'],
            'bg_color': cfg['bg_color'],
            'output_format': cfg['output_format'] if cfg['output_format'] in ('jpeg', 'png') else 'jpeg',
            'jpeg_quality': cfg['jpeg_quality'],
            'white_threshold': cfg['white_threshold'],
            'white_bg_min_percent': cfg['white_bg_min_percent'],
        })
        return res

    def action_preview(self):
        self.ensure_one()
        if not self.sample_image:
            raise UserError(_('Upload a sample image first.'))

        from ..services.background_analyzer import BackgroundAnalyzer
        from ..services.image_normalizer import ImageNormalizer
        from ..services.photoroom import BackgroundRemovalDispatcher

        cfg = self.env['res.config.settings'].sudo().get_aipie_config()

        raw = base64.b64decode(self.sample_image)
        analyzer = BackgroundAnalyzer(
            white_threshold=self.white_threshold,
            min_white_percent=self.white_bg_min_percent,
        )
        has_white, white_pct, info = analyzer.analyze(raw)

        # Mirror the real main-image pipeline: always run BG removal, output transparent PNG.
        notes = []
        dispatcher = BackgroundRemovalDispatcher(
            photoroom_api_key=cfg.get('photoroom_api_key', ''),
            rembg_model=cfg['rembg_model'],
        )
        try:
            bg_removed = dispatcher.remove(raw)
            notes.append(f'Background removed via {dispatcher.using}.')
        except Exception as e:
            bg_removed = raw
            notes.append(
                f'BG removal unavailable ({e}). Preview falls back to white-to-alpha '
                'approximation — edge halos will appear; configure Photoroom for clean output.'
            )

        normalizer = ImageNormalizer()
        normalized = normalizer.normalize(
            bg_removed,
            target_size=self.target_canvas_size,
            padding_percent=self.padding_percent,
            bg_color=self.bg_color,
            white_threshold=self.white_threshold,
            transparent_canvas=True,
        )

        if has_white:
            notes.append('Source already had a white background — Photoroom result will be very clean.')
        else:
            notes.append('Source has a non-white background — Photoroom doing the heavy lifting.')
        notes.append(f'Output: transparent PNG, {len(normalized) // 1024} KB')

        # Build a transparency-check composite on a checkerboard
        check_b64 = self._composite_on_checker(normalized)

        self.write({
            'detected_white_bg': has_white,
            'detected_white_pct': white_pct,
            'detected_top_corner_pct': info.get('top_corner_white_percent', 0.0),
            'detected_bottom_corner_pct': info.get('bottom_corner_white_percent', 0.0),
            'normalized_preview': base64.b64encode(normalized),
            'transparency_check_preview': check_b64,
            'notes': '\n'.join(notes),
        })
        return {
            'type': 'ir.actions.act_window',
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    @staticmethod
    def _composite_on_checker(png_bytes: bytes) -> bytes:
        """Composite a transparent PNG on a checker pattern. Output: base64 PNG bytes."""
        import io
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert('RGBA')
        w, h = img.size
        cell = max(16, min(w, h) // 32)
        checker = Image.new('RGB', (w, h), (240, 240, 240))
        from PIL import ImageDraw
        d = ImageDraw.Draw(checker)
        for y in range(0, h, cell):
            for x in range(0, w, cell):
                if ((x // cell) + (y // cell)) % 2 == 0:
                    d.rectangle([x, y, x + cell - 1, y + cell - 1], fill=(208, 208, 208))
        checker = checker.convert('RGBA')
        composite = Image.alpha_composite(checker, img)
        out = io.BytesIO()
        composite.convert('RGB').save(out, format='PNG', optimize=True)
        return base64.b64encode(out.getvalue())

    def action_save_as_defaults(self):
        self.ensure_one()
        param = self.env['ir.config_parameter'].sudo()
        param.set_param('ai_product_image_enrichment.aipie_target_canvas_size', str(self.target_canvas_size))
        param.set_param('ai_product_image_enrichment.aipie_padding_percent', str(self.padding_percent))
        param.set_param('ai_product_image_enrichment.aipie_bg_color', self.bg_color)
        param.set_param('ai_product_image_enrichment.aipie_jpeg_quality', str(self.jpeg_quality))
        param.set_param('ai_product_image_enrichment.aipie_output_format', self.output_format)
        param.set_param('ai_product_image_enrichment.aipie_white_threshold', str(self.white_threshold))
        param.set_param('ai_product_image_enrichment.aipie_white_bg_min_percent', str(self.white_bg_min_percent))
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Saved'),
                'message': _('Normalization defaults updated.'),
                'type': 'success',
            },
        }
