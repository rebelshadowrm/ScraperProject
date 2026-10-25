"""Softball detail eligibility, independent of every delivery acknowledgement."""
from datetime import datetime, timedelta, timezone


def needs_softball_details(entry, scraper, *, now=None):
    if entry.get('acquisition_status') != 'ready' or entry.get('last_attempt_status') == 'failed':
        return True
    observed = entry.get('observed_discovery_revision')
    if observed and observed != entry.get('acquired_discovery_revision'):
        return True
    now = now or datetime.now(timezone.utc)
    try:
        acquired = datetime.fromisoformat(entry.get('acquisition_refreshed_at', ''))
        if acquired.tzinfo is None:
            return True
    except (TypeError, ValueError):
        return True
    return now - acquired >= timedelta(hours=getattr(scraper, 'REFRESH_AFTER_HOURS', 24))
