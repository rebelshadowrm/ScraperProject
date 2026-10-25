"""Website-only retry/reconciliation; never scrape or rewrite Excel."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.delivery import DESTINATIONS, DeliveryStore
from common.json_utils import load_events
from common.storage import ledger_lock, save_json_atomic


def run(*, ledger, state, config=None, dry_run=False, limit=100, replay=None, export_dir=None, destination=None, allow_loopback=False):
    destinations = [destination] if destination else list(DESTINATIONS)
    if destination is not None and destination not in DESTINATIONS:
        raise ValueError("Unknown website destination")
    # Do not create/migrate a database or acquire production locks in dry-run.
    if dry_run:
        return {"status": "dry_run", "ledger": str(ledger), "state": str(state),
                "destinations": destinations, "network_requests": 0}
    with ledger_lock(state):
        with ledger_lock(ledger):
            data = load_events(ledger)
        store = DeliveryStore(state)
        try:
            ingestion = store.ingest(data)
            if replay:
                store.requeue(replay)
            destination_errors = {}
            blocked = {}
            for target_destination in destinations:
                if export_dir:
                    store.export(target_destination, Path(export_dir) / (target_destination + ".json"))
                target = (config or {}).get(target_destination, {})
                if not target.get("enabled", False):
                    blocked[target_destination] = 'delivery_disabled' if target else 'delivery_configuration_missing'
                    continue
                if not os.environ.get(target.get('token_env',''),''):
                    blocked[target_destination] = 'delivery_credential_missing'
                try:
                    interval=target.get('min_interval_seconds',0.6 if target_destination=='softball_connected' else 0)
                    if not isinstance(interval,(int,float)) or not 0<=interval<=60:raise ValueError('Invalid delivery pacing')
                    store.send(target_destination, target["endpoint"], os.environ.get(target["token_env"], ""), limit=limit,min_interval=interval,allow_loopback=allow_loopback)
                except Exception as exc:
                    destination_errors[target_destination] = type(exc).__name__
            summary = store.summary()
            pending = sum(v for name in destinations for key, v in summary[name].items() if key != "delivered")
            return {"status": "partial" if pending or destination_errors or ingestion["quarantined"] else "complete",
                    "ingestion": ingestion, "destinations": summary, "selected_destinations": destinations,
                    "errors": destination_errors, "blocked_destinations": blocked}
        finally:
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, default=ROOT / "events.json")
    parser.add_argument("--state", type=Path, default=ROOT / "Delivery/state.sqlite3")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, choices=range(1, 1001), default=100)
    parser.add_argument("--replay", choices=DESTINATIONS)
    parser.add_argument("--destination", choices=DESTINATIONS, help="Retry only this destination")
    parser.add_argument("--export-dir", type=Path, help="Materialize website envelopes without acknowledging delivery")
    parser.add_argument('--allow-loopback',action='store_true',help='Allow literal loopback HTTP for isolated acceptance only')
    args = parser.parse_args(argv)
    try:
        config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
        report = run(ledger=args.ledger, state=args.state, config=config, dry_run=args.dry_run,
                     limit=args.limit, replay=args.replay, export_dir=args.export_dir, destination=args.destination,allow_loopback=args.allow_loopback)
        if args.report and not args.dry_run:
            save_json_atomic(report, args.report)
        print(json.dumps(report, indent=2))
        return 0 if report["status"] in ("complete", "dry_run") else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
