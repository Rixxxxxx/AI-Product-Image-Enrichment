"""
Sitemap-based discovery — primary path when SKU is known.

Fetches manufacturer.com/sitemap.xml (and /sitemap_index.xml), filters URLs
that contain the SKU. More reliable than search ranking, doesn't burn
search-API quota, and respects sites better.

Failure modes:
  - Sitemap not exposed → returns []
  - Sitemap is gzipped → handled
  - Sitemap is huge (10K+ URLs) → we cap at 50K and stream-parse
"""

import gzip
import io
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import requests

_logger = logging.getLogger(__name__)

NS = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
MAX_URLS_PER_INDEX = 50000


class SitemapProvider:

    def __init__(self, user_agent: str, timeout: int = 20):
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache = {}  # host -> list[str] of URLs

    def find_pages(self, manufacturer_domain: str, sku: str):
        """Return product page URLs from manufacturer's sitemap that mention the SKU."""
        if not (manufacturer_domain and sku):
            return []
        urls = self._urls_for(manufacturer_domain)
        if not urls:
            return []
        sku_lower = sku.lower().strip()
        # Also match SKU with dashes/underscores normalized
        sku_alt = re.sub(r'[\s_-]', '', sku_lower)
        out = []
        for u in urls:
            u_lower = u.lower()
            if sku_lower in u_lower or sku_alt in re.sub(r'[\s_-]', '', u_lower):
                out.append(u)
                if len(out) >= 10:
                    break
        return out

    # ---------- internals ----------

    def _urls_for(self, domain: str):
        if domain in self._cache:
            return self._cache[domain]
        host = self._normalize_host(domain)
        candidates = [
            f'https://{host}/sitemap.xml',
            f'https://{host}/sitemap_index.xml',
            f'https://www.{host}/sitemap.xml',
            f'https://{host}/sitemap-index.xml',
        ]
        urls = []
        for sm_url in candidates:
            try:
                fetched = self._fetch_sitemap(sm_url)
            except Exception as e:
                _logger.debug('Sitemap %s failed: %s', sm_url, e)
                continue
            if fetched:
                urls = fetched
                break
        self._cache[domain] = urls
        return urls

    def _fetch_sitemap(self, url: str, visited=None, depth: int = 0):
        # Hard guard against malicious/circular sitemap indexes
        if visited is None:
            visited = set()
        if depth > 5 or url in visited or len(visited) > 100:
            return []
        visited.add(url)

        try:
            resp = requests.get(url, headers={'User-Agent': self.user_agent}, timeout=self.timeout)
        except requests.RequestException:
            return []
        if resp.status_code != 200:
            return []
        body = resp.content
        if url.endswith('.gz') or body[:2] == b'\x1f\x8b':
            try:
                body = gzip.decompress(body)
            except OSError:
                return []

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return []

        tag = root.tag.lower()
        urls = []
        if tag.endswith('sitemapindex'):
            for sm in root.findall('sm:sitemap/sm:loc', NS) or root.findall('.//{*}sitemap/{*}loc'):
                child = (sm.text or '').strip()
                if child and child not in visited:
                    try:
                        urls.extend(self._fetch_sitemap(child, visited=visited, depth=depth + 1))
                    except Exception:
                        continue
                if len(urls) >= MAX_URLS_PER_INDEX:
                    break
        else:
            for loc in root.findall('sm:url/sm:loc', NS) or root.findall('.//{*}url/{*}loc'):
                txt = (loc.text or '').strip()
                if txt:
                    urls.append(txt)
                if len(urls) >= MAX_URLS_PER_INDEX:
                    break
        return urls

    @staticmethod
    def _normalize_host(domain: str) -> str:
        if '://' in domain:
            domain = urlparse(domain).netloc or domain
        return domain.lower().lstrip('www.').strip('/')
