import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from common.identity import event_identity
from common.json_utils import load_events
from common.pull_options import PullOptions
from common.runner import process_location
from scrapers.base_scraper import BaseScraper
from scrapers.ncs_fastpitch_scraper import NCSFastpitchScraper
from scrapers.ncs_scraper import NCSScraper
from scrapers.topgun_fastpitch_scraper import TopGunFastpitchScraper


class FixtureDriver:
    def __init__(self):
        self.current_url = ""
        self.page_source = ""

    def get(self, url):
        self.current_url = url
        if "/Details/" in url:
            self.page_source = '<hgroup><h1 class="mv-sm"><strong>Fastpitch Classic</strong></h1><h3 class="inline-block m0">Oct 20-21, 2099</h3></hgroup>'
        elif "/Divisions/" in url:
            self.page_source = '''<div id="divisionsContainer"><div class="panel"><div class="division">12U C</div>
                <div class="amount">$190</div><div class="locations">$10 Individual Gate Fee</div></div>
                <div class="panel"><div class="division">14U B</div><div class="amount">$250</div></div></div>'''
        elif "/Locations/" in url:
            self.page_source = '''<ul class="list-sites"><li data-title="Veterans Park" data-line1="100 Main St"
                data-line2="Irving, TX 75060" data-state="TX"><h5 class="title">Veterans Park</h5></li>
                <li data-title="Second Park" data-line1="200 Main St" data-line2="Irving, TX 75060" data-state="TX"></li></ul>'''
        elif "/Contacts/" in url:
            self.page_source = '''<ul class="list-contacts"><li><h5 class="title">Public Organizer</h5><address>
                <a href="mailto:organizer@example.org">Email</a><a href="tel:5125550100">Phone</a></address></li></ul>'''
        else:
            self.page_source = '<div class="media-body"><div class="h4"><a href="/fastpitch/Events/Details/42/classic">Classic</a></div></div>'

    def find_elements(self, by, value):
        return BeautifulSoup(self.page_source, "html.parser").select(value)

    def find_element(self, by, value):
        return self.find_elements(by, value)[0]


class SoftballTests(unittest.TestCase):
    def test_permission_gate_and_default_discovery(self):
        from common.prompt_utils import get_scraper_classes
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "written"):
                NCSFastpitchScraper.require_permission()
        self.assertNotIn(NCSFastpitchScraper, get_scraper_classes())
        self.assertNotIn(TopGunFastpitchScraper, get_scraper_classes())

    def test_topgun_shared_feed_filters_sports_and_preserves_ids(self):
        import json
        from common.config import OUTPUT_COLUMNS
        scraper = TopGunFastpitchScraper()
        record = dict(TournamentID=99, SportID=2, LocationID=5, TournamentTypeID=7,
                      TournamentName="Softball Classic", StartDate="2099-10-20T00:00:00.000Z",
                      EndDate="2099-10-21T00:00:00.000Z", FullName="Public Organizer",
                      Email="organizer@example.org", CellPhone="5125550100",
                      TFeeObj=json.dumps([{"Fee": "450", "AgeGroups": ["12U", "14U"]}]))
        scraper._tournaments_cache = [record, dict(record, SportID=1, TournamentName="Baseball Classic")]
        scraper._filter_loaded = True
        scraper._state_to_location_ids = {"TX": [5]}
        scraper._type_id_to_name = {7: "Team"}
        self.addCleanup(scraper.close)
        with patch.object(scraper, "require_permission"):
            links = scraper.collect_event_links(None, "https://topgunstats.com/tournaments?state=TX&sport=2")
            self.assertEqual(len(links), 1)
            row = scraper.extract_event_details(links[0])
            self.assertEqual(set(row), set(OUTPUT_COLUMNS))
            self.assertEqual(row["Tournament-name"], "Softball Classic")
            self.assertEqual(row["Entry-fee.1"], "450")
            self.assertEqual(event_identity(scraper, links[0]), "TopGunFastpitchScraper|softball|tournament=99")
            with self.assertRaises(ValueError):
                scraper.collect_event_links(None, "https://topgunstats.com/tournaments?state=TX&sport=1")

    def test_provider_access_blocker_does_not_suppress_other_authorized_provider(self):
        from common.provider_access import softball_classes
        from unittest.mock import Mock
        manifest = Mock()
        with patch.object(NCSFastpitchScraper, "require_permission", side_effect=RuntimeError("blocked")), \
                patch.object(TopGunFastpitchScraper, "require_permission"):
            self.assertEqual(softball_classes(manifest), [TopGunFastpitchScraper])
        manifest.provider.assert_called_once_with("NCSFastpitchScraper", "queued", error="blocked")

    def test_id_and_route_separate_sports_preserve_baseball_identity(self):
        baseball = "https://playncs.com/baseball/Events/Details/42/classic"
        softball = "https://playncs.com/fastpitch/Events/Details/42/classic"
        self.assertEqual(event_identity(NCSScraper(), baseball), "NCSScraper|" + baseball)
        self.assertEqual(event_identity(NCSFastpitchScraper(), softball), "NCSFastpitchScraper|softball|42")
        with self.assertRaises(ValueError):
            event_identity(NCSFastpitchScraper(), baseball)
        with self.assertRaises(ValueError):
            NCSFastpitchScraper()._preserve_base_query_params(
                "/fastpitch/Events?page=2&countryState=US-AZ", "https://playncs.com/fastpitch/Events?countryState=US-TX")

    def test_missing_year_cannot_invent_fastpitch_season_dates(self):
        soup = BeautifulSoup('<hgroup><h1 class="mv-sm">2027 Season Classic</h1>'
                             '<h3 class="inline-block m0">Oct 17-18</h3></hgroup>', "html.parser")
        with self.assertRaisesRegex(ValueError, "explicit year"):
            NCSFastpitchScraper()._parse_details_page(soup)

    def test_full_shared_pipeline_preserves_fees_contacts_and_all_venues_without_excel(self):
        scraper = NCSFastpitchScraper()
        scraper.BATCH_DELAY_SECONDS = 0
        with tempfile.TemporaryDirectory() as temporary, patch.object(scraper, "require_permission"):
            root = Path(temporary)
            process_location(location_name="Texas (NCSFastpitch)",
                base_url="https://playncs.com/fastpitch/Events?countryState=US-TX", scraper=scraper,
                driver=FixtureDriver(), events_data={"events": {}}, failed_data={"failed_events": []},
                output_columns=__import__("common.config", fromlist=["OUTPUT_COLUMNS"]).OUTPUT_COLUMNS,
                json_file=str(root / "events.json"), failed_events_file=str(root / "failed.json"),
                download_dir=str(root / "downloads"), shutdown_check=lambda: False,
                output_base_folder=str(root / "Sheets"), scheduler_history_file=str(root / "history.json"),
                pull_options=PullOptions("snapshot"))
            entry = load_events(root / "events.json")["events"]["NCSFastpitchScraper|softball|42"]
            self.assertEqual(entry["sport"], "softball")
            self.assertEqual(entry["source_event_id"], "42")
            self.assertEqual(entry["event_data"]["Entry-fee.1"], "$190")
            self.assertEqual(entry["event_data"]["Age-groups.2"], "14u")
            self.assertEqual(entry["event_data"]["Email"], "organizer@example.org")
            self.assertEqual(entry["source_details"]["divisions"][1]["label"], "14U B")
            self.assertEqual(len(entry["source_details"]["locations"]), 2)
            self.assertFalse(list(root.rglob("*.xlsx")))
            self.assertFalse(entry["processed"])
            self.assertEqual(entry["acquisition_status"], "ready")


if __name__ == "__main__":
    unittest.main()
