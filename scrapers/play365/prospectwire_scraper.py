from scrapers.play365_base import Play365PostScraper


class ProspectWireScraper(Play365PostScraper):
    SCRAPER_NAME = "ProspectWire"
    DOMAIN = "https://play.prospectwire.com"
    UTILITIES_SUBDIR = "Play365Utilities/ProspectWire"
    DUMP_FILENAME = "prospectwire_dump.json"
    SCRIPT_FILENAME = "export_prospectwire.js"
    EXPORT_LAYOUT_DEFAULT = "full"
    EXPORT_EXTRA_PARAMS = {
        "organization_id": "",
    }
