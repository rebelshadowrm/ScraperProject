from scrapers.play365_base import Play365GetAllScraper


class TwoDSportsYouthScraper(Play365GetAllScraper):
    SCRAPER_NAME = "2DSportsYouth"
    DOMAIN = "https://youth.2dsports.org"
    UTILITIES_SUBDIR = "Play365Utilities/2DSportsYouth"
    DUMP_FILENAME = "2dsports_youth_dump.json"
    SCRIPT_FILENAME = "export_2dsports_youth.js"
