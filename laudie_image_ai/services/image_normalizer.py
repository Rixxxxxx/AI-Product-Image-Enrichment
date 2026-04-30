"""
The visual heart of the module.

Every main image gets:
  1. Trimmed to its actual content bounding box
  2. Resized proportionally to fit a target inner area
  3. Centered on a uniform canvas (transparent for main, white for legacy paths)
     with consistent padding

This is what makes the shop grid look professional — products at the same
proportional size and position within the same canvas dimensions.

Two output modes:
  * transparent_canvas=True (used for main images)  → RGBA canvas, PNG output
  * transparent_canvas=False (legacy / gallery)     → white RGB canvas, JPEG/PNG
"""

import io
import logging

import numpy as np
from PIL import Image, ImageOps

_logger = logging.getLogger(__name__)


def _hex_to_rgb(hex_color: str) -> tuple:
    h = (hex_color or '#FFFFFF').lstrip('#')
    if len(h) == 3:
        h = ''.join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class ImageNormalizer:

    def normalize(
        self,
        image_bytes: bytes,
        target_size: int = 1920,
        padding_percent: int = 8,
        bg_color: str = '#FFFFFF',
        output_format: str = 'jpeg',
        jpeg_quality: int = 92,
        already_has_white_bg: bool = False,
        white_threshold: int = 245,
        transparent_canvas: bool = False,
    ) -> bytes:
        """Normalize an image to the target uniform canvas.

        When transparent_canvas=True, the input is expected to already be
        RGBA (post-Photoroom/rembg). The output is a transparent PNG.
        """
        if target_size <= 0:
            raise ValueError('target_size must be positive')
        padding_percent = max(0, min(40, int(padding_percent)))

        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)

        # Trim to content
        if img.mode in ('RGBA', 'LA') or 'transparency' in img.info:
            img = img.convert('RGBA')
            content_bbox = self._find_content_bbox_alpha(img)
        else:
            rgb = img.convert('RGB')
            content_bbox = self._find_content_bbox_white_bg(rgb, white_threshold)
            if transparent_canvas:
                # Caller asked for transparency but we got an opaque image —
                # synthesize alpha by treating white-ish pixels as transparent.
                # Edges may have halos; better to BG-remove upstream.
                img = self._white_to_alpha(rgb, white_threshold)
            else:
                img = rgb

        if content_bbox:
            img = img.crop(content_bbox)

        # Inner area = target * (1 - 2 * padding/100)
        inner_size = max(1, int(target_size * (1 - 2 * padding_percent / 100)))

        scale = min(inner_size / img.width, inner_size / img.height)
        if scale < 1.0 or (img.width < inner_size and img.height < inner_size):
            new_w = max(1, int(img.width * scale))
            new_h = max(1, int(img.height * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)

        # Composite onto canvas
        if transparent_canvas:
            canvas = Image.new('RGBA', (target_size, target_size), (0, 0, 0, 0))
            offset = (
                (target_size - img.width) // 2,
                (target_size - img.height) // 2,
            )
            if img.mode != 'RGBA':
                img = img.convert('RGBA')
            canvas.paste(img, offset, img)
            out = io.BytesIO()
            canvas.save(out, format='PNG', optimize=True)
            return out.getvalue()

        # Legacy / gallery: white canvas
        canvas = Image.new('RGB', (target_size, target_size), _hex_to_rgb(bg_color))
        offset = (
            (target_size - img.width) // 2,
            (target_size - img.height) // 2,
        )
        if img.mode == 'RGBA':
            canvas.paste(img, offset, img)
        else:
            canvas.paste(img.convert('RGB'), offset)

        out = io.BytesIO()
        fmt = (output_format or 'jpeg').lower()
        if fmt == 'png':
            canvas.save(out, format='PNG', optimize=True)
        elif fmt == 'auto':
            if not already_has_white_bg and img.mode == 'RGBA':
                canvas.save(out, format='PNG', optimize=True)
            else:
                canvas.save(out, format='JPEG', quality=jpeg_quality, optimize=True, progressive=True)
        else:
            canvas.save(out, format='JPEG', quality=jpeg_quality, optimize=True, progressive=True)
        return out.getvalue()

    # ---------- helpers ----------

    @staticmethod
    def _find_content_bbox_alpha(img):
        alpha = img.split()[-1]
        return alpha.getbbox()

    @staticmethod
    def _find_content_bbox_white_bg(img_rgb, white_threshold=245):
        arr = np.asarray(img_rgb)
        non_white_mask = np.any(arr < white_threshold, axis=2)
        rows = np.any(non_white_mask, axis=1)
        cols = np.any(non_white_mask, axis=0)
        if not rows.any() or not cols.any():
            return None
        rmin, rmax = int(np.where(rows)[0][0]), int(np.where(rows)[0][-1])
        cmin, cmax = int(np.where(cols)[0][0]), int(np.where(cols)[0][-1])
        h, w = arr.shape[:2]
        return (max(0, cmin - 1), max(0, rmin - 1),
                min(w, cmax + 2), min(h, rmax + 2))

    @staticmethod
    def _white_to_alpha(img_rgb, white_threshold=245):
        """Approximate transparency from a white background. Produces halos on
        antialiased edges — only used as a fallback when no real BG removal ran."""
        arr = np.array(img_rgb.convert('RGB'))
        h, w = arr.shape[:2]
        white_mask = np.all(arr >= white_threshold, axis=2)
        rgba = np.zeros((h, w, 4), dtype=np.uint8)
        rgba[..., :3] = arr
        rgba[..., 3] = np.where(white_mask, 0, 255)
        return Image.fromarray(rgba, mode='RGBA')
