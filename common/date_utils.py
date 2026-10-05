"""Explicit provider date ranges; no inferred year rollover."""
import re
from datetime import datetime


def explicit_year_range(raw):
    match = re.fullmatch(r"\s*([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\s*[-\u2013\u2014]\s*"
                         r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})\s*", raw or "")
    if not match:
        return None
    dates = []
    for offset in (0, 3):
        text = " ".join(match.groups()[offset:offset + 3])
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                dates.append(datetime.strptime(text, fmt).date())
                break
            except ValueError:
                continue
        else:
            return None
    return tuple(dates)
