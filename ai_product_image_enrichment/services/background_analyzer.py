"""
Detects whether an image already has a white/near-white background so the rembg step can be skipped.

Strategy:
  - Sample a 5%-thick border ring of pixels.
  - A pixel counts as "white" if all RGB channels >= white_threshold.
  - If border whiteness >= min_white_percent AND top-corner whiteness is near-perfect,
    we treat the image as already having a clean white background.
  - Top corners use a strict threshold; bottom corners use a relaxed one to tolerate
    soft shadows under products (extremely common in real studio shots).
"""

import io

import numpy as np
from PIL import Image, ImageOps


class BackgroundAnalyzer:

    def __init__(self, white_threshold: int = 245, min_white_percent: int = 85):
        self.white_threshold = max(0, min(255, int(white_threshold)))
        self.min_white_percent = max(0, min(100, int(min_white_percent)))

    def analyze(self, image_bytes: bytes):
        """Return (has_white_bg: bool, white_percent: float, info: dict)."""
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert('RGB')
        arr = np.asarray(img)
        h, w = arr.shape[:2]

        border_thickness = max(1, int(min(h, w) * 0.05))
        top    = arr[:border_thickness, :, :]
        bottom = arr[-border_thickness:, :, :]
        left   = arr[:, :border_thickness, :]
        right  = arr[:, -border_thickness:, :]

        border_pixels = np.concatenate([
            top.reshape(-1, 3),
            bottom.reshape(-1, 3),
            left.reshape(-1, 3),
            right.reshape(-1, 3),
        ])
        white_mask = np.all(border_pixels >= self.white_threshold, axis=1)
        border_white_percent = float(white_mask.sum()) / len(border_pixels) * 100

        corner_size = max(5, int(min(h, w) * 0.02))
        top_corners = np.concatenate([
            arr[:corner_size, :corner_size, :].reshape(-1, 3),
            arr[:corner_size, -corner_size:, :].reshape(-1, 3),
        ])
        bottom_corners = np.concatenate([
            arr[-corner_size:, :corner_size, :].reshape(-1, 3),
            arr[-corner_size:, -corner_size:, :].reshape(-1, 3),
        ])
        top_white = float(np.all(top_corners >= self.white_threshold, axis=1).sum()) / len(top_corners) * 100
        # Relax bottom corners to tolerate product shadows
        bottom_white = float(np.all(bottom_corners >= self.white_threshold - 15, axis=1).sum()) / len(bottom_corners) * 100

        has_white_bg = (
            border_white_percent >= self.min_white_percent
            and top_white >= 95
            and bottom_white >= 80
        )

        return has_white_bg, border_white_percent, {
            'border_white_percent': border_white_percent,
            'top_corner_white_percent': top_white,
            'bottom_corner_white_percent': bottom_white,
            'threshold_used': self.white_threshold,
        }
