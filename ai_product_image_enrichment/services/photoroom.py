"""
Photoroom API wrapper for background removal.

Why this exists: rembg + onnxruntime is ~400MB installed and ~500MB RAM at inference.
Many CloudPepper plans kill workers above 512MB. Photoroom is purpose-built, costs
~$0.02/image in bulk, and removes the entire deployment risk.

API ref: https://www.photoroom.com/api/docs/reference

Falls back to local rembg if no API key is configured (unchanged legacy path).
"""

import logging
import requests

_logger = logging.getLogger(__name__)


class BackgroundRemovalError(Exception):
    pass


class PhotoroomClient:

    ENDPOINT = 'https://sdk.photoroom.com/v1/segment'

    def __init__(self, api_key: str, timeout: int = 60):
        if not api_key:
            raise BackgroundRemovalError('Photoroom API key required')
        self.api_key = api_key
        self.timeout = timeout

    def remove_background(self, image_bytes: bytes) -> bytes:
        """Returns RGBA PNG bytes with the background removed."""
        try:
            resp = requests.post(
                self.ENDPOINT,
                headers={'x-api-key': self.api_key, 'Accept': 'image/png'},
                files={'image_file': ('input.png', image_bytes, 'image/png')},
                data={'format': 'png', 'bg_color': 'transparent'},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise BackgroundRemovalError(f'Photoroom HTTP error: {e}') from e

        if resp.status_code == 402:
            raise BackgroundRemovalError('Photoroom: payment required (out of credits)')
        if resp.status_code == 429:
            raise BackgroundRemovalError('Photoroom: rate limited')
        if resp.status_code >= 400:
            raise BackgroundRemovalError(
                f'Photoroom HTTP {resp.status_code}: {resp.text[:200]}'
            )

        ct = (resp.headers.get('Content-Type') or '').lower()
        if not ct.startswith('image/'):
            raise BackgroundRemovalError(f'Photoroom returned non-image: {ct}')
        return resp.content


class BackgroundRemovalDispatcher:
    """Dispatches to Photoroom if configured, else falls back to local rembg.

    Single seam so the pipeline never imports rembg directly.
    """

    def __init__(self, photoroom_api_key: str = '', rembg_model: str = 'birefnet-general'):
        self.photoroom_api_key = (photoroom_api_key or '').strip()
        self.rembg_model = rembg_model

    def remove(self, image_bytes: bytes) -> bytes:
        if self.photoroom_api_key:
            client = PhotoroomClient(self.photoroom_api_key)
            return client.remove_background(image_bytes)
        # Fall back to local rembg
        from .background_remover import BackgroundRemover, BackgroundRemoverError
        try:
            return BackgroundRemover().remove_background(image_bytes, self.rembg_model)
        except BackgroundRemoverError as e:
            raise BackgroundRemovalError(str(e)) from e

    @property
    def using(self) -> str:
        return 'photoroom' if self.photoroom_api_key else 'rembg'
