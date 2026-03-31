from scrapers.play365_base import Play365PostScraper


class Play24SportsScraper(Play365PostScraper):
    SCRAPER_NAME = "Play24Sports"
    DOMAIN = "https://go.play24sports.com"
    UTILITIES_SUBDIR = "Play365Utilities/Play24Sports"
    DUMP_FILENAME = "play24sports_dump.json"
    SCRIPT_FILENAME = "export_play24sports.js"
    EXPORT_LAYOUT_DEFAULT = "full"
    EXPORT_EXTRA_PARAMS = {
        "organization_id": "",
        "month": "",
        "name": "",
        "event_label_id": "",
        "division": "",
        "level": "",
        "venue_id": "",
    }
