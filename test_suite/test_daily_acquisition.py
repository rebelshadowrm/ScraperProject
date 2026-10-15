import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from common.process_budget import run_bounded
from common.storage import ledger_lock
from scripts import daily_acquisition as daily


class DailyTests(unittest.TestCase):
    def test_dry_run_is_noninteractive_without_mutation_or_network(self):
        with patch.object(daily, "run_bounded", side_effect=AssertionError("unexpected child")), \
                patch.object(daily, "ledger_lock", side_effect=AssertionError("unexpected lock")), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(daily.main(["--dry-run"]), 0)
        plan = json.loads(output.getvalue())
        self.assertIn("--all", plan["acquisition"])
        self.assertNotIn("--interactive-fallback", plan["acquisition"])
        self.assertEqual(plan["network_requests"], 0)
        self.assertEqual(plan["timezone"], "America/Chicago")

    def test_owned_process_timeout_and_exit_status_are_real(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = run_bounded([sys.executable, "-c", "raise SystemExit(7)"], cwd=root,
                stdout=root / "out", stderr=root / "err", timeout=10)
            self.assertEqual(result["exit_code"], 7)
            result = run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], cwd=root,
                stdout=root / "out", stderr=root / "err", timeout=0.2)
            self.assertEqual(result, {"exit_code": 124, "timed_out": True})

    def test_cli_wrapper_propagates_status_without_using_local_main_changes(self):
        wrapper = Path(daily.__file__).resolve().with_name("acquire_unattended.py")
        code = "import runpy,sys; from unittest.mock import patch; " \
               "sys.path.insert(0,sys.argv[2]); " \
               "mock=patch('ui.cli.run_cli',return_value=7); mock.start(); " \
               "runpy.run_path(sys.argv[1],run_name='__main__')"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-c", code, str(wrapper), str(wrapper.parents[1])],
                                    cwd=temporary, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 7, result.stderr.decode(errors="replace"))

    def test_failure_does_not_suppress_delivery_and_reports_missed_days(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(daily, "ROOT", Path(temporary)):
            calls = []
            def bounded(command, **kwargs):
                calls.append(command)
                return {"exit_code": 124 if len(calls) == 1 else 1, "timed_out": len(calls) == 1}
            with patch.object(daily, "run_bounded", side_effect=bounded), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main([]), 1)
            self.assertEqual(len(calls), 3)
            self.assertIn("--destination", calls[1])
            self.assertIn("softball_connected", calls[2])
            report = json.loads((Path(temporary) / "Delivery/daily.json").read_text())
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["acquisition"]["exit_code"], 124)
            self.assertEqual(report["delivery"]["exit_code"], 1)

    def test_second_daily_run_fails_fast_under_whole_job_lock(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(daily, "ROOT", Path(temporary)):
            with ledger_lock(Path(temporary) / "Delivery/daily"), \
                    patch.object(daily, "run_bounded", side_effect=AssertionError("overlap")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main([]), 2)

    def test_abandoned_owned_snapshot_repairs_same_destination_on_same_day(self):
        from common.storage import save_json_atomic
        from datetime import datetime
        with tempfile.TemporaryDirectory() as temporary, patch.object(daily, "ROOT", Path(temporary)):
            root = Path(temporary)
            manifest = root / "Sheets/PullRuns/original.json"
            today = str(datetime.now(daily.ZoneInfo("America/Chicago")).date())
            save_json_atomic({"coordinator_id": "abandoned", "status": "running", "run_date": datetime.now().strftime("%Y-%m-%d")}, manifest)
            save_json_atomic({"run_id": "abandoned", "status": "running", "local_date": today,
                              "sport": "all", "mode": "snapshot", "acquisition_manifests": [str(manifest)]}, root / "Delivery/daily.json")
            commands = []
            def bounded(command, **kwargs):
                commands.append(command)
                return {"exit_code": 1, "timed_out": False}
            with patch.object(daily, "run_bounded", side_effect=bounded), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main([]), 1)
            self.assertIn("--repair-snapshot", commands[0])
            self.assertIn(str(manifest), commands[0])
            self.assertEqual(json.loads(manifest.read_text())["status"], "interrupted")

    def test_missed_day_starts_fresh_snapshot(self):
        from common.storage import save_json_atomic
        from datetime import datetime, timedelta
        with tempfile.TemporaryDirectory() as temporary, patch.object(daily, "ROOT", Path(temporary)):
            root = Path(temporary)
            before = datetime.now(daily.ZoneInfo("America/Chicago")).date() - timedelta(days=3)
            save_json_atomic({"run_id": "old", "local_date": str(before), "mode": "snapshot", "sport": "all"}, root / "Delivery/daily.json")
            with patch.object(daily, "run_bounded", return_value={"exit_code": 0, "timed_out": False}) as bounded, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(daily.main([]), 0)
            self.assertIn("--all", bounded.call_args_list[0].args[0])
            self.assertEqual(json.loads((root / "Delivery/daily.json").read_text())["missed_days"], 2)


if __name__ == "__main__":
    unittest.main()
