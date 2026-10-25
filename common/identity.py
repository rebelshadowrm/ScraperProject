from collections import defaultdict
from urllib.parse import urlsplit, urlunsplit, urldefrag


def normalize_url(url):
    """Normalize scheme/host, preserving case-sensitive path, query and fragment."""
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       parts.query, parts.fragment))


def event_identity(scraper, url):
    from common.sport import scraper_sport
    identity = getattr(scraper, "get_event_identity", None)
    value = identity(url) if identity else normalize_url(url)
    # Existing baseball keys and workbook aliases must remain byte-for-byte stable.
    prefix = scraper.__class__.__name__
    sport = scraper_sport(scraper)
    if sport != "baseball":
        prefix += "|" + sport
    return f"{prefix}|{value}"


def discovered_identities(events_data, failed_data, scraper, links, location):
    """Rekey unambiguous legacy records without losing processed or pending state."""
    provider = scraper.__class__.__name__
    from common.sport import scraper_sport
    if scraper_sport(scraper) != "baseball":
        # Never adopt an ambiguous old baseball lifecycle for a softball event.
        return {event_identity(scraper, url) for url in links}
    public_url = lambda u: urldefrag(u)[0] if getattr(scraper, "SCRAPER_NAME", "") == "TopGun" else u
    counts = defaultdict(set)
    for url in links:
        counts[normalize_url(public_url(url))].add(event_identity(scraper, url))
    legacy = defaultdict(list)
    for key, entry in events_data["events"].items():
        if "|" in key or entry.get("scraper") not in (None, "", provider):
            continue
        if location != entry.get("location") and location not in entry.get("locations", []):
            continue
        legacy[normalize_url(public_url(entry.get("url", key)))].append(key)
    keys = set()
    for url in links:
        key = event_identity(scraper, url)
        keys.add(key)
        canonical = normalize_url(public_url(url))
        matches = legacy.get(canonical, [])
        if key not in events_data["events"] and len(matches) == 1 and len(counts[canonical]) == 1:
            old_key = matches.pop()
            entry = events_data["events"].pop(old_key)
            entry.setdefault("export_identity", old_key)
            events_data["events"][key] = entry
            for failure in failed_data.get("failed_events", []):
                if failure.get("name") == old_key:
                    failure["name"] = key
    return keys


def pending_locations(events_data, scraper):
    """Keep staged exports reachable if a dynamic source removes their location."""
    result = {}
    for entry in events_data.get("events", {}).values():
        if (entry.get("scraper") == scraper.__class__.__name__
                and entry.get("export_status") == "pending" and not entry.get("is_deleted")):
            location = entry.get("export_location") or entry.get("location")
            source = entry.get("discovered_from")
            if location and source:
                result.setdefault(location, source)
    return result
