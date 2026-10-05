import logging
import tempfile
import unittest
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from selenium.common.exceptions import TimeoutException

from common.collection import CollectedLinks
from common.pull_options import PullOptions
from common.run_manifest import RunManifest
from common.runner import process_location
from common.config import OUTPUT_COLUMNS
from common.scheduler_history import load_scheduler_history
from common.driver_manager import initialize_driver
from scrapers.ncs_scraper import NCSScraper
from scrapers.usssa_scraper import USSSAScraper
from scrapers.play365.fivetool_scraper import FiveToolPlay365Scraper
from scrapers.play365_base import Play365ManualFallbackRequired
from scrapers.topgun_scraper import TopGunScraper
from test_pull_recovery import FakeScraper
from ui import cli


class ResilienceTests(unittest.TestCase):
    def test_browser_memory_failure_restarts_discovery_once(self):
        from selenium.common.exceptions import WebDriverException
        from common.driver_manager import is_driver_connection_error
        self.assertTrue(is_driver_connection_error(WebDriverException('out of memory')))
        self.assertFalse(is_driver_connection_error(ValueError('source text says out of memory')))
        with tempfile.TemporaryDirectory() as temporary:
            root, scraper = Path(temporary), FakeScraper()
            scraper.REQUIRES_DRIVER = True
            with patch.object(scraper, 'collect_event_links', side_effect=[WebDriverException('out of memory'), CollectedLinks()]) as collect, patch('common.runner.restart_driver', return_value='fresh-driver') as restart:
                driver = process_location(location_name='Texas', base_url='https://example.com', scraper=scraper,
                                          driver='old-driver', events_data={'events': {}}, failed_data={'failed_events': []},
                                          output_columns=OUTPUT_COLUMNS, json_file=str(root / 'events.json'),
                                          failed_events_file=str(root / 'failed.json'), download_dir='', shutdown_check=lambda: False,
                                          output_base_folder=str(root / 'Sheets'), scheduler_history_file=str(root / 'history.json'))
            self.assertEqual(driver, 'fresh-driver')
            self.assertEqual(collect.call_count, 2)
            restart.assert_called_once()
            self.assertEqual(scraper._last_location_result['status'], 'complete')

    def test_listing_date_skips_remain_in_manifest_accounting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, scraper = Path(temporary), FakeScraper()
            with patch.object(scraper, 'collect_event_links', return_value=CollectedLinks(non_future_skipped=7)):
                process_location(location_name='Texas', base_url='https://example.com', scraper=scraper,
                                 driver=None, events_data={'events': {}}, failed_data={'failed_events': []},
                                 output_columns=OUTPUT_COLUMNS, json_file=str(root / 'events.json'),
                                 failed_events_file=str(root / 'failed.json'), download_dir='', shutdown_check=lambda: False,
                                 output_base_folder=str(root / 'Sheets'), scheduler_history_file=str(root / 'history.json'))
            self.assertEqual(scraper._last_location_result['non_future_skipped'], 7)
            self.assertEqual(scraper._last_location_result['links_collected'], 7)
            self.assertEqual(scraper._last_location_result['status'], 'complete')

    def test_usssa_timeout_does_not_look_like_empty_success(self):
        scraper = USSSAScraper()
        driver = MagicMock()
        with patch.object(scraper, "_wait_for_listing_state", side_effect=TimeoutException("listing")):
            with self.assertRaises(TimeoutException):
                scraper.collect_event_links(driver, "https://example.com")

    def test_ncs_timeout_marks_collection_incomplete(self):
        driver = MagicMock(page_source="<html>Loading</html>")
        driver.get.side_effect = TimeoutException("listing")
        links = NCSScraper().collect_event_links(driver, "https://example.com", pause=0)
        self.assertFalse(links.complete)
        self.assertIn("timeout", links.error)

    def test_collection_failure_is_recorded_in_location_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scraper = FakeScraper()
            with patch.object(scraper, "collect_event_links", return_value=CollectedLinks(complete=False, error="missing page")):
                process_location(location_name="Texas", base_url="https://example.com", scraper=scraper,
                                 driver=None, events_data={"events": {}}, failed_data={"failed_events": []},
                                 output_columns=OUTPUT_COLUMNS, json_file=str(root / "events.json"),
                                 failed_events_file=str(root / "failed.json"), download_dir="", shutdown_check=lambda: False,
                                 output_base_folder=str(root / "Sheets"), scheduler_history_file=str(root / "history.json"))
            entry = next(iter(load_scheduler_history(root / "history.json")["locations"].values()))
            self.assertTrue(entry["last_failed"])
            self.assertEqual(entry["recent_runs"][-1]["collection_error"], "missing page")

    def test_failed_provider_discovery_does_not_stop_next_provider(self):
        class Broken(FakeScraper):
            SCRAPER_NAME = "Broken"
        class Working(FakeScraper):
            SCRAPER_NAME = "Working"
        with tempfile.TemporaryDirectory() as temporary:
            def manifest(options, **kwargs):
                return RunManifest(options, root=temporary)
            def resolve(locations, scraper):
                if isinstance(scraper, Broken):
                    raise RuntimeError("discovery unavailable")
                return "ALL", {"Texas": "https://example.com"}, scraper
            with patch.object(cli, "RunManifest", side_effect=manifest), \
                    patch.object(cli, "load_locations", return_value={}), \
                    patch.object(cli, "get_scraper_classes", return_value=[Broken, Working]), \
                    patch.object(cli, "_resolve_all_locations_for_scraper", side_effect=resolve), \
                    patch.object(cli, "_run_selected_scraper") as run, patch.object(cli, "shutdown_flag", False):
                result = cli._run_scrape_flow(all_scrapers=True)
            self.assertEqual(run.call_count, 1)
            self.assertIsInstance(run.call_args.kwargs["scraper"], Working)
            self.assertEqual(result.data["providers"]["Broken"]["status"], "failed")
            self.assertEqual(result.data["status"], "partial")
            self.assertEqual(result.data["mode"], "snapshot")

    def test_failed_middle_location_does_not_stop_later_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = RunManifest(PullOptions(), root=root)
            calls = []
            def location(**kwargs):
                calls.append(kwargs["location_name"])
                if kwargs["location_name"] == "B":
                    raise RuntimeError("location unavailable")
                return None
            with patch.multiple(cli, JSON_FILE=str(root / "events.json"), FAILED_EVENTS_FILE=str(root / "failed.json"),
                                SCHEDULER_HISTORY_FILE=str(root / "history.json"), shutdown_flag=False), \
                    patch.object(cli, "setup_logger", return_value=logging.getLogger("test")), \
                    patch.object(cli, "build_run_log_path", return_value=str(root / "test.log")), \
                    patch.object(cli, "flush_logs"), patch.object(cli, "process_location", side_effect=location):
                cli._run_selected_scraper_unlocked(scraper=FakeScraper(), selected_location="ALL",
                                                  loc_info={key: "https://example.com" for key in ("A", "B", "C")},
                                                  manifest=manifest)
            self.assertEqual(calls, ["A", "B", "C"])
            self.assertEqual(manifest.data["locations"]["FakeScraper|B"]["status"], "failed")

    def test_unattended_play365_queues_manual_work_and_continues(self):
        with tempfile.TemporaryDirectory() as temporary:
            manifest = RunManifest(PullOptions(), root=temporary)
            scrapers = [MagicMock(REQUIRES_DRIVER=False, SCRAPER_NAME=name) for name in ("A", "B")]
            for scraper in scrapers:
                scraper.get_dynamic_locations.return_value = {"Texas": "https://example.com"}
            with patch.object(cli, "_load_scraper_from_reference", side_effect=scrapers), \
                    patch.object(cli, "_process_location_safe", side_effect=[Play365ManualFallbackRequired("A"), None]), \
                    patch("builtins.input", side_effect=AssertionError("unattended prompt")), patch.object(cli, "shutdown_flag", False):
                cli._run_play365_batch(play365_targets={"A": "a:A", "B": "b:B"}, events_data={"events": {}},
                                       failed_data={"failed_events": []}, json_file="unused", failed_events_file="unused",
                                       output_base_folder=temporary, scheduler_history_file="unused", run_mode="test",
                                       manifest=manifest, unattended=True)
            self.assertEqual(manifest.data["providers"]["A"]["status"], "queued")
            self.assertEqual(manifest.data["providers"]["B"]["status"], "complete")

    def test_play365_empty_success_is_loaded_once(self):
        scraper = FiveToolPlay365Scraper()
        with patch.object(scraper, "_try_load_events_from_api", return_value=True) as load:
            for url in list(scraper.get_dynamic_locations().values())[:3]:
                self.assertEqual(scraper.collect_event_links(None, url, allow_manual_fallback=False), [])
        self.assertEqual(load.call_count, 1)

    def test_play365_repeated_page_is_bounded_and_not_cached_as_success(self):
        scraper = FiveToolPlay365Scraper()
        scraper.EXPORT_SUPPORTS_STATE_FILTER = False
        session = MagicMock()
        session.get.return_value.json.return_value = {'csrf_token': 'fixture-fresh-token'}
        session.post.return_value.json.return_value = [{"url_key": "one", "name": "Tournament"}]
        with patch("scrapers.play365_base.requests.Session", return_value=session):
            self.assertFalse(scraper._try_load_events_from_api())
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.post.call_count, 2)
        self.assertFalse(scraper._events_loaded)
        session.close.assert_called_once()

    def test_play365_malformed_response_is_not_empty_success(self):
        scraper = FiveToolPlay365Scraper()
        scraper.EXPORT_SUPPORTS_STATE_FILTER = False
        session = MagicMock()
        session.get.return_value.json.return_value = {'csrf_token': 'fixture-fresh-token'}
        session.post.return_value.json.return_value = {"error": "blocked"}
        with patch("scrapers.play365_base.requests.Session", return_value=session):
            self.assertFalse(scraper._try_load_events_from_api())
        self.assertFalse(scraper._events_loaded)

    def test_topgun_empty_feed_is_loaded_once_and_malformed_feed_rejected(self):
        scraper = TopGunScraper()
        scraper._filter_loaded = True
        scraper._state_to_location_ids = {"TX": [1], "OK": [2]}
        with patch.object(scraper, "_get", return_value=[]) as load:
            scraper.collect_event_links(None, "https://example.com?state=TX")
            scraper.collect_event_links(None, "https://example.com?state=OK")
        self.assertEqual(load.call_count, 1)
        scraper._tournaments_cache = None
        with patch.object(scraper, "_get", return_value={"error": "blocked"}):
            with self.assertRaises(ValueError):
                scraper.collect_event_links(None, "https://example.com?state=TX")

    def test_manual_dump_freshness_rejects_old_payload_even_if_file_is_recent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "dump.json"
            path.write_text(json.dumps({"generated_at": "2020-01-01T00:00:00Z", "pages": []}))
            scraper = FiveToolPlay365Scraper()
            self.assertFalse(scraper._dump_is_fresh(path))
            path.write_text(json.dumps({"pages": []}))
            self.assertTrue(scraper._dump_is_fresh(path))
            os.utime(path, (time.time() - 172800, time.time() - 172800))
            self.assertFalse(scraper._dump_is_fresh(path))

    def test_firefox_uses_real_headless_argument_and_bounded_timeouts(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch("common.driver_manager.GeckoDriverManager") as manager, \
                patch("common.driver_manager.webdriver.Firefox") as firefox:
            manager.return_value.install.return_value = "geckodriver.exe"
            driver = initialize_driver(download_dir=temporary)
        self.assertIn("-headless", firefox.call_args.kwargs["options"].arguments)
        driver.set_page_load_timeout.assert_called_once_with(60)
        driver.set_script_timeout.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
