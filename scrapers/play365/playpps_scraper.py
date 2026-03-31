from scrapers.play365_base import Play365PostScraper


class PlayPPSScraper(Play365PostScraper):
    SCRAPER_NAME = "PlayPPS"
    DOMAIN = "https://baseball.playpps.com"
    UTILITIES_SUBDIR = "Play365Utilities/PlayPPS"
    DUMP_FILENAME = "playpps_dump.json"
    SCRIPT_FILENAME = "export_playpps.js"
    EXPORT_LAYOUT_DEFAULT = "medium"
    EXPORT_EXTRA_PARAMS = {
        "organization_id": "",
    }
