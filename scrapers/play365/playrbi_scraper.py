from scrapers.play365_base import Play365BaseScraper


class PlayPPSScraper(Play365BaseScraper):
    SCRAPER_NAME = "PlayPPS"
    DOMAIN = "https://baseball.playpps.com"
    UTILITIES_SUBDIR = "Play365Utilities/PlayPPS"
    DUMP_FILENAME = "playpps_dump.json"
    SCRIPT_FILENAME = "export_playpps.js"
    FORCE_SPORT_ID = 1