from scrapers.play365_base import Play365GetAllScraper


class FiveToolYouthScraper(Play365GetAllScraper):
    SCRAPER_NAME = "FiveToolYouth"
    DOMAIN = "https://play.fivetoolyouth.org"
    UTILITIES_SUBDIR = "Play365Utilities/FiveToolYouth"
    DUMP_FILENAME = "fivetoolyouth_dump.json"
    SCRIPT_FILENAME = "export_fivetoolyouth.js"
