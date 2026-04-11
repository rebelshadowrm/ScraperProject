import json
import os
from datetime import datetime, timezone
from typing import Any, Dict


MAX_RECENT_RUNS_PER_LOCATION = 12


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_div(numerator: float, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 3)


def _default_location_entry(*, scraper: str, location: str) -> Dict[str, Any]:
    return {
        "scraper": scraper,
        "location": location,
        "runs_checked": 0,
        "runs_with_events": 0,
        "runs_with_writes": 0,
        "total_runtime_seconds": 0.0,
        "avg_runtime_seconds": 0.0,
        "total_links_collected": 0,
        "avg_links_collected": 0.0,
        "total_candidates_processed": 0,
        "avg_candidates_processed": 0.0,
        "total_rows_written": 0,
        "avg_rows_written": 0.0,
        "total_stale_skipped": 0,
        "avg_stale_skipped": 0.0,
        "total_validation_failures": 0,
        "total_extraction_failures": 0,
        "total_non_future_skipped": 0,
        "total_batches_attempted": 0,
        "total_batches_written": 0,
        "last_run_at": "",
        "last_runtime_seconds": 0.0,
        "last_links_collected": 0,
        "last_candidates_processed": 0,
        "last_rows_written": 0,
        "last_stale_skipped": 0,
        "last_run_mode": "",
        "last_had_events": False,
        "last_had_writes": False,
        "last_failed": False,
        "recent_runs": [],
    }


def ensure_scheduler_history_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("locations", {})
    if not isinstance(data["locations"], dict):
        data["locations"] = {}
    data.setdefault("updated_at", "")
    return data


def load_scheduler_history(json_path: str) -> Dict[str, Any]:
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as file:
            return ensure_scheduler_history_schema(json.load(file))
    return ensure_scheduler_history_schema({})


def save_scheduler_history(data: Dict[str, Any], json_path: str):
    data = ensure_scheduler_history_schema(data)
    data["updated_at"] = _utc_now_iso()
    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4)


def record_location_run(
    history_data: Dict[str, Any],
    *,
    scraper: str,
    location: str,
    run_mode: str,
    runtime_seconds: float,
    links_collected: int,
    candidates_processed: int,
    rows_written: int,
    stale_skipped: int,
    validation_failures: int,
    extraction_failures: int,
    non_future_skipped: int,
    batches_attempted: int,
    batches_written: int,
) -> Dict[str, Any]:
    history_data = ensure_scheduler_history_schema(history_data)
    key = f"{scraper}|{location}"
    entry = history_data["locations"].get(key)
    if not entry:
        entry = _default_location_entry(scraper=scraper, location=location)
        history_data["locations"][key] = entry

    entry["runs_checked"] += 1
    if links_collected > 0:
        entry["runs_with_events"] += 1
    if rows_written > 0:
        entry["runs_with_writes"] += 1

    entry["total_runtime_seconds"] += float(runtime_seconds)
    entry["total_links_collected"] += int(links_collected)
    entry["total_candidates_processed"] += int(candidates_processed)
    entry["total_rows_written"] += int(rows_written)
    entry["total_stale_skipped"] += int(stale_skipped)
    entry["total_validation_failures"] += int(validation_failures)
    entry["total_extraction_failures"] += int(extraction_failures)
    entry["total_non_future_skipped"] += int(non_future_skipped)
    entry["total_batches_attempted"] += int(batches_attempted)
    entry["total_batches_written"] += int(batches_written)

    runs_checked = int(entry["runs_checked"])
    entry["avg_runtime_seconds"] = _safe_div(entry["total_runtime_seconds"], runs_checked)
    entry["avg_links_collected"] = _safe_div(entry["total_links_collected"], runs_checked)
    entry["avg_candidates_processed"] = _safe_div(entry["total_candidates_processed"], runs_checked)
    entry["avg_rows_written"] = _safe_div(entry["total_rows_written"], runs_checked)
    entry["avg_stale_skipped"] = _safe_div(entry["total_stale_skipped"], runs_checked)

    entry["last_run_at"] = _utc_now_iso()
    entry["last_runtime_seconds"] = round(float(runtime_seconds), 3)
    entry["last_links_collected"] = int(links_collected)
    entry["last_candidates_processed"] = int(candidates_processed)
    entry["last_rows_written"] = int(rows_written)
    entry["last_stale_skipped"] = int(stale_skipped)
    entry["last_run_mode"] = run_mode
    entry["last_had_events"] = bool(links_collected > 0)
    entry["last_had_writes"] = bool(rows_written > 0)
    entry["last_failed"] = bool(validation_failures > 0 or extraction_failures > 0)

    recent_run = {
        "recorded_at": entry["last_run_at"],
        "run_mode": run_mode,
        "runtime_seconds": round(float(runtime_seconds), 3),
        "links_collected": int(links_collected),
        "candidates_processed": int(candidates_processed),
        "rows_written": int(rows_written),
        "stale_skipped": int(stale_skipped),
        "validation_failures": int(validation_failures),
        "extraction_failures": int(extraction_failures),
        "non_future_skipped": int(non_future_skipped),
        "batches_attempted": int(batches_attempted),
        "batches_written": int(batches_written),
    }
    recent_runs = entry.setdefault("recent_runs", [])
    recent_runs.append(recent_run)
    if len(recent_runs) > MAX_RECENT_RUNS_PER_LOCATION:
        del recent_runs[:-MAX_RECENT_RUNS_PER_LOCATION]

    return entry
