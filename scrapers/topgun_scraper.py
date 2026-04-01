# scrapers/topgun_scraper.py

import os
import json
import logging
import requests
import re
from datetime import datetime, date
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs
from us import states

from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

def _parse_iso_date(iso_str: str) -> Optional[date]:
    """
    Example: "2025-02-16T00:00:00.000Z"
    """
    if not iso_str:
        return None
    try:
        # safest for Z + millis
        dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S.%fZ")
        return dt.date()
    except Exception:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return dt.date()
        except Exception:
            return None


def _fmt_mmddyyyy(d: Optional[date]) -> str:
    return d.strftime("%m/%d/%Y") if d else ""


def _fmt_mm_dd_yyyy(d: Optional[date]) -> str:
    return d.strftime("%m-%d-%Y") if d else ""


def _normalize_url(u: str) -> str:
    return (u or "").strip()


def _safe_json_loads(maybe_json_str: str):
    if not maybe_json_str:
        return None
    if isinstance(maybe_json_str, (dict, list)):
        return maybe_json_str
    try:
        return json.loads(maybe_json_str)
    except Exception:
        return None


def _extract_state_from_citystate(city_state: str) -> str:
    """
    CityState example: "Snow Hill/Goldsboro, NC"
    """
    if not city_state:
        return ""
    m = re.search(r",\s*([A-Z]{2})\s*$", city_state.strip())
    return m.group(1) if m else ""


def _parse_complex_location(loc: str) -> Dict[str, str]:
    """
    Example: "871 Buck Swamp Rd Goldsboro, NC 27530"
    Best-effort parse: address (everything before city), city, state, zip.
    """
    out = {"Address": "", "City": "", "State": "", "Zip": ""}

    if not loc:
        return out

    s = " ".join(str(loc).split()).strip()

    # Try "..., ST ZIP"
    m = re.search(r",\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$", s)
    if m:
        out["State"] = m.group(1)
        out["Zip"] = m.group(2)
        left = s[: m.start()].strip()
        # left looks like "871 ... Goldsboro"
        # heuristic: last token(s) after last space = city (not perfect)
        parts = left.split(" ")
        if len(parts) >= 2:
            out["City"] = parts[-1].strip()
            out["Address"] = " ".join(parts[:-1]).strip()
        else:
            out["Address"] = left
        return out

    # Try "..., ST"
    m2 = re.search(r",\s*([A-Z]{2})\s*$", s)
    if m2:
        out["State"] = m2.group(1)
        left = s[: m2.start()].strip()
        parts = left.split(" ")
        if len(parts) >= 2:
            out["City"] = parts[-1].strip()
            out["Address"] = " ".join(parts[:-1]).strip()
        else:
            out["Address"] = left
        return out

    # fallback: whole string as address
    out["Address"] = s
    return out


def _normalize_age_group_token(raw: str) -> str:
    """
    TopGun examples: "8U CP", "13U 80", "HSU"
    We ignore HSU; for others, extract leading NN + U/u => "nnu"
    """
    if not raw:
        return ""
    s = raw.strip()
    m = re.match(r"^(\d{1,2})\s*[Uu]\b", s)
    if not m:
        return ""  # ignore unknown (including HSU)
    n = int(m.group(1))
    # keep within typical range; adjust if your importer supports broader
    if 6 <= n <= 18:
        return f"{n}u"
    return ""


class TopGunScraper(BaseScraper):
    SCRAPER_NAME = "TopGun"
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "dynamic"

    FILTER_DATA_URL = "https://topgunstats.com/api/queries/tournaments/tournament-filter-data"
    TOURNAMENT_DATA_URL = "https://topgunstats.com/api/queries/tournaments/tournament-data"
    TOURNAMENTS_PAGE_BASE = "https://topgunstats.com/tournaments"
    DYNAMIC_LOCATIONS = True

    def __init__(self):
        self.api_key = "secret"

        # Cache: synthetic_url -> tournament_obj
        self.tournaments_by_url: Dict[str, Dict] = {}

        # Lookup caches
        self._filter_loaded = False
        self._state_to_location_ids: Dict[str, List[int]] = {}
        self._type_id_to_name: Dict[int, str] = {}
        self._banned_type_ids: set[int] = set()

    def _get(self, url: str, params: Optional[Dict] = None) -> Dict:
        params = dict(params or {})
        params["api_key"] = self.api_key
        r = requests.get(url, params=params, headers={"Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    def _load_filter_data(self):
        if self._filter_loaded:
            return

        data = self._get(self.FILTER_DATA_URL)

        locs = data.get("locations") or data.get("Locations") or []
        types = data.get("types") or data.get("TournamentTypes") or []

        # Build type_id -> name + banned_type_ids (contains "umpire")
        type_map: Dict[int, str] = {}
        banned = set()
        for t in types:
            tid = t.get("TournamentTypeID") or t.get("tournamentTypeID") or t.get("id")
            name = (t.get("TournamentTypeName") or t.get("Name") or t.get("name") or "").strip()
            if tid is None:
                continue
            try:
                tid_i = int(tid)
            except Exception:
                continue
            type_map[tid_i] = name
            if "umpire" in name.lower():
                banned.add(tid_i)


        # Build state -> location ids
        state_map: Dict[str, List[int]] = {}
        for loc in locs:
            try:
                loc_id = int(loc.get("LocationID") or loc.get("locationID") or loc.get("id"))
            except Exception:
                continue

            citystate = loc.get("CityState") or loc.get("cityState") or ""
            st = _extract_state_from_citystate(citystate)
            if not st:
                continue
            state_map.setdefault(st, []).append(loc_id)

        self._state_to_location_ids = state_map

        # Build type_id -> name + banned_type_ids (contains "umpire")
        type_map: Dict[int, str] = {}
        banned = set()
        for t in types:
            tid = t.get("TournamentTypeID") or t.get("tournamentTypeID") or t.get("id")
            name = (t.get("Name") or t.get("name") or "").strip()
            if tid is None:
                continue
            try:
                tid_i = int(tid)
            except Exception:
                continue
            type_map[tid_i] = name
            if "umpire" in name.lower():
                banned.add(tid_i)

        self._type_id_to_name = type_map
        self._banned_type_ids = banned

        self._filter_loaded = True
        logger.info(
            "[TopGun] Filter-data loaded. States=%d, Types=%d, BannedTypes(umpire)=%d",
            len(self._state_to_location_ids),
            len(self._type_id_to_name),
            len(self._banned_type_ids),
        )

    def get_dynamic_locations(self) -> Dict[str, str]:
        """
        Build state list from filter-data and return it in the same shape as locations.json:
        { "North Carolina (TopGun)": "https://topgunstats.com/tournaments?state=NC&sport=1", ... }
        """
        self._load_filter_data()

        # TopGun is baseball=1 for your use case
        sport_id = 1

        # States we actually have LocationIDs for
        states = sorted(self._state_to_location_ids.keys())

        # Optional: map abbrev to pretty names (you can leave abbrev if you want)
        state_name = {
            "AL": "Alabama", "AR": "Arkansas", "AZ": "Arizona", "CA": "California", "CO": "Colorado",
            "CT": "Connecticut", "DC": "District of Columbia", "DE": "Delaware", "FL": "Florida",
            "GA": "Georgia", "IA": "Iowa", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana",
            "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "MA": "Massachusetts",
            "MD": "Maryland", "ME": "Maine", "MI": "Michigan", "MN": "Minnesota", "MO": "Missouri",
            "MS": "Mississippi", "MT": "Montana", "NC": "North Carolina", "ND": "North Dakota",
            "NE": "Nebraska", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
            "NV": "Nevada", "NY": "New York", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
            "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
            "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VA": "Virginia", "VT": "Vermont",
            "WA": "Washington", "WI": "Wisconsin", "WV": "West Virginia", "WY": "Wyoming",
        }

        dynamic = {}
        for st in states:
            label_state = state_name.get(st, st)
            label = f"{label_state} ({self.SCRAPER_NAME})"
            dynamic[label] = f"{self.TOURNAMENTS_PAGE_BASE}?state={st}&sport={sport_id}"

        return dynamic

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        """
        base_url convention: you can put ?state=CA&sport=1 (like FiveTool)
        We'll expand state -> location IDs using filter-data, then filter the get-all tournament-data locally.
        """
        self._load_filter_data()

        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        state = (qs.get("state", [""])[0] or "").upper().strip()
        sport = qs.get("sport", ["1"])[0].strip()

        if not state:
            raise ValueError("TopGun base_url must include ?state=XX (e.g. ?state=CA&sport=1)")

        try:
            sport_id = int(sport)
        except Exception:
            sport_id = 1

        location_ids = set(self._state_to_location_ids.get(state, []))
        if not location_ids:
            logger.warning("[TopGun] No LocationIDs found for state=%s. Returning no events.", state)
            return []

        all_tournaments = self._get(self.TOURNAMENT_DATA_URL)
        # The API might return a list directly or wrap in a property
        if isinstance(all_tournaments, dict):
            tournaments = (
                all_tournaments.get("Tournaments")
                or all_tournaments.get("tournaments")
                or all_tournaments.get("data")
                or []
            )
        else:
            tournaments = all_tournaments

        today = date.today()
        out_urls: List[str] = []
        self.tournaments_by_url.clear()

        for t in tournaments:
            try:
                # basic fields
                if t.get("IsHidden") is True:
                    continue

                # sport filter
                if int(t.get("SportID") or 0) != sport_id:
                    continue

                # state filter via location id membership
                loc_id = int(t.get("LocationID") or 0)
                if loc_id not in location_ids:
                    continue

                # date filter: exclude past events (end date < today)
                end_d = _parse_iso_date(t.get("EndDate") or "")
                if end_d and end_d < today:
                    continue

                # umpire filter by type + name fallback
                type_id = int(t.get("TournamentTypeID") or 0)
                type_name = (self._type_id_to_name.get(type_id) or t.get("Name") or "").strip()
                tourney_name = (t.get("TournamentName") or "").strip()

                if type_id in self._banned_type_ids:
                    continue
                if "umpire" in (type_name.lower() + " " + tourney_name.lower()):
                    continue

                # build synthetic “event url”
                start_d = _parse_iso_date(t.get("StartDate") or "")
                start_q = _fmt_mm_dd_yyyy(start_d)
                end_q = _fmt_mm_dd_yyyy(end_d)

                # Note: no directors param for now (ContactIDs can be absent in filter-data)
                synthetic_url = (
                    f"{self.TOURNAMENTS_PAGE_BASE}"
                    f"?sport={sport_id}"
                    f"&locations={loc_id}"
                    f"&types={type_id}"
                    f"&startdate={start_q}"
                    f"&enddate={end_q}"
                )

                synthetic_url = _normalize_url(synthetic_url)
                self.tournaments_by_url[synthetic_url] = t
                out_urls.append(synthetic_url)

            except Exception:
                # don't let one bad entry kill the run
                continue

        logger.info("[TopGun] Collected %d tournaments for state=%s sport=%d", len(out_urls), state, sport_id)
        return out_urls

    def extract_event_details(self, url: str, driver, **kwargs) -> Dict:
        """
        Looks up the tournament object cached by collect_event_links and maps it to your template fields.
        """
        t = self.tournaments_by_url.get(url)
        if not t:
            logger.error("[TopGun] Tournament for URL not in cache: %s", url)
            return None

        start_d = _parse_iso_date(t.get("StartDate") or "")
        end_d = _parse_iso_date(t.get("EndDate") or "")

        # Complexes
        complexes = _safe_json_loads(t.get("TComplexArr") or "") or []
        # Fee/Age groups
        fee_objs = _safe_json_loads(t.get("TFeeObj") or "") or []

        # Address defaults
        multiple_locations = "No"
        venue = ""
        address = ""
        city = ""
        state_full = ""  # your column is State-FullName; you can keep "NC" if your normalization handles it
        zip_code = ""
        more_locations = ""

        if isinstance(complexes, list) and len(complexes) > 0:
            first = complexes[0]
            venue = (first.get("name") or "").strip()
            loc_str = (first.get("location") or "").strip()
            parsed = _parse_complex_location(loc_str)
            address = parsed["Address"]
            city = parsed["City"]
            raw_state = (parsed["State"] or "").strip()
            if raw_state:
                state_obj = states.lookup(raw_state)
                state_full = state_obj.name if state_obj else raw_state
            else:
                state_full = ""
            zip_code = parsed["Zip"]

            if len(complexes) > 1:
                multiple_locations = "Yes"
                parts = []
                for c in complexes:
                    nm = (c.get("name") or "").strip()
                    lc = (c.get("location") or "").strip()
                    if nm and lc:
                        parts.append(f"{nm} - {lc}")
                    elif lc:
                        parts.append(lc)
                more_locations = "; ".join(parts)

        # Tournament type name
        type_id = int(t.get("TournamentTypeID") or 0)
        type_name = (self._type_id_to_name.get(type_id) or t.get("Name") or "").strip()

        # Build base event dict with template keys
        event = {k: "" for k in getattr(__import__("common.config", fromlist=["OUTPUT_COLUMNS"]), "OUTPUT_COLUMNS")}

        event["Tournament-name"] = (t.get("TournamentName") or "").strip()
        event["Start-date"] = _fmt_mmddyyyy(start_d)
        event["End-date"] = _fmt_mmddyyyy(end_d)

        event["Are-there-multiple_locations"] = multiple_locations
        event["Venue"] = venue
        event["Address"] = address
        event["City"] = city
        event["State-FullName"] = state_full
        event["Zip"] = zip_code

        # Age groups + fees (flatten)
        # Fee objects look like: [{ "Fee": "0", "AgeGroups": ["8U CP", ...] }]
        slot = 1
        for fo in fee_objs if isinstance(fee_objs, list) else []:
            fee = str(fo.get("Fee") or "").strip()
            ages = fo.get("AgeGroups") or []
            if not isinstance(ages, list):
                continue
            for raw_age in ages:
                age_token = _normalize_age_group_token(str(raw_age))
                if not age_token:
                    continue  # ignore HSU/unknowns
                if slot > 13:
                    break
                event[f"Age-groups.{slot}"] = age_token
                event[f"Entry-fee.{slot}"] = fee
                slot += 1
            if slot > 13:
                break

        # Competition levels: not provided by TopGun data → leave blank
        # Guaranteed games
        gg = t.get("GameGuarantee")
        event["Guaranteed-games"] = "" if gg is None else str(gg).strip()

        # Tournament type / surface
        event["Tournament-type"] = type_name
        event["Tournament-surface"] = ""

        # Contact fields
        event["Contact-name"] = (t.get("FullName") or "").strip()
        event["Phone-number"] = (t.get("CellPhone") or "").strip()
        event["Email"] = (t.get("Email") or "").strip()

        # Redirect URL (synthetic filter URL)
        event["Website-url"] = url

        # More info: notes + additional locations list if any
        notes = (t.get("Notes") or "").strip()
        extra = []
        if notes:
            extra.append(notes)
        if more_locations:
            extra.append(more_locations)
        event["More-information-and-additional-tournament-locations"] = " | ".join(extra)

        return event

    def get_event_key(self, url: str) -> str:
        # Your runner uses the returned URL as the JSON key anyway; keep it stable
        return url
