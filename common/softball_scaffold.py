"""Discovery-only building blocks. Partial coverage never becomes empty success."""

import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from common.collection import CollectedLinks
from common.identity import normalize_url
from scrapers.base_scraper import BaseScraper


class SoftballNotReady(NotImplementedError):
    pass


def fetch_listing(url, *, session=None, content_type='html', method='GET', data=None, json_body=None, headers=None, redirect_paths=()):
    """One bounded GET or observed read-only POST; no retries or redirects."""
    from common.softball_transport import intercept
    options=dict(session=session,content_type=content_type,method=method,data=data,json_body=json_body,headers=headers)
    if redirect_paths:options['redirect_paths']=redirect_paths
    handled,value=intercept(url,options)
    if handled:return value
    owned = session is None
    session = session or requests.Session()
    started = time.monotonic()
    try:
        if method not in ('GET','POST'):raise ValueError('Unsupported source read method')
        request=session.get if method=='GET' else session.post
        kwargs=dict(timeout=(5,10),allow_redirects=False,stream=True)
        if headers:kwargs['headers']=headers
        if data is not None and json_body is not None:raise ValueError('Choose form or JSON source read body')
        if method=='POST':
            if json_body is not None:kwargs['json']=json_body
            else:kwargs['data']=data
        with request(url,**kwargs) as response:
            if response.status_code in (301,302,303) and redirect_paths:
                target=urljoin(url,response.headers.get('Location',''))
                origin=urlsplit(url);parsed=urlsplit(target)
                if (parsed.scheme!='https' or parsed.netloc!=origin.netloc or parsed.path not in redirect_paths
                        or parsed.username or parsed.password or parsed.fragment or target==url):
                    raise ValueError('Source form redirected outside its verified route')
                # One observed form redirect only; recursion does not allow another.
                return fetch_listing(target,session=session,content_type=content_type,headers=headers)
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Listing returned HTTP {response.status_code}; inspect redirect manually")
            if content_type not in response.headers.get('Content-Type', '').lower():
                raise ValueError(f"Expected a tournament {content_type} listing")
            data = bytearray()
            for chunk in response.iter_content(16384):
                data.extend(chunk)
                if len(data) > 4_000_000 or time.monotonic() - started > 20:
                    raise ValueError("Listing exceeded byte/time budget")
            return bytes(data).decode(response.encoding or 'utf-8', errors='replace')
    finally:
        if owned:
            session.close()


class SoftballScaffoldScraper(BaseScraper):
    SPORT = 'softball'
    ENABLED_BY_DEFAULT = False
    REQUIRES_DRIVER = False
    SUPPORTS_ALL_LOCATIONS = False
    SUPPORTS_SPECIFIC_LOCATIONS = False
    LOCATIONS_SOURCE = 'dynamic'
    DYNAMIC_LOCATIONS = True
    BATCH_DELAY_SECONDS = 5
    DISCOVERY_URL = ''
    REQUEST_URL = ''
    CONTENT_TYPE = 'html'
    BLOCKER = 'Discovery and detail contracts are not verified'
    # Only populated from observed detail URLs. Unknown sources have no guessed ID.
    EVENT_HOST = ''
    EVENT_PATH = ''
    EVENT_QUERY_ID = ''
    DISCOVERY_SELECTOR = ''
    REUSE = ''
    ACQUISITION_READY = False
    DETAIL_HTML_VERIFIED = False
    DETAIL_INSPECTION_REQUESTS = 1

    def __init__(self):
        self.discovery_revisions = {}

    def get_discovery_revision(self, url):
        return self.discovery_revisions.get(url)

    def get_dynamic_locations(self):
        return {f'Discovery sample ({self.SCRAPER_NAME})': self.DISCOVERY_URL}

    @classmethod
    def require_ready(cls):
        if (not cls.ACQUISITION_READY
                or cls.extract_event_details is SoftballScaffoldScraper.extract_event_details):
            raise SoftballNotReady(f'{cls.SCRAPER_NAME}: {cls.BLOCKER}; see .agents/skills/scraper-daily-run/references/coverage.md')

    @classmethod
    def require_permission(cls):
        # No new access gate. Existing NCS/TopGun permission checks live in their adapters.
        cls.require_ready()

    def get_event_identity(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != self.EVENT_HOST
                or parsed.username or parsed.password):
            raise ValueError('Unverified softball detail host')
        match = re.fullmatch(self.EVENT_PATH, parsed.path) if self.EVENT_PATH else None
        if not match:
            raise ValueError('Unverified softball detail route')
        if self.EVENT_QUERY_ID:
            values = parse_qs(parsed.query).get(self.EVENT_QUERY_ID, [])
            if len(values) != 1 or not re.fullmatch(r'(?:USFA)?\d+', values[0]):
                raise ValueError('Missing or ambiguous published event ID')
            return values[0]
        return match.group(1) if match.lastindex else normalize_url(url)

    def get_event_key(self, url):
        return self.get_event_identity(url)

    def canonical_detail_url(self, url):
        self.get_event_identity(url)
        parsed = urlsplit(url)
        query = urlencode({self.EVENT_QUERY_ID: parse_qs(parsed.query)[self.EVENT_QUERY_ID][0]}) if self.EVENT_QUERY_ID else ''
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ''))

    def parse_listing(self, html, base_url, *, max_events=5):
        if not 1 <= max_events <= 25:
            raise ValueError('Discovery sample limit must be 1..25')
        if not self.DISCOVERY_SELECTOR:
            raise SoftballNotReady(f'{self.SCRAPER_NAME}: {self.BLOCKER}')
        soup = BeautifulSoup(html, 'html.parser')
        self.discovery_revisions.clear()
        links, identities = [], set()
        for anchor in soup.select(self.DISCOVERY_SELECTOR):
            url = urljoin(base_url, anchor['href'])
            # Ignore navigation/foreign links, but reject malformed matching IDs.
            if urlsplit(url).hostname != self.EVENT_HOST:
                continue
            identity = self.get_event_identity(url)
            if identity in identities:
                continue
            identities.add(identity)
            links.append(self.canonical_detail_url(url))
            if len(links) == max_events:
                break
        if not links:
            raise SoftballNotReady('No verified detail links: empty, blocked, or changed listing needs inspection')
        return CollectedLinks(links, complete=False, pages=1,
            error='Bounded discovery sample only; pagination, coverage and details are not certified')

    def collect_event_links(self, driver, base_url, **kwargs):
        if base_url != self.DISCOVERY_URL:
            raise ValueError('Use the configured discovery URL; filters are not yet verified')
        if not self.DISCOVERY_SELECTOR:
            raise SoftballNotReady(f'{self.SCRAPER_NAME}: {self.BLOCKER}')
        return self.parse_listing(fetch_listing(self.REQUEST_URL or base_url, content_type=self.CONTENT_TYPE), base_url,
                                  max_events=kwargs.get('max_events', 5))

    def extract_event_details(self, url, driver=None, **kwargs):
        raise SoftballNotReady(f'{self.SCRAPER_NAME}: detail extraction is not implemented; {self.BLOCKER}')

    def inspect_event(self, url):
        self.get_event_identity(url)
        if not self.DETAIL_HTML_VERIFIED:
            raise SoftballNotReady('No verified HTML detail inspection contract')
        return self.inspect_detail_html(url, fetch_listing(url))

    def inspect_detail_html(self, url, html):
        raise SoftballNotReady('HTML detail inspection is not implemented')
