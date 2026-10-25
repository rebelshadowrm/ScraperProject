"""Reuse TopGun's structured parser only after recorded provider permission."""
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from scrapers.topgun_scraper import TopGunScraper, _safe_json_loads
from common.collection import CollectedLinks,validated_records


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

    def collect_discovery_sample(self,driver,base_url,*,max_events=2):
        if not 1<=max_events<=3:raise ValueError('TopGun sample is limited to 1..3 events')
        links=self.collect_event_links(driver,base_url)
        for url in links:self.get_event_identity(url)
        return CollectedLinks(links[:max_events],complete=False,pages=1,error='State-scoped shared-feed sample; no detail sweep')

    def get_event_identity(self,url):
        parsed=urlsplit(url)
        if (parsed.scheme!='https' or parsed.netloc!='topgunstats.com' or parsed.path!='/tournaments'
                or parse_qs(parsed.query).get('sport')!=['2'] or not re.fullmatch(r'tournament=[1-9]\d*',parsed.fragment)):
            raise ValueError('TopGun fastpitch needs a published numeric TournamentID and sport=2; fallback hashes are not accepted')
        return parsed.fragment

    def extract_event_details(self, url, driver=None, **kwargs):
        self.require_permission()
        identity=self.get_event_identity(url)
        record = self.tournaments_by_url.get(url)
        if record is None:
            # The only verified independent read is the existing feed. Do not
            # invent a targeted endpoint or bind extraction to prior discovery.
            self._load_filter_data()
            feed=self._tournaments_cache
            if feed is None:feed=self._get(self.TOURNAMENT_DATA_URL)
            if isinstance(feed,dict):
                field=next((name for name in ('Tournaments','tournaments','data') if name in feed),None)
                if field is None:raise ValueError('TopGun independent feed has no tournament list')
                feed=feed[field]
            records=validated_records(feed,source='TopGun independent details')
            matches=[item for item in records if str(item.get('TournamentID'))==identity.split('=',1)[1] and str(item.get('SportID'))=='2']
            if len(matches)!=1:raise ValueError('TopGun numeric fastpitch ID is missing or ambiguous in the feed')
            record=matches[0]
            self._tournaments_cache=records
            self.tournaments_by_url[url]=record
        if not record or str(record.get("SportID")) != "2":
            raise ValueError("TopGun fastpitch source record has the wrong sport")
        self.source_details[url] = {"divisions": _safe_json_loads(record.get("TFeeObj")) or [],
                                    "locations": _safe_json_loads(record.get("TComplexArr")) or []}
        return super().extract_event_details(url, driver, **kwargs)
