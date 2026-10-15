"""Reuse TopGun's structured parser only after recorded provider permission."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scrapers.topgun_scraper import TopGunScraper, _safe_json_loads


class TopGunFastpitchScraper(TopGunScraper):
    SCRAPER_NAME = "TopGunFastpitch"
    SPORT = "softball"
    ENABLED_BY_DEFAULT = False

    def __init__(self):
        super().__init__()
        self.source_details = {}

    @staticmethod
    def require_permission():
        path = os.environ.get("SCRAPER_TOPGUN_FASTPITCH_PERMISSION_FILE")
        if not path or not Path(path).is_file() or not Path(path).read_text(encoding="utf-8").strip():
            raise RuntimeError("TopGun fastpitch is blocked by robots policy; record provider permission for "
                               "acquisition/redistribution in SCRAPER_TOPGUN_FASTPITCH_PERMISSION_FILE")

    def _get(self, url, params=None):
        self.require_permission()
        return super()._get(url, params)

    def get_dynamic_locations(self):
        self.require_permission()
        return {name.replace("(TopGun)", "(TopGunFastpitch)"): url.replace("sport=1", "sport=2")
                for name, url in super().get_dynamic_locations().items()}

    def collect_event_links(self, driver, base_url, **kwargs):
        self.require_permission()
        if parse_qs(urlsplit(base_url).query).get("sport") != ["2"]:
            raise ValueError("TopGun fastpitch requires the observed SportID=2 filter")
        return super().collect_event_links(driver, base_url, **kwargs)

    def extract_event_details(self, url, driver=None, **kwargs):
        self.require_permission()
        record = self.tournaments_by_url.get(url)
        if not record or str(record.get("SportID")) != "2":
            raise ValueError("TopGun fastpitch source record has the wrong sport")
        self.source_details[url] = {"divisions": _safe_json_loads(record.get("TFeeObj")) or [],
                                    "locations": _safe_json_loads(record.get("TComplexArr")) or []}
        return super().extract_event_details(url, driver, **kwargs)
