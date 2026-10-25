"""National softball acquisition with durable per-event and per-state checkpoints.

Implemented searches run across all US states and DC. Providers lacking verified
national discovery are reported as gaps, never represented by sample successes.
This command never exports a workbook or sends an event to a website.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from us import states

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.identity import event_identity
from common.json_utils import load_events, save_events
from common.normalize import normalize_row
from common.softball_national import NATIONAL_STRATEGIES, REGISTERPLAY, discover_state, discover_pgf
from common.softball_discovery import discover_national
from common.softball_refresh import needs_softball_details
from common.softball_registry import softball_registry
from common.source_exclusions import SourceEventIncomplete, SourceEventOutOfScope
from common.storage import ledger_lock, save_json_atomic
from common.validation import is_future_event, validate_event_data
from scripts.softball_site_run import site_locations
from common.softball_transport import source_stage, source_transport, PROVIDER, operations
from common.softball_lifecycle import retain_cancellation


def acquire(*, run_dir, ledger, registry=None, state_codes=None):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    registry = registry or softball_registry()
    default_scopes = state_codes is None
    state_codes = list(dict.fromkeys(state_codes or [s.abbr for s in states.STATES] + ["DC"]))
    report = {"status": "running", "sport": "softball", "scope": "US states and DC" if default_scopes else "explicit bounded scopes",
              "requested_scopes":state_codes, "national_scopes_requested":default_scopes,
              "coverage_complete": False, "providers": {}, "accepted": 0, "unchanged": 0,
              "started_at": datetime.now(timezone.utc).isoformat()}
    report_path = run_dir / "acquisition.json"
    save_json_atomic(report, report_path)
    for provider, cls in registry.items():
        if provider not in NATIONAL_STRATEGIES:
            report["providers"][provider] = {"status": "queued", "coverage_complete": False,
                "reason": getattr(cls, "BLOCKER", "National discovery or source access is not verified")}
            save_json_atomic(report, report_path)
            continue
        outcome = {"status": "running", "coverage_complete": False, "states": {},'started_at':datetime.now(timezone.utc).isoformat()}
        report["providers"][provider] = outcome
        scraper = cls()
        provider_token=PROVIDER.set(provider)
        outcome['coverage_limitations'] = list(getattr(scraper,'COVERAGE_LIMITATIONS',[]))
        provider_dir = run_dir / provider
        provider_dir.mkdir()
        try:
            scopes = list(dict.fromkeys(state_codes + (list(getattr(scraper,'SEARCH_STATE_ALIASES',{}))+
                list(getattr(scraper,'SEARCH_MULTI_STATE_SCOPES',{})) if default_scopes else [])))
            for state in (scopes if provider in REGISTERPLAY else ["US"]):
                scope = {"status": "running", "discovered": 0, "accepted": 0, "unchanged": 0,
                         "detail_attempted": 0, "held": [],'lifecycle_updates':0}
                outcome["states"][state] = scope
                save_json_atomic(report, report_path)
                try:
                    discovery_offset=len(operations())
                    with source_stage('discovery'):
                        links = (discover_pgf(scraper) if provider == "pgf" else
                                 discover_state(scraper, state, capture=provider_dir / (state + ".json")) if provider in REGISTERPLAY else
                                 discover_national(provider,scraper))
                    scope.update(discovered=len(links), held=links.held, collection_complete=links.complete,
                                 collection_error=links.error, source_total=getattr(links,'source_total',None),
                                 filter_counts=getattr(links,'filter_counts',{}))
                    scope['examined_scopes']=getattr(links,'scopes',[])
                    scope['listing_pages']=links.pages
                    for url in links:
                        key = event_identity(scraper, url)
                        with ledger_lock(ledger):
                            data = load_events(ledger)
                            entry = data["events"].get(key, {})
                            if entry.get("sport", "softball") != "softball":
                                raise ValueError("Stored event sport differs from acquisition")
                            observed = scraper.get_discovery_revision(url)
                            candidate = dict(entry, observed_discovery_revision=observed)
                            refresh = needs_softball_details(candidate, scraper)
                        if not refresh:
                            scope["unchanged"] += 1
                            report["unchanged"] += 1
                            continue
                        try:
                            scope['detail_attempted'] += 1
                            detail_offset=len(operations())
                            with source_stage('detail'):
                                row = normalize_row(scraper.extract_event_details(url))
                            valid, reason = validate_event_data(row)
                            if not valid:
                                raise SourceEventIncomplete(reason)
                            if not is_future_event(row):
                                raise SourceEventOutOfScope("Source event is no longer future")
                            if not row.get("City") or row["City"].lower() in {"tbd", "unknown", "various", "varies", "multiple locations"}:
                                raise SourceEventIncomplete("No usable source city")
                            details = dict(scraper.source_details.get(url, {}))
                            locations = site_locations(details)
                            if locations:
                                details["site_locations"] = locations
                            payload = dict(row, source_details=details, eventStatus="active",
                                           **({"locations": locations} if locations else {}))
                            if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 256 * 1024:
                                raise SourceEventIncomplete("Payload exceeds receiver limit; source retained for repair")
                            observed_times=[op['observed_at'] for op in operations()[detail_offset:] if op.get('observed_at')]
                            if not observed_times:
                                observed_times=[op['observed_at'] for op in operations()[discovery_offset:detail_offset] if op.get('observed_at')]
                            source_observed_at=datetime.fromtimestamp(min(observed_times),timezone.utc).isoformat() if observed_times else datetime.now(timezone.utc).isoformat()
                            with ledger_lock(ledger):
                                data = load_events(ledger)
                                # Re-read to preserve any independent operator/export flags.
                                current = data["events"].setdefault(key, {})
                                current.update(scraper=cls.__name__, sport="softball", sport_verified=True,
                                    source_event_id=scraper.get_event_identity(url), url=url, event_data=row,
                                    source_details=details, acquisition_status="ready",
                                    acquisition_refreshed_at=source_observed_at,
                                    acquired_discovery_revision=observed, observed_discovery_revision=observed,
                                    last_attempt_status="success")
                                current.setdefault("processed", False)
                                current.setdefault("export_status", "not_applicable")
                                current.pop('source_review_required',None)
                                current.pop('source_review_reason',None)
                                save_events(data, ledger)
                            scope["accepted"] += 1
                            report["accepted"] += 1
                        except (SourceEventIncomplete, SourceEventOutOfScope, ValueError) as exc:
                            if isinstance(exc,SourceEventOutOfScope):
                                facts=getattr(scraper,'source_details',{}).get(url,{})
                                with ledger_lock(ledger):
                                    data=load_events(ledger)
                                    times=[op['observed_at'] for op in operations()[detail_offset:] if op.get('observed_at')]
                                    when=datetime.fromtimestamp(min(times),timezone.utc).isoformat() if times else datetime.now(timezone.utc).isoformat()
                                    if retain_cancellation(data['events'].get(key),facts,observed_at=when):
                                        save_events(data,ledger);scope['lifecycle_updates']+=1
                            scope["held"].append({"event_id": key, "reason": str(exc),
                                                  "out_of_scope": isinstance(exc, SourceEventOutOfScope)})
                        except Exception as exc:
                            scope["held"].append({"event_id": key, "reason": type(exc).__name__, "out_of_scope": False})
                        save_json_atomic(report, report_path)
                    scope["status"] = "partial" if any(not item.get("out_of_scope") for item in scope["held"]) or not links.complete else "complete"
                except Exception as exc:
                    scope.update(status="failed", error=type(exc).__name__,error_message=str(exc)[:500])
                    if 'collection_complete' not in scope:scope['discovered']=None
                save_json_atomic(report, report_path)
            outcome['requested_scope_complete']=all(s["status"] == "complete" for s in outcome["states"].values())
            outcome["coverage_complete"] = (default_scopes and not outcome['coverage_limitations']
                and all(s["status"] == "complete" for s in outcome["states"].values()))
            outcome["status"] = "complete" if outcome["coverage_complete"] else "partial"
        finally:
            outcome['finished_at']=datetime.now(timezone.utc).isoformat()
            requests=[op for op in operations() if op.get('provider')==provider]
            outcome['transport']={'network_requests':sum(op.get('network_attempts',0) for op in requests),
                'detail_requests':sum(op.get('network_attempts',0) for op in requests if op['stage']=='detail'),
                'failures':[op for op in requests if op['status']=='failed']}
            PROVIDER.reset(provider_token)
            scraper.close()
            save_json_atomic(report, report_path)
    report["coverage_complete"] = all(p["coverage_complete"] for p in report["providers"].values())
    report.update(status="complete" if report["coverage_complete"] else "partial",
                  finished_at=datetime.now(timezone.utc).isoformat())
    save_json_atomic(report, report_path)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    args = parser.parse_args(argv)
    from scripts.softball_provider_cycle import CaptureFetcher
    fetcher=CaptureFetcher(args.run_dir.parent/(args.run_dir.name+'-captures'),max_requests=20000)
    with source_transport(fetcher):
        result = acquire(run_dir=args.run_dir, ledger=args.ledger)
    print(json.dumps(result))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
