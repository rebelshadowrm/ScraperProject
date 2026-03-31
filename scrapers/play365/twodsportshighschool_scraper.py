from scrapers.play365_base import Play365GetAllScraper


class TwoDSportsHighSchoolScraper(Play365GetAllScraper):
    SCRAPER_NAME = "2DSportsHighSchool"
    DOMAIN = "https://play.2dsports.org"
    UTILITIES_SUBDIR = "Play365Utilities/2DSportsHighSchool"
    DUMP_FILENAME = "2dsports_highschool_dump.json"
    SCRIPT_FILENAME = "export_2dsports_highschool.js"
    PREPARE_DUMP_IN_PROMPT = False
    DIRECT_API_ENABLED = True
