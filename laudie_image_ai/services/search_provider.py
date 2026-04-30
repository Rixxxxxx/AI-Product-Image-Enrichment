"""
Web search abstraction.

Default: Brave Search API. Fallback options: SerpAPI, Google CSE, DuckDuckGo HTML.
DuckDuckGo HTML is included for completeness but is unreliable in production.
"""

import logging
import re
from urllib.parse import quote_plus, urlparse

import requests

_logger = logging.getLogger(__name__)


_BAD_DOMAINS = (
    'amazon.', 'ebay.', 'walmart.', 'aliexpress.', 'wish.', 'pinterest.',
    'reddit.', 'facebook.', 'youtube.com', 'tiktok.', 'instagram.',
)
_BAD_EXTENSIONS = ('.pdf', '.doc', '.xls', '.ppt')


class SearchResult:
    __slots__ = ('url', 'title', 'snippet', 'rank')

    def __init__(self, url, title='', snippet='', rank=0):
        self.url = url
        self.title = title
        self.snippet = snippet
        self.rank = rank


class SearchProvider:

    def __init__(self, provider: str, api_key: str = '', user_agent: str = 'LaudieImageBot/1.0'):
        self.provider = provider
        self.api_key = api_key or ''
        self.user_agent = user_agent

    # ---------- public ----------

    def search_product_page(self, product) -> list:
        """Build query and return up to ~10 candidate page URLs, manufacturer-domain-preferred."""
        manufacturer = (product._effective_manufacturer() or '').strip()
        sku = (product.laudie_manufacturer_sku or product.default_code or '').strip()
        name = (product.name or '').strip()

        if not (manufacturer or sku or name):
            return []

        # Most discriminating bits first
        parts = [manufacturer, sku, name]
        query = ' '.join(p for p in parts if p)[:200]

        try:
            results = self._dispatch(query)
        except Exception as e:
            _logger.warning('Search provider %s failed: %s', self.provider, e)
            return []

        return self._filter_and_rank(results, manufacturer)

    # ---------- providers ----------

    def _dispatch(self, query: str) -> list:
        if self.provider == 'brave':
            return self._brave(query)
        if self.provider == 'serpapi':
            return self._serpapi(query)
        if self.provider == 'google_cse':
            return self._google_cse(query)
        return self._ddg_html(query)

    def _brave(self, query: str) -> list:
        if not self.api_key:
            _logger.warning('Brave search: missing API key')
            return []
        resp = requests.get(
            'https://api.search.brave.com/res/v1/web/search',
            params={'q': query, 'count': 10, 'safesearch': 'moderate'},
            headers={
                'X-Subscription-Token': self.api_key,
                'Accept': 'application/json',
                'User-Agent': self.user_agent,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for i, item in enumerate((data.get('web') or {}).get('results') or []):
            results.append(SearchResult(
                url=item.get('url'),
                title=item.get('title') or '',
                snippet=item.get('description') or '',
                rank=i,
            ))
        return results

    def _serpapi(self, query: str) -> list:
        if not self.api_key:
            return []
        resp = requests.get(
            'https://serpapi.com/search.json',
            params={'q': query, 'api_key': self.api_key, 'num': 10, 'engine': 'google'},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for i, item in enumerate(data.get('organic_results') or []):
            results.append(SearchResult(
                url=item.get('link'),
                title=item.get('title') or '',
                snippet=item.get('snippet') or '',
                rank=i,
            ))
        return results

    def _google_cse(self, query: str) -> list:
        # api_key is "API_KEY:CX" combined
        if not self.api_key or ':' not in self.api_key:
            _logger.warning('Google CSE expects api_key formatted as "KEY:CX"')
            return []
        key, cx = self.api_key.split(':', 1)
        resp = requests.get(
            'https://www.googleapis.com/customsearch/v1',
            params={'q': query, 'key': key, 'cx': cx, 'num': 10},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for i, item in enumerate(data.get('items') or []):
            results.append(SearchResult(
                url=item.get('link'),
                title=item.get('title') or '',
                snippet=item.get('snippet') or '',
                rank=i,
            ))
        return results

    def _ddg_html(self, query: str) -> list:
        """Last-resort no-key fallback. Often blocked. Do not rely on this in production."""
        try:
            resp = requests.post(
                'https://html.duckduckgo.com/html/',
                data={'q': query},
                headers={'User-Agent': self.user_agent},
                timeout=15,
            )
            if resp.status_code != 200:
                return []
        except requests.RequestException:
            return []
        # Light parse — DDG HTML markup changes; do not invest much
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'lxml')
        results = []
        for i, a in enumerate(soup.select('a.result__a')[:10]):
            href = a.get('href')
            if not href:
                continue
            results.append(SearchResult(url=href, title=a.text, snippet='', rank=i))
        return results

    # ---------- filtering ----------

    def _filter_and_rank(self, results: list, manufacturer: str) -> list:
        manufacturer_l = (manufacturer or '').lower()
        keep = []
        for r in results:
            if not r.url:
                continue
            host = urlparse(r.url).netloc.lower()
            path = urlparse(r.url).path.lower()
            if any(b in host for b in _BAD_DOMAINS):
                continue
            if any(path.endswith(e) for e in _BAD_EXTENSIONS):
                continue
            keep.append(r)

        # Boost results whose host matches manufacturer name
        def _score(res):
            host = urlparse(res.url).netloc.lower()
            host_match = bool(manufacturer_l and re.sub(r'[^a-z0-9]', '', manufacturer_l) in re.sub(r'[^a-z0-9]', '', host))
            return (0 if host_match else 1, res.rank)

        keep.sort(key=_score)
        return keep
