"""Preliminary USASoftball adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_api import RegisterPlaySoftballScraper


class USASoftballScraper(RegisterPlaySoftballScraper):
    SCRAPER_NAME = "USASoftball"
    DISCOVERY_URL = "https://www.usasoftball.com/tournaments-national-championships/"
    REQUEST_URL = 'https://usa.registerplay.com/api/v1/Event/display/active'
    DETAIL_API_VERIFIED = True
    SEARCH_API_VERIFIED = True
    DIRECTOR_FORM_VERIFIED = True
    DIRECTIONS_TEMPLATE_VERIFIED = True
    # Published searchParams.states tokens (2026-10-21). Cross-border Kansas
    # City and the source USA scope are searched without inventing a single state.
    SEARCH_STATE_ALIASES = {'TX-DFW':'TX','TX-Houston':'TX','OK-DevonPark':'OK',
        'CA-Central':'CA','CA-Northern':'CA','CA-Sacramento':'CA','CA-Southern':'CA',
        'MI-Detroit':'MI','MO-StLouis':'MO','MN-MPLS':'MN','NYC':'NY','OR-Portland':'OR','WA-Seattle':'WA'}
    SEARCH_MULTI_STATE_SCOPES = {'KSC':('KS','MO'),'USA':None}
    COVERAGE_LIMITATIONS = ['Published affiliate scopes require live transport verification; source USA scope may overlap state inventories']
    BLOCKER = "State team searches verified; affiliate/metro completeness, HS eligibility and optional source fields remain partial"
    EVENT_HOST = "usa.registerplay.com"
    EVENT_PATH = r'/pages/event/detail/(\d+)/?'
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "api"
    REUSE = ""
