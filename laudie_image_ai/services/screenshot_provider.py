"""
Headless screenshot for SPA/JS-rendered manufacturer pages.

Used as a FALLBACK when DOM parsing finds <5 plausible images on a page.
Many modern manufacturer sites render the gallery via React; the static HTML
is essentially empty. Screenshot + Claude vision sidesteps that entirely.

Default backend: Browserless.io. Swap by configuring browserless_endpoint.
"""

import base64
import logging
import requests

_logger = logging.getLogger(__name__)


class ScreenshotError(Exception):
    pass


class BrowserlessClient:

    def __init__(self, api_key: str,
                 endpoint: str = 'https://chrome.browserless.io',
                 timeout: int = 45):
        if not api_key:
            raise ScreenshotError('Browserless API key required')
        self.api_key = api_key
        self.endpoint = endpoint.rstrip('/')
        self.timeout = timeout

    def screenshot(self, url: str, viewport=(1280, 1600), full_page: bool = True) -> bytes:
        """Render the URL and return PNG screenshot bytes."""
        try:
            resp = requests.post(
                f'{self.endpoint}/screenshot?token={self.api_key}',
                json={
                    'url': url,
                    'options': {'fullPage': full_page, 'type': 'png'},
                    'viewport': {'width': viewport[0], 'height': viewport[1]},
                    'waitFor': 1500,  # ms — let lazy-loaded gallery images appear
                    'gotoOptions': {'waitUntil': 'networkidle2', 'timeout': 30000},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ScreenshotError(f'Browserless HTTP error: {e}') from e

        if resp.status_code >= 400:
            raise ScreenshotError(f'Browserless HTTP {resp.status_code}: {resp.text[:200]}')
        return resp.content

    def get_rendered_html(self, url: str) -> str:
        """Return fully-rendered HTML (post-JS execution).

        Useful when you want DOM parsing on the *rendered* DOM rather than the
        static HTML. Costs roughly the same as a screenshot.
        """
        try:
            resp = requests.post(
                f'{self.endpoint}/content?token={self.api_key}',
                json={
                    'url': url,
                    'waitFor': 1500,
                    'gotoOptions': {'waitUntil': 'networkidle2', 'timeout': 30000},
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ScreenshotError(f'Browserless HTTP error: {e}') from e

        if resp.status_code >= 400:
            raise ScreenshotError(f'Browserless HTTP {resp.status_code}: {resp.text[:200]}')
        return resp.text


def encode_screenshot_b64(png_bytes: bytes) -> str:
    return base64.b64encode(png_bytes).decode('ascii')
