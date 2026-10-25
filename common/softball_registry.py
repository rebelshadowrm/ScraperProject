"""Complete initial softball scope, including explicit unavailable adapters."""
from importlib import import_module
from dataclasses import asdict, dataclass


PROVIDERS = {
    'usfa': ('scrapers.usfa_scraper', 'USFAScraper'),
    'usa_softball': ('scrapers.usa_softball_scraper', 'USASoftballScraper'),
    'fasa': ('scrapers.fasa_scraper', 'FASAScraper'),
    'nsa_fastpitch': ('scrapers.nsa_fastpitch_scraper', 'NSAFastpitchScraper'),
    'usa_fastpitch': ('scrapers.usa_fastpitch_scraper', 'USAFastpitchScraper'),
    'usssa_fastpitch': ('scrapers.usssa_fastpitch_scraper', 'USSSAFastpitchScraper'),
    'topgun_events': ('scrapers.topgun_events_scraper', 'TopGunEventsScraper'),
    'pgf': ('scrapers.pgf_scraper', 'PGFScraper'),
    'triplecrown_fastpitch': ('scrapers.triplecrown_fastpitch_scraper', 'TripleCrownFastpitchScraper'),
    'pg_softball': ('scrapers.pg_softball_scraper', 'PGSoftballScraper'),
    'alliance_fastpitch': ('scrapers.alliance_fastpitch_scraper', 'AllianceFastpitchScraper'),
    'topgun_sports': ('scrapers.topgun_fastpitch_scraper', 'TopGunFastpitchScraper'),
    'ncs_fastpitch': ('scrapers.ncs_fastpitch_scraper', 'NCSFastpitchScraper'),
}


def softball_registry():
    return {key: getattr(import_module(module), name) for key, (module, name) in PROVIDERS.items()}


@dataclass(frozen=True)
class ProviderProfile:
    discovery: str
    details: str
    family: str
    evidence: str
    priority: int


# Runtime strategy is explicit. A browser-rendered site can still have a JSON API;
# unresolved paths are not assigned Selenium just because their HTML is empty.
PROFILES = {
    'usa_fastpitch': ProviderProfile('api', 'api', 'registerplay', 'verified', 10),
    'usa_softball': ProviderProfile('api', 'api', 'registerplay', 'verified_state_team_search', 20),
    'nsa_fastpitch': ProviderProfile('api', 'api', 'registerplay', 'verified_state_period_search', 30),
    'alliance_fastpitch': ProviderProfile('api', 'html', 'wordpress_proxy', 'source_season_all_league_pages_verified_zero_ids_held', 40),
    'topgun_sports': ProviderProfile('api', 'api', 'topgun_feed', 'existing_access_gate', 50),
    'fasa': ProviderProfile('html', 'html', 'classic_asp', 'verified', 60),
    'usfa': ProviderProfile('html', 'html', 'usfa_events', 'ok_state_cards_verified', 70),
    'usssa_fastpitch': ProviderProfile('html', 'html+api', 'usssa_wordpress', 'published_state_directory_and_canonical_pages_verified', 80),
    'triplecrown_fastpitch': ProviderProfile('html', 'html', 'component_iframe_webflow', 'published_fastpitch_schedule_verified_alternate_layouts_held', 90),
    'topgun_events': ProviderProfile('html', 'html', 'tagup', 'published_query_and_pagination_verified', 100),
    'ncs_fastpitch': ProviderProfile('browser', 'browser', 'ncs', 'existing_access_and_date_gates', 110),
    'pg_softball': ProviderProfile('html', 'html', 'aspnet_published_form', 'published_form_and_server_paging_verified', 105),
    'pgf': ProviderProfile('api', 'api+html', 'tournamentconnect_pgf', 'current_public_read_verified', 45),
}


def softball_catalog(*, kind=None):
    """Read-only development order and evidence; never enables acquisition."""
    from common.softball_scaffold import SoftballScaffoldScraper
    from common.softball_national import NATIONAL_STRATEGIES
    if kind is not None and kind not in {'api', 'html', 'browser', 'unresolved'}:
        raise ValueError('Unknown acquisition kind')
    result = {}
    for key in sorted(PROFILES, key=lambda key: PROFILES[key].priority):
        profile = PROFILES[key]
        if kind is not None and profile.discovery != kind:
            continue
        module, name = PROVIDERS[key]
        cls = getattr(import_module(module), name)
        scaffold = issubclass(cls, SoftballScaffoldScraper)
        result[key] = dict(asdict(profile), scraper=name, sport=cls.SPORT,
            national_strategy=NATIONAL_STRATEGIES.get(key),
            national_coverage_certified=False,
            discovery_sample=(scaffold and bool(cls.DISCOVERY_SELECTOR)) or hasattr(cls,'collect_discovery_sample'),
            detail_inspection=bool(getattr(cls, 'DETAIL_API_VERIFIED', False)
                                   or getattr(cls, 'DETAIL_HTML_VERIFIED', False)),
            detail_extraction=cls.extract_event_details is not SoftballScaffoldScraper.extract_event_details,
            acquisition_ready=bool(getattr(cls, 'ACQUISITION_READY', False)),
            default_enabled=bool(cls.ENABLED_BY_DEFAULT), driver_required=bool(cls.REQUIRES_DRIVER),
            request_url=getattr(cls, 'REQUEST_URL', '') or getattr(cls, 'DISCOVERY_URL', ''),
            blocker=getattr(cls, 'BLOCKER', 'Existing source access/date verification remains required'))
    return result
