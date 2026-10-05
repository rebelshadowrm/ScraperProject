# common/json_utils.py

import json
import os
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from common.storage import save_json_atomic

logger = logging.getLogger(__name__)

# ----------------------------
# Time helpers
# ----------------------------

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _coerce_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y"):
            return True
        if s in ("false", "0", "no", "n", ""):
            return False
    return default

# ----------------------------
# Schema defaults
# ----------------------------

def default_event_entry(
    *,
    url: str,
    location: str,
    scraper: Optional[str] = None,
    discovered_from: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Canonical event entry schema (JSON v1).
    This is intentionally DB-friendly.
    """
    now = _utc_now_iso()
    return {
        "url": url,
        "location": location,
        "locations": [location] if location else [],

        # provenance
        "scraper": scraper or "",              # e.g. "PGScraper"
        "discovered_from": discovered_from or "",  # base_url / listing page

        # lifecycle
        "processed": False,
        "created_at": now,          # first time we ever recorded this event
        "last_seen_at": now,        # last time it appeared during link collection
        "processed_at": "",         # when processed was set True
        "event_data": None,         # durable payload for replay and snapshots
        "export_status": "",       # pending, exported, expired
        "export_file": "",         # replay to the original destination

        # attempts + failures (event detail extraction attempts)
        "attempt_count": 0,
        "last_attempt_at": "",
        "last_attempt_status": "",  # "success" | "failed" | ""
        "last_error": "",

        # soft delete (future DB-style)
        "is_deleted": False,
        "deleted_at": "",
        "delete_reason": "",
    }

def default_failed_entry(
    *,
    name: str,
    url: str,
    location: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    now = _utc_now_iso()
    return {
        "name": name,
        "url": url,
        "location": location,
        "reason": reason,

        # useful history
        "first_failed_at": now,
        "last_failed_at": now,
        "fail_count": 1,
        "resolved_at": "",  # set if/when we remove it because it succeeded
    }

def ensure_events_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migrate older events.json structures forward in-place (but returns data for convenience).
    - Ensures root keys exist.
    - Ensures each event entry has all default keys.
    """
    if not isinstance(data, dict):
        data = {}
    data.setdefault("events", {})
    if not isinstance(data["events"], dict):
        data["events"] = {}

    now = _utc_now_iso()
    for key, ev in list(data["events"].items()):
        if not isinstance(ev, dict):
            # replace malformed entry
            data["events"][key] = default_event_entry(url=str(key), location="")
            continue

        # old fields: url/processed/location existed
        url = ev.get("url", key)
        location = ev.get("location", "")

        # Fill defaults without overwriting existing values
        base = default_event_entry(
            url=url,
            location=location,
            scraper=ev.get("scraper") or "",
            discovered_from=ev.get("discovered_from") or "",
        )
        for k, v in base.items():
            ev.setdefault(k, v)

        # Coerce certain fields safely
        ev["processed"] = _coerce_bool(ev.get("processed", False), default=False)
        ev["attempt_count"] = int(ev.get("attempt_count") or 0)

        # If this was an old entry and created_at missing, set it
        if not ev.get("created_at"):
            ev["created_at"] = now
        if not ev.get("last_seen_at"):
            ev["last_seen_at"] = ev["created_at"]

    return data

def ensure_failed_schema(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Migrate older failed_events.json forward in-place.
    Ensures each failure has timestamps + fail_count.
    """
    if not isinstance(data, dict):
        data = {}
    data.setdefault("failed_events", [])
    if not isinstance(data["failed_events"], list):
        data["failed_events"] = []

    now = _utc_now_iso()
    for i, fev in enumerate(list(data["failed_events"])):
        if not isinstance(fev, dict):
            continue
        if not fev.get("first_failed_at"):
            fev["first_failed_at"] = now
        if not fev.get("last_failed_at"):
            fev["last_failed_at"] = fev["first_failed_at"]
        if "fail_count" not in fev:
            fev["fail_count"] = 1
        fev.setdefault("location", "")
        fev.setdefault("resolved_at", "")
        fev.setdefault("reason", "")

    return data

# ----------------------------
# Existing API (kept compatible)
# ----------------------------

def load_events(json_path: str) -> Dict:
    """
    Load events.json. Ensures schema + defaults for older files.
    """
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as file:
            data = json.load(file)
            return ensure_events_schema(data)
    return {"events": {}}

def save_events(data: Dict, json_path: str):
    """
    Save updated events data back to the JSON file.
    """
    save_json_atomic(data, json_path)
    logger.debug("Events saved to %s", json_path)

def load_failed_events(json_path: str) -> Dict:
    """
    Load failed_events.json, deduplicating by (name|url), and ensuring schema.
    """
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as file:
            data = json.load(file)
            data = ensure_failed_schema(data)

            # Deduplicate by name|url (preserve most recent last_failed_at if possible)
            unique = {}
            for ev in data.get("failed_events", []):
                key = f"{ev.get('name','')}|{ev.get('url','')}"
                if key not in unique:
                    unique[key] = ev
                else:
                    # merge: keep latest timestamps + increment counts
                    prev = unique[key]
                    prev_last = prev.get("last_failed_at", "")
                    cur_last = ev.get("last_failed_at", "")
                    if cur_last and (not prev_last or cur_last > prev_last):
                        prev["last_failed_at"] = cur_last
                        prev["reason"] = ev.get("reason", prev.get("reason", ""))
                        prev["location"] = ev.get("location", prev.get("location", ""))
                    prev["fail_count"] = int(prev.get("fail_count") or 1) + int(ev.get("fail_count") or 1)

            data["failed_events"] = list(unique.values())
            return data

    return {"failed_events": []}

def save_failed_events(data: Dict, json_path: str):
    """
    Save and deduplicate failed events.
    """
    data = ensure_failed_schema(data)

    unique = {}
    for ev in data.get("failed_events", []):
        key = f"{ev.get('name','')}|{ev.get('url','')}"
        if key not in unique:
            unique[key] = ev
        else:
            prev = unique[key]
            prev["fail_count"] = int(prev.get("fail_count") or 1) + int(ev.get("fail_count") or 1)
            prev["last_failed_at"] = max(prev.get("last_failed_at", ""), ev.get("last_failed_at", ""))
            # prefer latest reason/location if present
            if ev.get("reason"):
                prev["reason"] = ev["reason"]
            if ev.get("location"):
                prev["location"] = ev["location"]

    data["failed_events"] = list(unique.values())

    save_json_atomic(data, json_path)

    logger.debug("Failed events saved to %s", json_path)

# ----------------------------
# New helper API (optional now, very useful later)
# ----------------------------

def add_or_update_event(
    events_data: Dict[str, Any],
    *,
    key: str,
    url: str,
    location: str,
    scraper: Optional[str] = None,
    discovered_from: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Create event entry if missing, otherwise update last_seen_at and provenance.
    Returns the entry dict.
    """
    events_data.setdefault("events", {})

    now = _utc_now_iso()
    ev = events_data["events"].get(key)
    if not ev:
        ev = default_event_entry(
            url=url,
            location=location,
            scraper=scraper,
            discovered_from=discovered_from,
        )
        events_data["events"][key] = ev
        return ev

    # update "seen" and provenance
    ev["last_seen_at"] = now
    ev["url"] = url
    if location:
        ev["location"] = location
        memberships = ev.setdefault("locations", [])
        if location not in memberships:
            memberships.append(location)
    if scraper and not ev.get("scraper"):
        ev["scraper"] = scraper
    if discovered_from and not ev.get("discovered_from"):
        ev["discovered_from"] = discovered_from
    return ev

def record_attempt(
    events_data: Dict[str, Any],
    *,
    key: str,
    status: str,
    error: str = "",
):
    """
    Record an attempt to extract event details for this event key.
    status: "success" or "failed"
    """
    ev = events_data["events"].get(key)
    if not ev:
        return
    now = _utc_now_iso()
    ev["attempt_count"] = int(ev.get("attempt_count") or 0) + 1
    ev["last_attempt_at"] = now
    ev["last_attempt_status"] = status
    ev["last_error"] = error or ""

def mark_processed(events_data: Dict[str, Any], *, key: str):
    ev = events_data["events"].get(key)
    if not ev:
        return
    ev["processed"] = True
    ev["processed_at"] = _utc_now_iso()
    ev["last_attempt_status"] = "success"
    ev["last_error"] = ""

def add_or_update_failure(
    failed_data: Dict[str, Any],
    *,
    name: str,
    url: str,
    location: str,
    reason: str,
):
    ensure_failed_schema(failed_data)

    now = _utc_now_iso()
    key = f"{name}|{url}"
    existing = None
    for fev in failed_data["failed_events"]:
        if f"{fev.get('name','')}|{fev.get('url','')}" == key:
            existing = fev
            break

    if not existing:
        failed_data["failed_events"].append(default_failed_entry(
            name=name, url=url, location=location, reason=reason
        ))
        return

    existing["last_failed_at"] = now
    existing["reason"] = reason
    if location:
        existing["location"] = location
    existing["fail_count"] = int(existing.get("fail_count") or 1) + 1

def mark_failure_resolved(failed_data: Dict[str, Any], *, name: str, url: str):
    """
    Optionally mark resolved; you may still remove the item from list if desired.
    """
    ensure_failed_schema(failed_data)
    now = _utc_now_iso()
    for fev in failed_data["failed_events"]:
        if fev.get("name") == name and fev.get("url") == url:
            fev["resolved_at"] = now
            return
