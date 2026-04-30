"""
rembg wrapper. Lazily imports rembg so module install never fails on hosts without it.

Heads-up for CloudPepper:
  * First call downloads model weights (~150MB for birefnet-general).
  * onnxruntime needs a few hundred MB RAM at inference.
  * We cache the session at class level — do not call rembg.new_session() per image.
"""

import logging

_logger = logging.getLogger(__name__)


class BackgroundRemoverError(Exception):
    pass


class BackgroundRemover:
    _sessions = {}  # model_name -> rembg session

    @classmethod
    def get_session(cls, model_name: str = 'birefnet-general'):
        if model_name in cls._sessions:
            return cls._sessions[model_name]
        try:
            from rembg import new_session  # noqa: WPS433 (runtime import on purpose)
        except ImportError as e:
            raise BackgroundRemoverError(
                f'rembg not installed in the Odoo Python environment: {e}. '
                f'Run: pip install rembg onnxruntime'
            )
        try:
            session = new_session(model_name)
        except Exception as e:
            raise BackgroundRemoverError(
                f'Failed to load rembg model {model_name!r}: {e}. '
                f'On first run rembg downloads ~150MB of weights — verify outbound HTTPS and disk space.'
            )
        cls._sessions[model_name] = session
        return session

    def remove_background(self, image_bytes: bytes, model_name: str = 'birefnet-general') -> bytes:
        """Returns a PNG (RGBA) with the background removed."""
        try:
            from rembg import remove
        except ImportError as e:
            raise BackgroundRemoverError(f'rembg not installed: {e}')
        session = self.get_session(model_name)
        return remove(image_bytes, session=session)
