"""Sport provenance is metadata, never an extra Excel column."""

SPORTS = {"baseball", "softball"}


def scraper_sport(scraper):
    sport = getattr(scraper, "SPORT", "baseball")
    if sport not in SPORTS:
        raise ValueError(f"Unsupported scraper sport: {sport}")
    return sport


def payload_revision(payload):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()
