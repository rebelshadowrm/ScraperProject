"""Preliminary USAFastpitch adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_api import RegisterPlaySoftballScraper


class USAFastpitchScraper(RegisterPlaySoftballScraper):
    SCRAPER_NAME = "USAFastpitch"
    DISCOVERY_URL = "https://usafastpitch.org/pages/events"
    REQUEST_URL = 'https://usafastpitch.org/api/v1/Event/display/active'
    CONTENT_TYPE = 'json'
    BLOCKER = "Bounded team rows verified; complete season/state search and missing venue/contact enrichment remain"
    DETAIL_API_VERIFIED = True
    SEARCH_API_VERIFIED = True
    EVENT_HOST = "usafastpitch.org"
    EVENT_PATH = "/pages/event/detail/(\\d+)/?"
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "api"
    REUSE = ""
