"""Noninteractive daily coordinator. Does not register a Windows scheduled task."""
import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.process_budget import run_bounded
from common.storage import ledger_lock, save_json_atomic


def plan(*, sport, mode, config=None, repair=None):
    acquisition = [sys.executable, "-u", str(ROOT / "scripts/acquire_unattended.py")]
    if repair:
        acquisition += ["--repair-snapshot", str(repair)]
    else:
        acquisition += ["--all", "--mode", mode, "--sport", sport]
    delivery = [sys.executable, "-u", str(ROOT / "scripts/sync_destinations.py")]
    if config:
        delivery += ["--config", str(config)]
    return acquisition, delivery


def interrupted_manifests(owner):
    """Only coordinator-tagged manifests are eligible; caller holds ledger lock."""
    paths = []
    for path in (ROOT / "Sheets/PullRuns").glob("*.json"):
        if ".before_repair_" in path.name:
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("coordinator_id") != owner:
            continue
        if data.get("status") == "running":
            data.update(status="interrupted", finished_at=datetime.now(timezone.utc).isoformat(),
                        coordinator_error="Owned acquisition process terminated before final acknowledgement")
            save_json_atomic(data, path)
        paths.append(str(path))
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sport", choices=("baseball", "softball", "all"), default="all")
    parser.add_argument("--mode", choices=("snapshot", "refresh", "incremental"), default="snapshot")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--acquisition-timeout", type=int, default=21600)
    parser.add_argument("--delivery-timeout", type=int, default=1800, help="Whole-process budget per website destination")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.config:
        args.config = args.config.resolve()
    if min(args.acquisition_timeout, args.delivery_timeout) <= 0:
        parser.error("Timeouts must be positive seconds")
    acquisition, delivery = plan(sport=args.sport, mode=args.mode, config=args.config)
    chicago = datetime.now(ZoneInfo("America/Chicago"))
    if args.dry_run:
        print(json.dumps({"status": "dry_run", "timezone": "America/Chicago", "local_date": str(chicago.date()),
            "acquisition": acquisition, "delivery": delivery, "max_concurrency": 1,
            "destination_commands": {name: delivery + ["--destination", name]
                                     for name in ("baseball_connected", "softball_connected")},
            "timeouts": {"acquisition": args.acquisition_timeout, "per_destination": args.delivery_timeout},
            "network_requests": 0}, indent=2))
        return 0
    state = ROOT / "Delivery/daily.json"
    try:
        with ledger_lock(ROOT / "Delivery/daily"):
            previous = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
            run_id = chicago.strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
            log_root = ROOT / "Delivery/runs" / run_id
            log_root.mkdir(parents=True)
            report = {"run_id": run_id, "local_date": str(chicago.date()), "timezone": "America/Chicago",
                "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                "sport": args.sport, "mode": args.mode, "previous_run_id": previous.get("run_id"),
                "log_root": str(log_root), "acquisition_manifests": [], "max_concurrency": 1}
            if previous.get("local_date"):
                from datetime import date
                report["missed_days"] = max(0, (chicago.date() - date.fromisoformat(previous["local_date"])).days - 1)
            with ledger_lock(ROOT / "events.json"):
                if previous.get("run_id"):
                    interrupted_manifests(previous["run_id"])
            repair = None
            # Same-day snapshot repair reuses the existing CLI recovery path.
            if (args.mode == "snapshot" and previous.get("mode") == args.mode
                    and previous.get("sport") == args.sport and previous.get("local_date") == report["local_date"]):
                for path in previous.get("acquisition_manifests", []):
                    old = json.loads(Path(path).read_text(encoding="utf-8"))
                    if old.get("status") in ("partial", "interrupted") and old.get("run_date") == datetime.now().strftime("%Y-%m-%d"):
                        repair = path
                        break
            save_json_atomic(report, state)
            save_json_atomic(report, log_root / "manifest.json")
            env = dict(os.environ, SCRAPER_DAILY_RUN_ID=run_id, PYTHONUNBUFFERED="1")
            acquisition, delivery = plan(sport=args.sport, mode=args.mode, config=args.config, repair=repair)
            for phase, command, budget in (("acquisition", acquisition, args.acquisition_timeout),
                    ("baseball_connected", delivery + ["--destination", "baseball_connected"], args.delivery_timeout),
                    ("softball_connected", delivery + ["--destination", "softball_connected"], args.delivery_timeout)):
                # A failed acquisition must not suppress destination-only retry.
                try:
                    report[phase] = run_bounded(command, cwd=ROOT, stdout=log_root / (phase + ".stdout.log"),
                        stderr=log_root / (phase + ".stderr.log"), timeout=budget, env=env)
                except Exception as exc:
                    report[phase] = {"exit_code": 2, "error": type(exc).__name__}
                if phase == "acquisition":
                    with ledger_lock(ROOT / "events.json"):
                        report["acquisition_manifests"] = interrupted_manifests(run_id)
                    if repair and repair not in report["acquisition_manifests"]:
                        report["acquisition_manifests"].append(repair)
                save_json_atomic(report, state)
                save_json_atomic(report, log_root / "manifest.json")
            report["delivery"] = {"exit_code": 0 if all(report[name]["exit_code"] == 0
                for name in ("baseball_connected", "softball_connected")) else 1,
                "destinations": {name: report[name] for name in ("baseball_connected", "softball_connected")}}
            report["status"] = "complete" if all(report[p]["exit_code"] == 0 for p in ("acquisition", "delivery")) else "partial"
            report["finished_at"] = datetime.now(timezone.utc).isoformat()
            save_json_atomic(report, state)
            save_json_atomic(report, log_root / "manifest.json")
            print(json.dumps(report, indent=2))
            return 0 if report["status"] == "complete" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
