import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from common.pull_options import PullOptions
from common.run_manifest import RunManifest
from ui import cli


class Good:
    SCRAPER_NAME = "Good"
    def close(self):
        pass


class Broken(Good):
    SCRAPER_NAME = "Broken"


class Dispatcher(Good):
    SCRAPER_NAME = "Play365Base"


class SnapshotRepairTests(unittest.TestCase):
    def test_test_snapshot_cannot_be_repaired_against_production_ledgers(self):
        with tempfile.TemporaryDirectory() as root:
            m = RunManifest(PullOptions("snapshot"), root=root, test_mode=True)
            m.provider("Good", "failed")
            m.finish()
            original = m.path.read_bytes()
            with self.assertRaisesRegex(ValueError, 'isolated test state'):
                RunManifest.load_for_repair(m.path)
            self.assertEqual(m.path.read_bytes(), original)

    def fixture(self, root):
        manifest = RunManifest(PullOptions("snapshot"), root=root)
        manifest.provider("Good", "partial", locations=["Texas", "Florida"])
        manifest.location("Good", "Texas", {"status": "complete", "rows_written": 8})
        manifest.location("Good", "Florida", {"status": "partial", "rows_written": 0})
        manifest.finish()
        return manifest

    def test_finished_same_day_only_and_read_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as root:
            m = self.fixture(root)
            original = m.path.read_bytes()
            self.assertEqual(RunManifest.load_for_repair(m.path).run_id, m.run_id)
            self.assertEqual(m.path.read_bytes(), original)
            for status, run_date in [("running", date.today()), ("complete", date.today()),
                                     ("partial", date.today() - timedelta(days=1))]:
                m.data.update(status=status, run_date=str(run_date))
                m.save()
                with self.assertRaises(ValueError):
                    RunManifest.load_for_repair(m.path)

    def test_only_incomplete_locations_retry_with_original_destination_and_history(self):
        with tempfile.TemporaryDirectory() as root:
            m = self.fixture(root)
            original = m.path.read_bytes()
            def run(**kwargs):
                self.assertEqual(kwargs["loc_info"], {"Florida": "fl"})
                self.assertEqual(kwargs["manifest"].output_root, m.output_root)
                self.assertEqual(kwargs["pull_options"], PullOptions("snapshot"))
                kwargs["manifest"].location("Good", "Florida", {"status": "complete", "rows_written": 3})
            with patch.object(cli, "JSON_FILE", str(Path(root) / "events.json")), \
                    patch.object(cli, "load_locations", return_value={}), \
                    patch.object(cli, "get_scraper_classes", return_value=[Good]), \
                    patch.object(cli, "_resolve_all_locations_for_scraper", side_effect=lambda c, s: ("ALL", {"Texas": "tx", "Florida": "fl"}, s)), \
                    patch.object(cli, "_run_selected_scraper_unlocked", side_effect=run) as retry, \
                    patch.object(cli, "shutdown_flag", False):
                result = cli._repair_snapshot(m.path)
            self.assertEqual(retry.call_count, 1)
            self.assertEqual(result.data["status"], "complete")
            self.assertEqual(result.data["locations"]["Good|Texas"]["rows_written"], 8)
            self.assertEqual(result.data["providers"]["Good"]["locations"], ["Texas", "Florida"])
            self.assertEqual(Path(result.data["repair_attempts"][0]["previous_manifest"]).read_bytes(), original)

    def test_discovery_failure_preserves_expected_work_and_other_providers_continue(self):
        with tempfile.TemporaryDirectory() as root:
            m = self.fixture(root)
            m.provider("Broken", "failed", locations=["Missing"])
            m.finish()
            def resolve(c, s):
                return "ALL", ({"Other": "url"} if isinstance(s, Broken) else {"Florida": "fl"}), s
            def run(**kwargs):
                kwargs["manifest"].location("Good", "Florida", {"status": "complete"})
            with patch.object(cli, "JSON_FILE", str(Path(root) / "events.json")), \
                    patch.object(cli, "load_locations", return_value={}), \
                    patch.object(cli, "get_scraper_classes", return_value=[Broken, Good]), \
                    patch.object(cli, "_resolve_all_locations_for_scraper", side_effect=resolve), \
                    patch.object(cli, "_run_selected_scraper_unlocked", side_effect=run) as retry, \
                    patch.object(cli, "shutdown_flag", False):
                result = cli._repair_snapshot(m.path)
            self.assertEqual(retry.call_count, 1)
            self.assertEqual(result.data["status"], "partial")
            self.assertEqual(result.data["providers"]["Broken"]["locations"], ["Missing"])
            self.assertEqual(result.data["providers"]["Good"]["status"], "complete")

    def test_dispatcher_retries_failed_children_and_keeps_full_expected_coverage(self):
        with tempfile.TemporaryDirectory() as root:
            m = RunManifest(PullOptions("snapshot"), root=root)
            done, failed = "Done (Play365Base)", "Failed (Play365Base)"
            m.provider("Dispatcher", "complete", locations=[done, failed])
            m.provider(done, "complete", locations=["Texas"])
            m.provider(failed, "queued", locations=["Florida"])
            m.location(done, "Texas", {"status": "complete", "rows_written": 7})
            m.finish()
            def run(**kwargs):
                self.assertEqual(kwargs["loc_info"], {failed: "new-ref"})
                kwargs["manifest"].provider(failed, "complete")
                kwargs["manifest"].location(failed, "Florida", {"status": "complete"})
            with patch.object(cli, "JSON_FILE", str(Path(root) / "events.json")), \
                    patch.object(cli, "load_locations", return_value={done: "old-ref", failed: "new-ref"}), \
                    patch.object(cli, "get_scraper_classes", return_value=[Dispatcher]), \
                    patch.object(cli, "_resolve_all_locations_for_scraper", side_effect=lambda c, s: ("ALL_PLAY365_SCRAPERS", c, s)), \
                    patch.object(cli, "_run_selected_scraper_unlocked", side_effect=run), \
                    patch.object(cli, "shutdown_flag", False):
                result = cli._repair_snapshot(m.path)
            self.assertEqual(result.data["status"], "complete")
            self.assertEqual(result.data["providers"]["Dispatcher"]["locations"], [done, failed])
            self.assertEqual(result.data["locations"][done + "|Texas"]["rows_written"], 7)

    def test_cli_rejects_new_settings_for_repair(self):
        with self.assertRaises(SystemExit) as raised:
            cli.run_cli(["--repair-snapshot", "example.json", "--end-date", "2027-01-13"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
