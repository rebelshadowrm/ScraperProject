"""Preliminary NSAFastpitch adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_api import RegisterPlaySoftballScraper


class NSAFastpitchScraper(RegisterPlaySoftballScraper):
    SCRAPER_NAME = "NSAFastpitch"
    DISCOVERY_URL = "https://nationalsoftballassociation.com/pages/events"
    REQUEST_URL = 'https://nationalsoftballassociation.com/api/v1/Event/display/weekend'
    OTHER_SPORTS = ('SP',)  # Observed mixed feed; never admit slowpitch as fastpitch.
    DETAIL_API_VERIFIED = True
    SEARCH_API_VERIFIED = True
    BLOCKER = "Bounded weekend team rows verified; complete future search, pagination and field enrichment remain"
    EVENT_HOST = "nationalsoftballassociation.com"
    EVENT_PATH = r'/pages/event/detail/(\d+)/?'
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "api"
    REUSE = ""
