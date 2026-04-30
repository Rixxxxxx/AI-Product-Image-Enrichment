"""
Polite HTTP fetcher.

  * Per-domain rate limiting (in-process)
  * robots.txt compliance — cached per domain
  * Detects soft-404s and basic anti-bot challenge pages and skips them (never bypass)
  * Strips scripts/styles before returning HTML — saves Claude tokens
"""

import io
import ipaddress
import logging
import socket
import time
import threading
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


_BLOCKED_HOSTS = {
    'localhost', 'localhost.localdomain', 'metadata.google.internal',
    'metadata', 'instance-data',
}


def is_safe_external_url(url: str) -> bool:
    """SSRF guard: reject URLs that resolve to private / loopback / link-local addresses.

    Claude returns image URLs from arbitrary manufacturer pages. A malicious or
    prompt-injected page could return e.g. `http://169.254.169.254/...` (AWS IMDS)
    or `http://10.0.0.1/...`. We resolve the host and refuse if any resolved IP
    falls in a forbidden range.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ('http', 'https'):
        return False
    host = (parsed.hostname or '').lower().strip()
    if not host:
        return False
    if host in _BLOCKED_HOSTS:
        return False
    # If host is an IP literal, validate directly
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_public(ip)
    except ValueError:
        pass
    # Hostname — resolve. TOCTOU exists vs the actual HTTP request, but this
    # blocks the most common attack vectors (IMDS, RFC1918 literals in hostnames).
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if not _ip_is_public(ip):
            return False
    return True


def _ip_is_public(ip) -> bool:
    return not (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )

_logger = logging.getLogger(__name__)


class PageFetcher:

    def __init__(self, user_agent: str, request_delay_seconds: float = 2.0,
                 timeout: int = 20):
        self.user_agent = user_agent
        self.delay = float(request_delay_seconds or 2.0)
        self.timeout = timeout
        self._last_hit = {}  # host -> epoch seconds
        self._robots_cache = {}  # host -> RobotFileParser or False (fetch failed)
        self._lock = threading.Lock()

    # ---------- public ----------

    def can_fetch(self, url: str) -> bool:
        host = urlparse(url).netloc
        rp = self._get_robots(host, urlparse(url).scheme or 'https')
        if rp is False:  # robots.txt unreachable — be conservative? Most crawlers proceed.
            return True
        return rp.can_fetch(self.user_agent, url)

    def fetch(self, url: str):
        """Fetch HTML. Returns (html_text, soup) or (None, None) if blocked/failed."""
        if not is_safe_external_url(url):
            _logger.info('Refusing fetch of unsafe URL (SSRF guard): %s', url)
            return None, None
        if not self.can_fetch(url):
            _logger.info('robots.txt forbids %s', url)
            return None, None
        self._respect_delay(url)
        try:
            resp = requests.get(
                url,
                headers={'User-Agent': self.user_agent, 'Accept': 'text/html,*/*;q=0.8'},
                timeout=self.timeout,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            _logger.warning('Fetch failed %s: %s', url, e)
            return None, None

        if resp.status_code != 200 or not resp.text:
            return None, None

        ct = (resp.headers.get('Content-Type') or '').lower()
        if 'html' not in ct:
            return None, None

        if self._looks_like_anti_bot(resp.text):
            _logger.info('Anti-bot challenge detected on %s — skipping', url)
            return None, None

        soup = BeautifulSoup(resp.text, 'lxml')
        # Strip noise to reduce tokens for Claude
        for tag in soup(['script', 'style', 'noscript', 'svg', 'iframe']):
            tag.decompose()
        return resp.text, soup

    def download_image(self, url: str, max_bytes: int = 10 * 1024 * 1024):
        """Download an image (binary). Returns (bytes, mimetype) or (None, None)."""
        if not is_safe_external_url(url):
            _logger.info('Refusing image download of unsafe URL (SSRF guard): %s', url)
            return None, None
        self._respect_delay(url)
        try:
            with requests.get(
                url,
                headers={'User-Agent': self.user_agent},
                timeout=self.timeout,
                stream=True,
            ) as resp:
                if resp.status_code != 200:
                    return None, None
                ct = (resp.headers.get('Content-Type') or '').lower()
                if not (ct.startswith('image/') or url.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))):
                    return None, None
                buf = io.BytesIO()
                size = 0
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        _logger.info('Image too large, aborted: %s', url)
                        return None, None
                    buf.write(chunk)
                return buf.getvalue(), ct or 'image/jpeg'
        except requests.RequestException as e:
            _logger.warning('Image download failed %s: %s', url, e)
            return None, None

    # ---------- internals ----------

    def _respect_delay(self, url: str):
        host = urlparse(url).netloc
        with self._lock:
            last = self._last_hit.get(host, 0)
            wait = self.delay - (time.time() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.time()

    def _get_robots(self, host: str, scheme: str = 'https'):
        if host in self._robots_cache:
            return self._robots_cache[host]
        rp = RobotFileParser()
        rp.set_url(f'{scheme}://{host}/robots.txt')
        try:
            rp.read()
        except Exception as e:
            _logger.info('robots.txt unreachable for %s: %s', host, e)
            self._robots_cache[host] = False
            return False
        self._robots_cache[host] = rp
        return rp

    @staticmethod
    def _looks_like_anti_bot(html: str) -> bool:
        h = html.lower()
        markers = (
            'cf-chl-bypass', 'cf_chl_opt', 'checking your browser',
            '__cf_bm', 'akamai bot manager', 'incapsula incident',
            'please verify you are a human',
        )
        return any(m in h for m in markers)
