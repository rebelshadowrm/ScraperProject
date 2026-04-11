from __future__ import annotations

import re
from typing import Any, Dict
from datetime import datetime
from us import states

from common.config import (
    VALID_LEVELS,
    VALID_AGE_GROUPS,
    VALID_GUARANTEED_GAMES,
    VALID_TOURNAMENT_TYPES,
    VALID_TOURNAMENT_SURFACES,
    VALID_YES_NO_NA,
)

NBSP = "\u00A0"

# Build fast lookup maps once
_LEVEL_CANON_MAP = {v.upper(): v for v in VALID_LEVELS}
_AGE_CANON_SET = set(VALID_AGE_GROUPS)
_GG_CANON_SET = set(VALID_GUARANTEED_GAMES)
_TYPE_CANON_MAP = {v.upper(): v for v in VALID_TOURNAMENT_TYPES}
_SURF_CANON_MAP = {v.upper(): v for v in VALID_TOURNAMENT_SURFACES}
_YESNO_CANON_MAP = {v.upper(): v for v in VALID_YES_NO_NA}

# Extra aliases that frequently show up from scrapers
_LEVEL_ALIASES = {
    "OPEN": "Open",
    "OPN": "Open",
    "RECREATIONAL": "Rec",
    "REC": "Rec",
    "SILVER": "Silver",

    # Youth-specific / flattened aliases
    "COACH PITCH - OPEN": "Open",
    "COACH PITCH OPEN": "Open",
    "KID PITCH": "Open",

    "COACH PITCH - AA": "AA",
    "COACH PITCH AA": "AA",

    "AMERICAN": "A",

    # Unsupported / omit from dropdown-constrained export
    "MODIFIED TBALL": "",
    "MODIFIED T-BALL": "",
    "TBALL": "",
    "T-BALL": "",
    "CP": "",
    "NATIONAL": "",
}

_TYPE_ALIASES = {
    "INDIVIDUAL": "Individual",
    "TEAM": "Team",
}

_SURF_ALIASES = {
    "TURF": "Artificial",
    "ARTIFICIAL TURF": "Artificial",
    "SYNTHETIC": "Artificial",
    "N/A": "NA",
    "NONE": "NA",
}

_YESNO_ALIASES = {
    "Y": "Yes",
    "YES": "Yes",
    "N": "No",
    "NO": "No",
    "N/A": "NA",
    "NONE": "NA",
    "": "NA",
}


def clean_text(value: Any) -> str:
    """Normalize whitespace and common hidden chars; return a safe string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    s = str(value)
    s = s.replace(NBSP, " ")
    s = s.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _canon_from_map(raw: str, canon_map: Dict[str, str], aliases: Dict[str, str] | None = None) -> str:
    if not raw:
        return ""
    u = raw.upper()
    if aliases and u in aliases:
        return aliases[u]
    return canon_map.get(u, "")


def normalize_comp_level(value: Any) -> str:
    """
    Return canonical competition level token (e.g. 'Open', 'AA', 'D1', etc.)
    or '' if invalid/unrecognized.

    If multiple levels are crammed into one cell (e.g. 'D2 / D3'),
    return the first valid canonical level. The scraper can split these
    beforehand when it wants to preserve more than one.
    """
    raw = clean_text(value)
    if not raw:
        return ""

    parts = re.split(r"[\/,|]+", raw)
    for part in parts:
        t = clean_text(part).strip("(),;")
        if not t:
            continue

        canon = _canon_from_map(t, _LEVEL_CANON_MAP, _LEVEL_ALIASES)
        if canon:
            return canon

    return ""


def normalize_age_group(value: Any) -> str:
    """
    Canonical age group tokens are '6u'..'18u'.

    Accepts richer youth inputs like:
      - '12U'
      - 'U12'
      - '12'
      - '12u/13u'      -> returns first valid
      - '6U CP'        -> 6u
      - '8U KP'        -> 8u
      - '8U PP'        -> 8u
      - '13U (54/80)'  -> 13u
      - '13U (60/90)'  -> 13u
    """
    raw = clean_text(value)
    if not raw:
        return ""

    parts = re.split(r"[\/,|]+", raw)
    for p in parts:
        t = clean_text(p)
        if not t:
            continue

        if t in _AGE_CANON_SET:
            return t

        num = None

        m1 = re.fullmatch(r"(\d{1,2})\s*[uU]", t)
        m2 = re.fullmatch(r"[uU]\s*(\d{1,2})", t)

        if m1:
            num = int(m1.group(1))
        elif m2:
            num = int(m2.group(1))
        elif re.fullmatch(r"\d{1,2}", t):
            num = int(t)
        else:
            m3 = re.search(r"\b(\d{1,2})\s*[uU]\b", t)
            if m3:
                num = int(m3.group(1))

        if num is not None:
            candidate = f"{num}u"
            if candidate in _AGE_CANON_SET:
                return candidate

    return ""


def normalize_guaranteed_games(value: Any) -> str:
    """
    Canonical GG tokens are numeric strings (e.g. '1'..'9') OR 'None' ONLY when explicitly stated.
    IMPORTANT: empty/unknown stays '' (do NOT coerce to 'None').
    """
    raw = clean_text(value)
    if raw == "":
        return ""

    u = raw.strip("(),;").upper()

    if u in {"0", "NONE", "NO", "NO GG", "NO GUARANTEED GAMES"}:
        return "None"

    if u.isdigit() and u in _GG_CANON_SET:
        return u

    m = re.search(r"(\d+)", u)
    if m:
        n = m.group(1)
        if n in _GG_CANON_SET:
            return n

    return ""


def normalize_tournament_type(value: Any) -> str:
    """Canonical tokens: 'Team', 'Individual'"""
    raw = clean_text(value)
    if not raw:
        return ""
    raw = raw.strip("(),;")
    return _canon_from_map(raw, _TYPE_CANON_MAP, _TYPE_ALIASES)


def normalize_surface(value: Any) -> str:
    """Canonical tokens: 'NA', 'Grass', 'Artificial'"""
    raw = clean_text(value)
    if not raw:
        return ""
    raw = raw.strip("(),;")
    return _canon_from_map(raw, _SURF_CANON_MAP, _SURF_ALIASES)


def normalize_yes_no_na(value: Any) -> str:
    """Canonical tokens: 'NA', 'Yes', 'No'"""
    raw = clean_text(value)
    if raw == "":
        return "NA"
    raw = raw.strip("(),;")
    u = raw.upper()
    if u in _YESNO_ALIASES:
        return _YESNO_ALIASES[u]
    return _YESNO_CANON_MAP.get(u, "NA")


def normalize_gate_fees(value: Any) -> str:
    """
    Gate-fees is a Yes/No/NA dropdown in the export.

    Rules:
    - blank/missing -> NA
    - explicit "no gate fee" style text -> No
    - any concrete fee text or gate-fee mention -> Yes
    - already-normalized Yes/No/NA stays as-is
    """
    raw = clean_text(value)
    if raw == "":
        return "NA"

    canon = normalize_yes_no_na(raw)
    if canon in {"Yes", "No", "NA"} and raw.upper() in set(_YESNO_ALIASES) | set(_YESNO_CANON_MAP):
        return canon

    upper = raw.upper()
    if "NO GATE FEE" in upper or "NO GATE FEES" in upper:
        return "No"

    if "GATE FEE" in upper or "GATE FEES" in upper or re.search(r"\$\s*\d", raw):
        return "Yes"

    return "NA"


def normalize_date_mmddyyyy(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    fmts = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%b %d %Y",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.strftime("%m/%d/%Y")
        except ValueError:
            continue

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{int(mm):02d}/{int(dd):02d}/{yyyy}"

    return ""


def normalize_state_full_name(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    st = states.lookup(raw)
    return st.name if st else raw


def normalize_required_address(value: Any) -> str:
    raw = clean_text(value)
    if not raw:
        return "N/A"

    upper = raw.upper()
    if upper in {"N/A", "NA", "NONE", "NULL", "-", "--", "NOT AVAILABLE", "NOT APPLICABLE"}:
        return "N/A"

    return raw


def normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize all known dropdown-constrained fields in a single event row dict.
    Returns a COPY of the dict (does not mutate the input).
    """
    out = dict(row)

    # Dates
    if "Start-date" in out:
        out["Start-date"] = normalize_date_mmddyyyy(out.get("Start-date"))
    if "End-date" in out:
        out["End-date"] = normalize_date_mmddyyyy(out.get("End-date"))

    # State
    if "State-FullName" in out:
        out["State-FullName"] = normalize_state_full_name(out.get("State-FullName"))

    if "Address" in out:
        out["Address"] = normalize_required_address(out.get("Address"))

    # Single dropdown columns
    if "Guaranteed-games" in out:
        out["Guaranteed-games"] = normalize_guaranteed_games(out.get("Guaranteed-games"))

    if "Tournament-type" in out:
        out["Tournament-type"] = normalize_tournament_type(out.get("Tournament-type"))

    if "Tournament-surface" in out:
        out["Tournament-surface"] = normalize_surface(out.get("Tournament-surface"))

    if "Hotel-required" in out:
        out["Hotel-required"] = normalize_yes_no_na(out.get("Hotel-required"))

    if "Are-there-multiple_locations" in out:
        out["Are-there-multiple_locations"] = normalize_yes_no_na(out.get("Are-there-multiple_locations"))

    if "Gate-fees" in out:
        out["Gate-fees"] = normalize_gate_fees(out.get("Gate-fees"))

    # Repeating age/fee columns
    for i in range(1, 14):
        age_key = f"Age-groups.{i}"
        if age_key in out:
            out[age_key] = normalize_age_group(out.get(age_key))

    # Repeating competition level columns
    for i in range(1, 10):
        comp_key = f"Competition-level.{i}"
        if comp_key in out:
            out[comp_key] = normalize_comp_level(out.get(comp_key))

    return out


def normalize_rows(rows: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Convenience helper for lists."""
    return [normalize_row(r) for r in rows]
