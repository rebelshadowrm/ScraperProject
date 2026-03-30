import json
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

from scrapers.base_scraper import BaseScraper
from common.config import OUTPUT_COLUMNS
from common.normalize import (
    clean_text,
    normalize_age_group,
    normalize_comp_level,
    normalize_state_full_name,
)

logger = logging.getLogger(__name__)


ALL_50_STATES = [
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
]


def _first_non_empty(*values) -> str:
    for v in values:
        s = clean_text(v)
        if s:
            return s
    return ""


def _normalize_phone_basic(value: str) -> str:
    raw = clean_text(value)
    if not raw:
        return ""

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]

    if len(digits) == 10:
        return f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}"

    return raw


def _looks_like_event_dict(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False

    url_key = clean_text(obj.get("url_key"))
    if not url_key:
        return False

    return any(
        key in obj
        for key in (
            "id",
            "event_id",
            "name",
            "label",
            "start_date",
            "end_date",
            "divisions",
            "venues",
            "contacts",
        )
    )


def _walk_event_objects(blob: Any) -> Iterable[Dict[str, Any]]:
    if blob is None:
        return

    if isinstance(blob, list):
        for item in blob:
            yield from _walk_event_objects(item)
        return

    if not isinstance(blob, dict):
        return

    if _looks_like_event_dict(blob):
        yield blob
        return

    for key in ("pages", "payload", "events", "data", "eventlist", "items", "results"):
        if key in blob:
            yield from _walk_event_objects(blob.get(key))

    for value in blob.values():
        if isinstance(value, (list, dict)):
            yield from _walk_event_objects(value)


def _city_tokens(value: str) -> List[str]:
    raw = clean_text(value)
    if not raw:
        return []

    tokens = []
    for token in raw.lower().replace(".", "").split():
        if token == "mount":
            tokens.append("mt")
        elif token == "saint":
            tokens.append("st")
        else:
            tokens.append(token)
    return tokens


def _city_matches(candidate: str, structured_city: str) -> bool:
    cand_tokens = _city_tokens(candidate)
    city_tokens = _city_tokens(structured_city)
    return bool(cand_tokens and city_tokens and cand_tokens == city_tokens)


def _clean_street_address(address1: str, city: str, state: str, postal_code: str) -> str:
    raw = clean_text(address1)
    if not raw:
        return ""

    city = clean_text(city)
    state = clean_text(state)
    postal_code = clean_text(postal_code)

    parts = [clean_text(p) for p in raw.split(",") if clean_text(p)]
    if len(parts) <= 1:
        return raw

    if parts and parts[-1].upper() == "USA":
        parts.pop()

    if parts:
        tail = parts[-1]
        tail_upper = tail.upper()

        state_upper = state.upper()
        state_full_upper = normalize_state_full_name(state).upper() if state else ""

        zip_match = bool(postal_code and re.search(rf"\b{re.escape(postal_code)}\b", tail))
        state_match = bool(
            (state_upper and re.search(rf"\b{re.escape(state_upper)}\b", tail_upper))
            or (state_full_upper and re.search(rf"\b{re.escape(state_full_upper)}\b", tail_upper))
        )

        if zip_match or state_match:
            parts.pop()

    if parts and city and _city_matches(parts[-1], city):
        parts.pop()

    cleaned = ", ".join(parts).strip(" ,")
    return cleaned or raw


class Play365BaseScraper(BaseScraper):
    """
    Shared local-dump scraper for Play365-family sites.
    Site-specific sibling scrapers should override:
      - SCRAPER_NAME
      - DOMAIN
      - UTILITIES_SUBDIR
      - DUMP_FILENAME
      - SCRIPT_FILENAME
      - FORCE_SPORT_ID (optional)
    """

    SCRAPER_NAME = "Play365Base"
    DYNAMIC_LOCATIONS = True
    BATCH_DELAY_SECONDS = 0

    DOMAIN = ""
    UTILITIES_SUBDIR = "Play365Utilities/Generic"
    DUMP_FILENAME = "play365_dump.json"
    SCRIPT_FILENAME = "export_play365.js"
    FORCE_SPORT_ID: Optional[int] = 1

    def __init__(self):
        self.events_by_url: Dict[str, Dict[str, Any]] = {}
        self._loaded_dump_path: Optional[str] = None
        self._loaded_dump_mtime: Optional[float] = None
        self._all_events_cache: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # Paths / locations
    # -------------------------------------------------------------------------

    def _project_root(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def get_dynamic_locations(self) -> Dict[str, str]:
        dump_rel = f"./{self.UTILITIES_SUBDIR}/{self.DUMP_FILENAME}"
        return {
            f"{state_name} ({self.SCRAPER_NAME})": f"{dump_rel}?state={state_abbr}"
            for state_abbr, state_name in ALL_50_STATES
        }

    def _parse_base_url(self, base_url: str) -> Tuple[str, Dict[str, str]]:
        raw = clean_text(base_url).strip().strip('"').strip("'")
        path_part, _, query_part = raw.partition("?")

        if not os.path.isabs(path_part):
            path_part = os.path.join(self._project_root(), path_part)

        abs_path = os.path.abspath(os.path.expanduser(path_part))

        params = {}
        if query_part:
            parsed = parse_qs(query_part, keep_blank_values=True)
            params = {k: (v[0] if v else "") for k, v in parsed.items()}

        return abs_path, params

    def _utilities_dir_from_dump_path(self, dump_path: str) -> str:
        return os.path.dirname(dump_path)

    # -------------------------------------------------------------------------
    # Browser export script generation
    # -------------------------------------------------------------------------

    def _build_export_script(self) -> str:
        states_json = json.dumps([abbr for abbr, _ in ALL_50_STATES])
        force_sport = "null" if self.FORCE_SPORT_ID is None else str(self.FORCE_SPORT_ID)

        return f"""(async () => {{
  const STATES = {states_json};
  const MAX_RETRIES = 20;
  const FORCE_SPORT_ID = {force_sport};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  function findAjaxSeed() {{
    try {{
      const entries = performance.getEntriesByType("resource")
        .map(e => e.name)
        .filter(u => u && u.includes("/ajax-events"));

      if (entries.length) {{
        const preferred =
          entries.find(u => u.includes("season_ids=") && u.includes("page=1")) ||
          entries.find(u => u.includes("page=1")) ||
          entries[entries.length - 1];

        const url = new URL(preferred);
        return {{
          ajaxUrl: `${{url.origin}}${{url.pathname}}`,
          params: Object.fromEntries(url.searchParams.entries())
        }};
      }}
    }} catch (e) {{
      console.log("Could not inspect ajax-events request from performance:", e);
    }}

    try {{
      const fallbackUrl = `${{location.origin}}/ajax-events`;
      const params = Object.fromEntries(new URLSearchParams(location.search).entries());
      return {{
        ajaxUrl: fallbackUrl,
        params
      }};
    }} catch (e) {{
      console.log("Could not build fallback ajax URL from current page:", e);
      return null;
    }}
  }}

  const seed = findAjaxSeed();
  if (!seed) {{
    alert(
      "Could not detect /ajax-events parameters.\\n\\n" +
      "Open the target site page first, wait until listings load, " +
      "apply any season/year filter you want, then run this script again."
    );
    return;
  }}

  console.log("Using ajax endpoint:", seed.ajaxUrl);
  console.log("Seed params:", seed.params);

  async function fetchState(state) {{
    const params = new URLSearchParams(seed.params || {{}});

    params.set("layout", params.get("layout") || "medium");
    params.set("past_events", params.get("past_events") || "false");
    params.set("events_exits", "");
    params.set("state", state);

    if (FORCE_SPORT_ID !== null) {{
      params.set("sport_id", String(FORCE_SPORT_ID));
    }}

    const allPages = [];
    let page = 1;
    let retries = 0;

    while (true) {{
      params.set("page", String(page));
      const url = `${{seed.ajaxUrl}}?${{params.toString()}}`;

      let resp;
      try {{
        resp = await fetch(url, {{
          credentials: "include",
          headers: {{
            "x-requested-with": "XMLHttpRequest"
          }}
        }});
      }} catch (e) {{
        console.log(`[${{state}}] Network error on page ${{page}}:`, e);
        retries++;
        if (retries > MAX_RETRIES) break;
        await sleep(3000);
        continue;
      }}

      if (resp.status === 403) {{
        console.log(`[${{state}}] 403 on page ${{page}}. Solve any challenge in this tab, then wait...`);
        retries++;
        if (retries > MAX_RETRIES) break;
        await sleep(5000);
        continue;
      }}

      retries = 0;

      const text = await resp.text();
      let data;
      try {{
        data = JSON.parse(text);
      }} catch (e) {{
        console.log(`[${{state}}] Non-JSON on page ${{page}}. Stopping.`);
        break;
      }}

      let eventCount = 0;
      if (Array.isArray(data)) {{
        eventCount = data.length;
      }} else if (Array.isArray(data?.events)) {{
        eventCount = data.events.length;
      }} else if (Array.isArray(data?.data)) {{
        eventCount = data.data.length;
      }} else if (Array.isArray(data?.eventlist)) {{
        eventCount = data.eventlist.length;
      }} else if (Array.isArray(data?.payload)) {{
        eventCount = data.payload.length;
      }} else if (Array.isArray(data?.payload?.events)) {{
        eventCount = data.payload.events.length;
      }} else if (Array.isArray(data?.payload?.data)) {{
        eventCount = data.payload.data.length;
      }} else if (Array.isArray(data?.payload?.eventlist)) {{
        eventCount = data.payload.eventlist.length;
      }} else if (typeof data?.html === "string") {{
        eventCount = (data.html.match(/data-event-id=/g) || []).length;
      }} else if (typeof data?.payload?.html === "string") {{
        eventCount = (data.payload.html.match(/data-event-id=/g) || []).length;
      }}

      if (eventCount === 0) {{
        console.log(`[${{state}}] No more events at page ${{page}}. Stopping.`);
        break;
      }}

      console.log(`[${{state}}] Saved page ${{page}} (${{eventCount}} events)`);

      allPages.push({{
        state,
        page,
        url,
        fetchedAt: new Date().toISOString(),
        payload: data
      }});

      page++;
      await sleep(500);
    }}

    return allPages;
  }}

  const dump = {{
    generated_at: new Date().toISOString(),
    source: location.href,
    domain: location.origin,
    forced_sport_id: FORCE_SPORT_ID,
    states: STATES,
    seed_params: seed.params || {{}},
    pages: []
  }};

  for (const state of STATES) {{
    console.log(`Starting export for ${{state}}...`);
    const pages = await fetchState(state);
    dump.pages.push(...pages);
    console.log(`Done with ${{state}}. Added ${{pages.length}} page(s).`);
    await sleep(1000);
  }}

  const out = JSON.stringify(dump, null, 2);
  const blob = new Blob([out], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "{self.DUMP_FILENAME}";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);

  console.log("All states complete. Downloaded {self.DUMP_FILENAME}");
}})();
"""

    def _write_export_script(self, dump_path: str) -> str:
        utilities_dir = self._utilities_dir_from_dump_path(dump_path)
        os.makedirs(utilities_dir, exist_ok=True)

        script_path = os.path.join(utilities_dir, self.SCRIPT_FILENAME)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(self._build_export_script())

        logger.info("[%s] Wrote browser export script to: %s", self.SCRAPER_NAME, script_path)
        return script_path

    # -------------------------------------------------------------------------
    # Dump loading / caching
    # -------------------------------------------------------------------------

    def _load_dump_if_needed(self, dump_path: str) -> None:
        if not os.path.exists(dump_path):
            raise FileNotFoundError(dump_path)

        mtime = os.path.getmtime(dump_path)
        if self._loaded_dump_path == dump_path and self._loaded_dump_mtime == mtime:
            return

        with open(dump_path, "r", encoding="utf-8") as f:
            blob = json.load(f)

        self._all_events_cache = list(_walk_event_objects(blob))
        self._loaded_dump_path = dump_path
        self._loaded_dump_mtime = mtime

        logger.info(
            "[%s] Loaded %s raw event object(s) from dump: %s",
            self.SCRAPER_NAME,
            len(self._all_events_cache),
            dump_path,
        )

    def _ensure_dump_exists(self, dump_path: str) -> None:
        if os.path.exists(dump_path):
            return

        script_path = self._write_export_script(dump_path)

        logger.warning("[%s] Required dump file not found: %s", self.SCRAPER_NAME, dump_path)
        logger.warning("[%s] Browser export script generated at: %s", self.SCRAPER_NAME, script_path)

        try:
            input(
                f"\n{self.SCRAPER_NAME} dump is missing.\n"
                f"Run the generated browser script from: {script_path}\n"
                f"Place/save the resulting {self.DUMP_FILENAME} in: {os.path.dirname(dump_path)}\n"
                "Press Enter after the dump file is ready..."
            )
        except EOFError:
            pass

        if not os.path.exists(dump_path):
            raise FileNotFoundError(
                f"{self.SCRAPER_NAME} dump still not found after prompt: {dump_path}"
            )

    # -------------------------------------------------------------------------
    # BaseScraper interface
    # -------------------------------------------------------------------------

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        dump_path, params = self._parse_base_url(base_url)
        target_state = clean_text(params.get("state")).upper()

        if not target_state:
            raise ValueError(f"{self.SCRAPER_NAME} base_url is missing ?state=XX")

        self._ensure_dump_exists(dump_path)
        self._load_dump_if_needed(dump_path)

        self.events_by_url.clear()

        for event in self._all_events_cache:
            sport_id = event.get("sport_id")
            if self.FORCE_SPORT_ID is not None and sport_id not in (self.FORCE_SPORT_ID, str(self.FORCE_SPORT_ID), None):
                continue

            event_state = clean_text(_first_non_empty(event.get("state"))).upper()
            if not event_state:
                venues = event.get("venues") or []
                if venues:
                    event_state = clean_text(_first_non_empty(venues[0].get("state"))).upper()

            if event_state != target_state:
                continue

            url_key = clean_text(event.get("url_key"))
            if not url_key:
                continue

            event_url = _build_event_url(self.DOMAIN, url_key)
            self.events_by_url[event_url] = event

        links = sorted(self.events_by_url.keys())
        logger.info(
            "[%s] Returning %s event link(s) for state %s from local dump.",
            self.SCRAPER_NAME,
            len(links),
            target_state,
        )
        return links

    def extract_event_details(self, url: str, driver=None, **kwargs) -> Optional[Dict[str, Any]]:
        event = self.events_by_url.get(url)
        if not event:
            logger.error("[%s] Event details for URL not found in cached data: %s", self.SCRAPER_NAME, url)
            return None

        data = {col: "" for col in OUTPUT_COLUMNS}

        venues = event.get("venues") or []
        contacts = event.get("contacts") or []
        divisions = event.get("divisions") or []

        first_venue = venues[0] if venues else {}

        city = _first_non_empty(first_venue.get("city"), event.get("city"))
        raw_state = _first_non_empty(first_venue.get("state"), event.get("state"))
        postal_code = clean_text(first_venue.get("postal_code"))

        address = _clean_street_address(
            first_venue.get("address1"),
            city,
            raw_state,
            postal_code,
        )
        address2 = clean_text(first_venue.get("address2"))
        state_full = normalize_state_full_name(raw_state)
        venue_name = clean_text(first_venue.get("name"))

        multiple_locations = "Yes" if len(venues) > 1 else "No"

        additional_locations_parts = []
        if len(venues) > 1:
            for venue in venues[1:]:
                nm = clean_text(venue.get("name"))
                v_city = clean_text(venue.get("city"))
                v_state_raw = clean_text(venue.get("state"))
                v_zip = clean_text(venue.get("postal_code"))
                v_addr = _clean_street_address(venue.get("address1"), v_city, v_state_raw, v_zip)
                v_state = normalize_state_full_name(v_state_raw)

                bits = [x for x in [v_addr, v_city, v_state, v_zip] if x]
                line = " - ".join([x for x in [nm, ", ".join(bits)] if x])
                if line:
                    additional_locations_parts.append(line)

        contact_name = ""
        phone_number = ""
        email = ""
        if contacts:
            first_contact = contacts[0]
            contact_name = " ".join(
                x for x in [
                    clean_text(first_contact.get("first_name")),
                    clean_text(first_contact.get("last_name")),
                ] if x
            )
            phone_number = _normalize_phone_basic(
                _first_non_empty(first_contact.get("mobile"), first_contact.get("telephone"))
            )
            email = clean_text(first_contact.get("email"))

        data["Tournament-name"] = clean_text(event.get("name") or event.get("label"))
        data["Start-date"] = clean_text(event.get("start_date"))
        data["End-date"] = clean_text(event.get("end_date"))
        data["Are-there-multiple_locations"] = multiple_locations
        data["Address"] = address
        data["Address-2"] = address2
        data["Venue"] = "Multiple Locations" if multiple_locations == "Yes" else venue_name
        data["City"] = city
        data["State-FullName"] = state_full
        data["Zip"] = postal_code
        data["Guaranteed-games"] = clean_text(event.get("game_guarantee"))
        data["Gate-fees"] = "NA"
        data["Hotel-required"] = "Yes" if int(event.get("stay_to_play") or 0) == 1 else "No"
        data["Tournament-type"] = "Team"
        data["Tournament-surface"] = "NA"
        data["Contact-name"] = contact_name
        data["Phone-number"] = phone_number
        data["Email"] = email
        data["Website-url"] = url
        data["More-information-and-additional-tournament-locations"] = "; ".join(additional_locations_parts)

        division_rows: List[Dict[str, str]] = []

        for div in divisions:
            age_group_raw = clean_text(div.get("label"))
            division_price = clean_text(div.get("price"))
            division_level_raw = _first_non_empty(div.get("level"), div.get("team_label"))

            nested_levels = div.get("team_levels") or []
            candidate_rows: List[Tuple[str, str, str]] = []

            if nested_levels:
                for lvl in nested_levels:
                    nested_level_raw = _first_non_empty(
                        lvl.get("label"),
                        div.get("level"),
                        div.get("team_label"),
                    )
                    nested_price = clean_text(_first_non_empty(lvl.get("price"), division_price))
                    candidate_rows.append((age_group_raw, nested_price, nested_level_raw))
            else:
                candidate_rows.append((age_group_raw, division_price, division_level_raw))

            for age_raw, fee_raw, level_raw in candidate_rows:
                if not clean_text(age_raw):
                    continue

                level_parts = [clean_text(p) for p in re.split(r"/|,|\|", clean_text(level_raw)) if clean_text(p)]
                if not level_parts:
                    level_parts = [""]

                for level_part in level_parts:
                    division_rows.append(
                        {
                            "age_group": clean_text(age_raw),
                            "entry_fee": clean_text(fee_raw),
                            "competition_level": level_part,
                        }
                    )

        unique_ages: List[str] = []
        age_first_fee: Dict[str, str] = {}
        unique_levels: List[str] = []

        for row in division_rows:
            raw_age = clean_text(row.get("age_group"))
            raw_fee = clean_text(row.get("entry_fee"))
            raw_level = clean_text(row.get("competition_level"))

            age_norm = normalize_age_group(raw_age)
            if age_norm and age_norm not in unique_ages:
                unique_ages.append(age_norm)

            if age_norm and age_norm not in age_first_fee:
                age_first_fee[age_norm] = raw_fee

            for level_part in [clean_text(p) for p in re.split(r"/|,|\|", raw_level) if clean_text(p)] or [""]:
                level_norm = normalize_comp_level(level_part)
                if level_norm and level_norm not in unique_levels:
                    unique_levels.append(level_norm)

        for i in range(1, 14):
            idx = i - 1
            if idx < len(unique_ages):
                age_val = unique_ages[idx]
                data[f"Age-groups.{i}"] = age_val
                data[f"Entry-fee.{i}"] = age_first_fee.get(age_val, "")
            else:
                data[f"Age-groups.{i}"] = ""
                data[f"Entry-fee.{i}"] = ""

        for i in range(1, 10):
            idx = i - 1
            if idx < len(unique_levels):
                data[f"Competition-level.{i}"] = unique_levels[idx]
            else:
                data[f"Competition-level.{i}"] = ""

        data["Select All Competition Levels That Apply."] = ""

        for col in OUTPUT_COLUMNS:
            if col not in data:
                data[col] = ""

        return data

    def get_event_key(self, url: str) -> str:
        match = re.search(r"/events/([^/?#]+)/?$", url)
        if match:
            return match.group(1)
        return url