"""NCS fastpitch using the existing browser adapter; permission-gated rollout."""
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from us import states

from common.normalize import normalize_age_group
from scrapers.ncs_scraper import NCSScraper


class NCSFastpitchScraper(NCSScraper):
    SCRAPER_NAME = "NCSFastpitch"
    SPORT = "softball"
    ENABLED_BY_DEFAULT = False
    LOCATIONS_SOURCE = "dynamic"
    DYNAMIC_LOCATIONS = True
    BATCH_DELAY_SECONDS = 5

    def __init__(self):
        self._source_pages = {}
        self.source_details = {}

    @staticmethod
    def require_permission():
        path = os.environ.get("SCRAPER_NCS_FASTPITCH_PERMISSION_FILE")
        if not path or not Path(path).is_file() or not Path(path).read_text(encoding="utf-8").strip():
            raise RuntimeError("NCS fastpitch requires a local record of written acquisition/redistribution permission; "
                               "set SCRAPER_NCS_FASTPITCH_PERMISSION_FILE to that record")

    def get_dynamic_locations(self):
        self.require_permission()
        return {f"{state.name} (NCSFastpitch)":
                f"https://www.playncs.com/fastpitch/Events?countryState=US-{state.abbr}"
                for state in states.STATES}

    def _canonicalize_event_url(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in {"playncs.com", "www.playncs.com"}
                or parsed.username or parsed.password or not parsed.path.lower().startswith("/fastpitch/events")):
            raise ValueError("NCS fastpitch URL has an unexpected host or sport route")
        url = re.sub(r"/fastpitch/events", "/fastpitch/Events", url, flags=re.I)
        return super()._canonicalize_event_url(url)

    def get_event_identity(self, url):
        match = re.search(r"/Events/(?:Details|Divisions|Locations|Contacts)/(\d+)(?:/|$)",
                          self._canonicalize_event_url(url))
        if not match:
            raise ValueError("NCS fastpitch event has no published numeric ID")
        return match.group(1)

    def _preserve_base_query_params(self, next_url, base_url):
        url = super()._preserve_base_query_params(next_url, base_url)
        self._canonicalize_event_url(url)
        if parse_qs(urlsplit(url).query).get("countryState") != parse_qs(urlsplit(base_url).query).get("countryState"):
            raise ValueError("NCS pagination changed the selected state")
        return url

    def collect_event_links(self, driver, base_url, **kwargs):
        self.require_permission()
        self._canonicalize_event_url(base_url)
        country_state = parse_qs(urlsplit(base_url).query).get("countryState", [])
        if len(country_state) != 1 or not re.fullmatch(r"US-[A-Z]{2}", country_state[0]):
            raise ValueError("NCS fastpitch discovery requires the observed countryState filter")
        links = super().collect_event_links(driver, base_url, **kwargs)
        current = getattr(driver, "current_url", "")
        if current:
            self._canonicalize_event_url(current)
        return links

    def _parse_details_page(self, soup):
        date_tags = soup.select("hgroup > h3.inline-block.m0")
        raw = date_tags[-1].get_text(" ", strip=True) if date_tags else ""
        if not re.search(r"\b\d{4}\b", raw):
            raise ValueError("NCS fastpitch source date has no explicit year; obtain a permitted dated feed")
        return super()._parse_details_page(soup)

    def _get_soup(self, driver, page_url, **kwargs):
        # Do not certify missing fee/contact/venue pages from best-effort HTML.
        kwargs["soft_timeout"] = False
        soup = super()._get_soup(driver, page_url, **kwargs)
        if getattr(driver, "current_url", ""):
            self._canonicalize_event_url(driver.current_url)
        self._source_pages[page_url] = soup
        return soup

    def extract_event_details(self, url, driver, **kwargs):
        self.require_permission()
        self._source_pages.clear()
        row = super().extract_event_details(url, driver, **kwargs)
        if not row:
            return row
        details_url = self._force_details_url(url)
        divisions = self._source_pages[details_url.replace("/Details/", "/Divisions/")]
        locations = self._source_pages[details_url.replace("/Details/", "/Locations/")]
        source_divisions = []
        for panel in divisions.select("#divisionsContainer .panel"):
            label = panel.select_one(".division")
            if label is None:
                continue
            label = label.get_text(" ", strip=True)
            amount = panel.select_one(".amount")
            fee = amount.get_text(" ", strip=True) if amount else ""
            from common.age_groups import age_tokens
            source_divisions.append({"label": label, "age_groups": age_tokens(label), "entry_fee": fee,
                "restrictions": panel.select_one(".locations").get_text(" ", strip=True)
                if panel.select_one(".locations") else ""})
        source_locations = [{key: site.get("data-" + attr, "") for key, attr in
                             (("venue", "title"), ("address", "line1"), ("city_state_zip", "line2"), ("state", "state"))}
                            for site in locations.select("ul.list-sites li[data-title]")]
        for index in range(1, 14):
            row[f"Age-groups.{index}"] = ""
            row[f"Entry-fee.{index}"] = ""
        for index, division in enumerate(source_divisions[:13], 1):
            row[f"Age-groups.{index}"] = normalize_age_group(division["label"])
            row[f"Entry-fee.{index}"] = division["entry_fee"]
        # Preserve B/C/Rec labels, unknown eligibility and every venue in rich
        # canonical metadata, without extending the baseball dropdown contract.
        self.source_details[url] = {"divisions": source_divisions, "locations": source_locations}
        row["More-information-and-additional-tournament-locations"] = " | ".join(
            [f"{item['label']}: {item['entry_fee']} {item['restrictions']}" for item in source_divisions]
            + ["; ".join(item.values()) for item in source_locations])
        return row
