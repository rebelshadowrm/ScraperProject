from scrapers.play365_base import Play365GetScraper


class FiveToolPlay365Scraper(Play365GetScraper):
    SCRAPER_NAME = "FiveTool"
    DOMAIN = "https://events.fivetool.org"
    UTILITIES_SUBDIR = "Play365Utilities/FiveTool"
    DUMP_FILENAME = "fivetool_dump.json"
    SCRIPT_FILENAME = "export_fivetool.js"
    FORCE_SPORT_ID = None
