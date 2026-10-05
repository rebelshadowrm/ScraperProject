import logging
from datetime import datetime, timezone
from typing import Any, Dict, Set

from common.json_utils import (
    load_events,
    save_events,
    load_failed_events,
    save_failed_events,
)
from common.storage import ledger_lock

logger = logging.getLogger(__name__)


def _parse_cutoff(value: str) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("A cutoff date/time is required.")

    if len(raw) == 10:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)

    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def rollback_events(**kwargs) -> Dict[str, Any]:
    with ledger_lock(kwargs["events_path"]):
        return _rollback_events_unlocked(**kwargs)


def _rollback_events_unlocked(
    *,
    events_path: str,
    failed_events_path: str,
    created_after: str,
    scraper_filter: str = "",
    location_filter: str = "",
    reason: str = "",
) -> Dict[str, Any]:
    cutoff = _parse_cutoff(created_after)
    scraper_filter = scraper_filter.strip().lower()
    location_filter = location_filter.strip().lower()
    reason = reason.strip() or f"Rollback via Data Control after {cutoff.isoformat()}"

    events_data = load_events(events_path)
    failed_data = load_failed_events(failed_events_path)

    affected_urls: Set[str] = set()
    rolled_back = 0
    already_deleted = 0

    for event in events_data.get("events", {}).values():
        if not isinstance(event, dict):
            continue

        created_at_raw = str(event.get("created_at") or "").strip()
        if not created_at_raw:
            continue

        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError:
            continue

        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)

        if created_at < cutoff:
            continue

        scraper_name = str(event.get("scraper") or "").lower()
        location_name = str(event.get("location") or "").lower()

        if scraper_filter and scraper_filter not in scraper_name:
            continue
        if location_filter and location_filter not in location_name:
            continue

        if event.get("is_deleted"):
            already_deleted += 1
            continue

        event["is_deleted"] = True
        event["deleted_at"] = datetime.now(timezone.utc).isoformat()
        event["delete_reason"] = reason
        event["processed"] = False
        event["processed_at"] = ""
        affected_urls.add(str(event.get("url") or ""))
        rolled_back += 1

    if affected_urls:
        failed_data["failed_events"] = [
            ev for ev in failed_data.get("failed_events", [])
            if str(ev.get("url") or "") not in affected_urls
        ]

    save_events(events_data, events_path)
    save_failed_events(failed_data, failed_events_path)

    logger.info(
        "Rollback complete. Rolled back=%d, already_deleted=%d, affected_failures_removed=%d",
        rolled_back,
        already_deleted,
        len(affected_urls),
    )

    return {
        "rolled_back": rolled_back,
        "already_deleted": already_deleted,
        "affected_failures_removed": len(affected_urls),
        "cutoff": cutoff.isoformat(),
    }
