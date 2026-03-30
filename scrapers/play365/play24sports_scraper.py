from scrapers.play365_base import Play365BaseScraper


class Play24SportsScraper(Play365BaseScraper):
    SCRAPER_NAME = "Play24Sports"
    DOMAIN = "https://go.play24sports.com"
    UTILITIES_SUBDIR = "Play365Utilities/Play24Sports"
    DUMP_FILENAME = "play24sports_dump.json"
    SCRIPT_FILENAME = "export_play24sports.js"
    FORCE_SPORT_ID = 1