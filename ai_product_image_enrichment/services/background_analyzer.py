"""
Detects the source-image background state so the pipeline can skip work that
isn't needed.

Three states (returned in info['source_state']):
  'transparent' — RGBA source whose border pixels are mostly already transparent.
                  Background removal is unnecessary; trim/center/pad applies.
  'white'       — Opaque source with a clean white background.
                  Background removal still runs (output must be transparent), but
                  edges will be cleanest with no halos.
  'complex'     — Anything else — non-white background, busy scene, etc.
                  Background removal still runs; edges may have minor artifacts.

Strategy for opaque sources:
  - Sample a 5%-thick border ring of pixels.
  - A pixel counts as "white" if all RGB channels >= white_threshold.
  - If border whiteness >= min_white_percent AND top-corner whiteness is near-perfect,
    we treat the image as already having a clean white background.
  - Top corners use a strict threshold; bottom corners use a relaxed one to tolerate
    soft shadows under products.
"""

import io

import numpy as np
from PIL import Image, ImageOps


class BackgroundAnalyzer:

    def __init__(self, white_threshold: int = 245, min_white_percent: int = 85):
        self.white_threshold = max(0, min(255, int(white_threshold)))
        self.min_white_percent = max(0, min(100, int(min_white_percent)))

    def analyze(self, image_bytes: bytes):
        """Return (has_white_bg: bool, white_percent: float, info: dict).

        For backward compatibility, has_white_bg is True only for the legacy
        'white' state. Callers checking source_state explicitly should look at
        info['source_state'] which is one of 'transparent' / 'white' / 'complex'.
        """
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img)

        # Stage 1: detect already-transparent sources via alpha channel
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            rgba = img.convert('RGBA')
            arr_rgba = np.asarray(rgba)
            alpha = arr_rgba[..., 3]
            h, w = alpha.shape
            border_thickness = max(1, int(min(h, w) * 0.05))
            border_alpha = np.concatenate([
                alpha[:border_thickness, :].reshape(-1),
                alpha[-border_thickness:, :].reshape(-1),
                alpha[:, :border_thickness].reshape(-1),
                alpha[:, -border_thickness:].reshape(-1),
            ])
            transparent_pct = float((border_alpha < 32).sum()) / len(border_alpha) * 100
            if transparent_pct >= 80:
                return False, 0.0, {
                    'source_state': 'transparent',
                    'border_transparent_percent': transparent_pct,
                }

        # Stage 2: opaque image — analyze for white background
        rgb = img.convert('RGB')
        arr = np.asarray(rgb)
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
        bottom_white = float(np.all(bottom_corners >= self.white_threshold - 15, axis=1).sum()) / len(bottom_corners) * 100

        has_white_bg = (
            border_white_percent >= self.min_white_percent
            and top_white >= 95
            and bottom_white >= 80
        )
        source_state = 'white' if has_white_bg else 'complex'

        return has_white_bg, border_white_percent, {
            'source_state': source_state,
            'border_white_percent': border_white_percent,
            'top_corner_white_percent': top_white,
            'bottom_corner_white_percent': bottom_white,
            'threshold_used': self.white_threshold,
        }
