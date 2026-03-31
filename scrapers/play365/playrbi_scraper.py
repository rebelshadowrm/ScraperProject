from scrapers.play365_base import Play365PostScraper


class PlayRBIScraper(Play365PostScraper):
    SCRAPER_NAME = "PlayRBI"
    DOMAIN = "https://events.playrbi.com"
    UTILITIES_SUBDIR = "Play365Utilities/PlayRBI"
    DUMP_FILENAME = "playrbi_dump.json"
    SCRIPT_FILENAME = "export_playrbi.js"
    EXPORT_LAYOUT_DEFAULT = "full"
    EXPORT_EXTRA_PARAMS = {
        "organization_id": "",
    }
