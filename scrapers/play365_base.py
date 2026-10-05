import json
import logging
import os
import re
import shutil
import time
import hashlib
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs

import requests

from scrapers.base_scraper import BaseScraper
from common.config import OUTPUT_COLUMNS
from common.logging_utils import log_progress
from common.normalize import (
    clean_text,
    normalize_age_group,
    normalize_comp_level,
    normalize_state_full_name,
)

logger = logging.getLogger(__name__)


class Play365ManualFallbackRequired(Exception):
    pass


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


def _build_event_url(domain: str, url_key: str) -> str:
    raw_domain = clean_text(domain).rstrip("/")
    raw_key = clean_text(url_key).strip()

    if not raw_key:
        return ""

    if raw_key.startswith("http://") or raw_key.startswith("https://"):
        return raw_key

    if "/events/" in raw_key:
        if raw_key.startswith("/"):
            return f"{raw_domain}{raw_key}"
        return f"{raw_domain}/{raw_key.lstrip('/')}"

    return f"{raw_domain}/events/{raw_key.lstrip('/')}"


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

    if obj.get("start_date") and obj.get("end_date"):
        return True

    return any(
        key in obj
        for key in (
            "event_id",
            "registration_status",
            "season_id",
            "game_guarantee",
            "stay_to_play",
            "display_location",
            "schedule_link",
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
    REQUIRES_DRIVER = False
    LOCATIONS_SOURCE = "dynamic"
    DYNAMIC_LOCATIONS = True
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    BATCH_DELAY_SECONDS = 0

    DOMAIN = ""
    UTILITIES_SUBDIR = "Play365Utilities/Generic"
    DUMP_FILENAME = "play365_dump.json"
    SCRIPT_FILENAME = "export_play365.js"
    FORCE_SPORT_ID: Optional[int] = 1
    EXPORT_HTTP_METHOD = "POST"
    EXPORT_SUPPORTS_STATE_FILTER = True
    EXPORT_LAYOUT_DEFAULT = "medium"
    EXPORT_EXTRA_PARAMS: Dict[str, str] = {}
    EXPORT_TOKEN_PARAM: Optional[str] = "_token"
    PREPARE_DUMP_IN_PROMPT = False
    SUPPORTS_SELENIUM_EXPORT = False
    DIRECT_API_ENABLED = True
    DIRECT_API_HEADERS: Dict[str, str] = {
        "x-requested-with": "XMLHttpRequest",
        "user-agent": "Mozilla/5.0",
    }

    def __init__(self):
        self.events_by_url: Dict[str, Dict[str, Any]] = {}
        self._loaded_dump_path: Optional[str] = None
        self._loaded_dump_mtime: Optional[float] = None
        self._all_events_cache: List[Dict[str, Any]] = []
        self._events_by_state: Dict[str, List[Dict[str, Any]]] = {}
        self._events_loaded = False
        self._event_source_label: str = ""
        self._last_collection_source: str = ""

    # -------------------------------------------------------------------------
    # Paths / locations
    # -------------------------------------------------------------------------

    def _project_root(self) -> str:
        return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    def _default_dump_path(self) -> str:
        return os.path.join(self._project_root(), self.UTILITIES_SUBDIR, self.DUMP_FILENAME)

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

    def _manual_dump_path(self, workspace_root: str) -> str:
        return os.path.join(workspace_root, self.DUMP_FILENAME)

    def _manual_readme_path(self, workspace_root: str) -> str:
        return os.path.join(workspace_root, "README.txt")

    def _write_manual_readme(self, workspace_root: str) -> str:
        os.makedirs(workspace_root, exist_ok=True)
        path = self._manual_readme_path(workspace_root)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                "Manual Play365 Extraction\n\n"
                "1. Open the matching site in your normal browser.\n"
                "2. Open DevTools Console.\n"
                "3. Paste and run the matching export_*.js script in this folder.\n"
                "4. If the dump downloads into Downloads instead of this folder, drag it here or leave it in Downloads.\n"
                "5. Return to the scraper program and continue.\n"
            )
        return path

    def _try_recover_downloaded_dump(self, expected_dump_path: str) -> bool:
        if self._dump_is_fresh(expected_dump_path):
            return True

        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        candidate = os.path.join(downloads_dir, os.path.basename(expected_dump_path))
        if not self._dump_is_fresh(candidate):
            return False

        try:
            os.makedirs(os.path.dirname(expected_dump_path), exist_ok=True)
            from common.storage import atomic_output
            with atomic_output(expected_dump_path) as staged:
                shutil.copyfile(candidate, staged)
            logger.info("[%s] Recovered dump from Downloads: %s", self.SCRAPER_NAME, candidate)
            return True
        except Exception as e:
            logger.warning("[%s] Failed to recover dump from Downloads: %s", self.SCRAPER_NAME, e)
            return False

    # -------------------------------------------------------------------------
    # Browser export script generation
    # -------------------------------------------------------------------------

    def _export_safety_helpers(self) -> str:
        return r"""
const sourceDeadline = Date.now() + 600000;
function checkBudget(page) {
  if (page > 200 || Date.now() >= sourceDeadline) throw new Error("Source page/time budget exceeded");
}
function eventRecords(data) {
  if (Array.isArray(data)) {
    if (data.some(item => !item || typeof item !== "object" || Array.isArray(item)))
      throw new Error("Events must be record objects");
    return data;
  }
  if (data && typeof data === "object") {
    for (const key of ["eventlist", "events", "data", "payload"]) {
      if (Object.prototype.hasOwnProperty.call(data, key)) return eventRecords(data[key]);
    }
  }
  throw new Error("No recognized events list; cannot verify completeness");
}
function pageFingerprint(records) {
  return JSON.stringify(records.map(item => String(item.url_key || item.event_id || item.id || JSON.stringify(item))).sort());
}
async function fetchPage(url, options) {
  checkBudget(1);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), Math.min(30000, sourceDeadline - Date.now()));
  try {
    const response = await fetch(url, {...options, signal: controller.signal});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return JSON.parse(await response.text());
  } finally {
    clearTimeout(timer);
  }
}
"""

    def _build_export_script(self) -> str:
        states_json = json.dumps([abbr for abbr, _ in ALL_50_STATES])
        force_sport = "null" if self.FORCE_SPORT_ID is None else str(self.FORCE_SPORT_ID)
        export_method = json.dumps(self.EXPORT_HTTP_METHOD.upper())
        supports_state_filter = "true" if self.EXPORT_SUPPORTS_STATE_FILTER else "false"
        layout_default = json.dumps(self.EXPORT_LAYOUT_DEFAULT)
        extra_params_json = json.dumps(self.EXPORT_EXTRA_PARAMS)
        token_param = "null" if self.EXPORT_TOKEN_PARAM is None else json.dumps(self.EXPORT_TOKEN_PARAM)

        return f"""(async () => {{
  const STATES = {states_json};
  const MAX_RETRIES = 20;
  const FORCE_SPORT_ID = {force_sport};
  const HTTP_METHOD = {export_method};
  const SUPPORTS_STATE_FILTER = {supports_state_filter};
  const LAYOUT_DEFAULT = {layout_default};
  const EXTRA_PARAMS = {extra_params_json};
  const TOKEN_PARAM = {token_param};
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  {self._export_safety_helpers()}

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

  function findToken() {{
    try {{
      const tokenInput = document.querySelector('input[name="_token"]');
      if (tokenInput && tokenInput.value) return tokenInput.value;

      const tokenMeta = document.querySelector('meta[name="csrf-token"]');
      if (tokenMeta && tokenMeta.content) return tokenMeta.content;
    }} catch (e) {{
      console.log("Could not discover CSRF token from page:", e);
    }}
    return "";
  }}

  function shouldCaptureHiddenParam(name) {{
    if (!name) return false;
    const normalized = String(name).trim().toLowerCase();
    return (
      normalized === "_token" ||
      normalized === "token" ||
      normalized === "granted" ||
      normalized.endsWith("_token") ||
      normalized.endsWith("token") ||
      normalized.endsWith("granted")
    );
  }}

  function collectHiddenParams() {{
    const out = {{}};
    try {{
      const hiddenInputs = document.querySelectorAll('input[type="hidden"][name]');
      for (const input of hiddenInputs) {{
        const name = input.getAttribute('name') || "";
        const value = input.value || "";
        if (!value || !shouldCaptureHiddenParam(name) || out[name]) continue;
        out[name] = value;
      }}
    }} catch (e) {{
      console.log("Could not collect hidden form params from page:", e);
    }}
    return out;
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
    const hiddenParams = collectHiddenParams();
    for (const [key, value] of Object.entries(hiddenParams)) {{
      if (!params.has(key)) {{
        params.set(key, value);
      }}
    }}
    for (const [key, value] of Object.entries(EXTRA_PARAMS || {{}})) {{
      if (!params.has(key)) {{
        params.set(key, value);
      }}
    }}

    params.set("layout", params.get("layout") || LAYOUT_DEFAULT);
    params.set("past_events", params.get("past_events") || "false");
    params.set("events_exits", "");

    if (SUPPORTS_STATE_FILTER) {{
      params.set("state", state);
    }} else {{
      params.delete("state");
    }}

    if (FORCE_SPORT_ID !== null) {{
      params.set("sport_id", String(FORCE_SPORT_ID));
    }}

    if (TOKEN_PARAM) {{
      const token = findToken();
      if (token) {{
        params.set(TOKEN_PARAM, token);
      }}
    }}

    const allPages = [];
    let page = 1;
    let retries = 0;
    const seenPages = new Set();

    while (true) {{
      checkBudget(page);
      params.set("page", String(page));
      const url = `${{seed.ajaxUrl}}?${{params.toString()}}`;

      let data;
      try {{
        const fetchOptions = {{
          method: HTTP_METHOD,
          credentials: "include",
          headers: {{
            "x-requested-with": "XMLHttpRequest"
          }}
        }};

        let requestUrl = url;
        if (HTTP_METHOD === "POST") {{
          fetchOptions.headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8";
          fetchOptions.body = params.toString();
          requestUrl = seed.ajaxUrl;
        }}

        data = await fetchPage(requestUrl, fetchOptions);
      }} catch (e) {{
        console.log(`[${{state}}] Network error on page ${{page}}:`, e);
        retries++;
        if (retries > MAX_RETRIES) throw e;
        await sleep(3000);
        continue;
      }}

      retries = 0;
      const records = eventRecords(data);
      const eventCount = records.length;

      if (eventCount === 0) {{
        console.log(`[${{state}}] No more events at page ${{page}}. Stopping.`);
        break;
      }}
      const fingerprint = pageFingerprint(records);
      if (seenPages.has(fingerprint)) throw new Error("Repeated nonempty events page");
      seenPages.add(fingerprint);

      console.log(`[${{state}}] Saved page ${{page}} (${{eventCount}} events)`);

      allPages.push({{
        state: SUPPORTS_STATE_FILTER ? state : "",
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
    pages: [],
    completion: {{complete: false, completed_states: []}}
  }};

  const exportStates = SUPPORTS_STATE_FILTER ? STATES : [""];

  for (const state of exportStates) {{
    const stateLabel = state || "ALL";
    console.log(`Starting export for ${{stateLabel}}...`);
    const pages = await fetchState(state);
    dump.pages.push(...pages);
    dump.completion.completed_states.push(state);
    console.log(`Done with ${{stateLabel}}. Added ${{pages.length}} page(s).`);
    await sleep(1000);
  }}

  dump.completion.complete = true;
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

        log_progress(logger, "[%s] Browser export script generated: %s", self.SCRAPER_NAME, script_path)
        return script_path

    def _build_selenium_export_script(self) -> str:
        states_json = json.dumps([abbr for abbr, _ in ALL_50_STATES])
        force_sport = "null" if self.FORCE_SPORT_ID is None else str(self.FORCE_SPORT_ID)
        export_method = json.dumps(self.EXPORT_HTTP_METHOD.upper())
        supports_state_filter = "true" if self.EXPORT_SUPPORTS_STATE_FILTER else "false"
        layout_default = json.dumps(self.EXPORT_LAYOUT_DEFAULT)
        extra_params_json = json.dumps(self.EXPORT_EXTRA_PARAMS)
        token_param = "null" if self.EXPORT_TOKEN_PARAM is None else json.dumps(self.EXPORT_TOKEN_PARAM)

        return f"""
const callback = arguments[arguments.length - 1];
const STATES = {states_json};
const MAX_RETRIES = 10;
const FORCE_SPORT_ID = {force_sport};
const HTTP_METHOD = {export_method};
const SUPPORTS_STATE_FILTER = {supports_state_filter};
const LAYOUT_DEFAULT = {layout_default};
const EXTRA_PARAMS = {extra_params_json};
const TOKEN_PARAM = {token_param};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
{self._export_safety_helpers()}

function findAjaxSeed() {{
  try {{
    const entries = performance.getEntriesByType("resource")
      .map((e) => e.name)
      .filter((u) => u && u.includes("/ajax-events"));

    if (entries.length) {{
      const preferred =
        entries.find((u) => u.includes("season_ids=") && u.includes("page=1")) ||
        entries.find((u) => u.includes("page=1")) ||
        entries[entries.length - 1];

      const url = new URL(preferred);
      return {{
        ajaxUrl: `${{url.origin}}${{url.pathname}}`,
        params: Object.fromEntries(url.searchParams.entries())
      }};
    }}
  }} catch (e) {{}}

  return {{
    ajaxUrl: `${{location.origin}}/ajax-events`,
    params: Object.fromEntries(new URLSearchParams(location.search).entries())
  }};
}}

function findToken() {{
  try {{
    const tokenInput = document.querySelector('input[name="_token"]');
    if (tokenInput && tokenInput.value) return tokenInput.value;
    const tokenMeta = document.querySelector('meta[name="csrf-token"]');
    if (tokenMeta && tokenMeta.content) return tokenMeta.content;
  }} catch (e) {{}}
  return "";
}}

function shouldCaptureHiddenParam(name) {{
  if (!name) return false;
  const normalized = String(name).trim().toLowerCase();
  return (
    normalized === "_token" ||
    normalized === "token" ||
    normalized === "granted" ||
    normalized.endsWith("_token") ||
    normalized.endsWith("token") ||
    normalized.endsWith("granted")
  );
}}

function collectHiddenParams() {{
  const out = {{}};
  try {{
    const hiddenInputs = document.querySelectorAll('input[type="hidden"][name]');
    for (const input of hiddenInputs) {{
      const name = input.getAttribute('name') || "";
      const value = input.value || "";
      if (!value || !shouldCaptureHiddenParam(name) || out[name]) continue;
      out[name] = value;
    }}
  }} catch (e) {{}}
  return out;
}}

function countEvents(data) {{
  return eventRecords(data).length;
}}

(async () => {{
  try {{
    const seed = findAjaxSeed();
    const dump = {{
      generated_at: new Date().toISOString(),
      source: location.href,
      domain: location.origin,
      forced_sport_id: FORCE_SPORT_ID,
      states: STATES,
      seed_params: seed.params || {{}},
      pages: [],
      completion: {{complete: false, completed_states: []}}
    }};

    const exportStates = SUPPORTS_STATE_FILTER ? STATES : [""];

    for (const state of exportStates) {{
      const params = new URLSearchParams(seed.params || {{}});
      const hiddenParams = collectHiddenParams();
      for (const [key, value] of Object.entries(hiddenParams)) {{
        if (!params.has(key)) params.set(key, value);
      }}
      for (const [key, value] of Object.entries(EXTRA_PARAMS || {{}})) {{
        if (!params.has(key)) params.set(key, value);
      }}

      params.set("layout", params.get("layout") || LAYOUT_DEFAULT);
      params.set("past_events", params.get("past_events") || "false");
      params.set("events_exits", "");

      if (SUPPORTS_STATE_FILTER) params.set("state", state);
      else params.delete("state");

      if (FORCE_SPORT_ID !== null) params.set("sport_id", String(FORCE_SPORT_ID));

      if (TOKEN_PARAM) {{
        const token = findToken();
        if (token) params.set(TOKEN_PARAM, token);
      }}

      let page = 1;
      let retries = 0;
      const seenPages = new Set();
      while (true) {{
        checkBudget(page);
        params.set("page", String(page));
        const url = `${{seed.ajaxUrl}}?${{params.toString()}}`;

        try {{
          const fetchOptions = {{
            method: HTTP_METHOD,
            credentials: "include",
            headers: {{
              "x-requested-with": "XMLHttpRequest"
            }}
          }};

          let requestUrl = url;
          if (HTTP_METHOD === "POST") {{
            fetchOptions.headers["content-type"] = "application/x-www-form-urlencoded; charset=UTF-8";
            fetchOptions.body = params.toString();
            requestUrl = seed.ajaxUrl;
          }}

          const data = await fetchPage(requestUrl, fetchOptions);
          const eventCount = countEvents(data);
          if (!eventCount) break;
          const fingerprint = pageFingerprint(eventRecords(data));
          if (seenPages.has(fingerprint)) throw new Error("Repeated nonempty events page");
          seenPages.add(fingerprint);

          dump.pages.push({{
            state: SUPPORTS_STATE_FILTER ? state : "",
            page,
            url,
            fetchedAt: new Date().toISOString(),
            payload: data
          }});

          retries = 0;
          page += 1;
          await sleep(250);
        }} catch (err) {{
          retries += 1;
          if (retries > MAX_RETRIES) throw err;
          await sleep(1000);
        }}
      }}
      dump.completion.completed_states.push(state);
    }}

    dump.completion.complete = true;
    callback({{ ok: true, dump }});
  }} catch (err) {{
    callback({{ ok: false, error: String(err && err.message ? err.message : err) }});
  }}
}})();
"""

    def _try_export_dump_with_selenium(self, driver, dump_path: str) -> bool:
        if not self.SUPPORTS_SELENIUM_EXPORT or driver is None:
            return False

        try:
            driver.get(self.DOMAIN)
            time.sleep(3)
            result = driver.execute_async_script(self._build_selenium_export_script())
        except Exception as e:
            logger.warning("[%s] Selenium export attempt failed: %s", self.SCRAPER_NAME, e)
            return False

        if not isinstance(result, dict) or not result.get("ok") or "dump" not in result:
            logger.warning(
                "[%s] Selenium export did not return a usable dump%s",
                self.SCRAPER_NAME,
                f": {result.get('error')}" if isinstance(result, dict) and result.get("error") else ".",
            )
            return False

        try:
            self._validate_complete_dump(result["dump"])
        except ValueError as exc:
            logger.warning("[%s] Incomplete Selenium dump: %s", self.SCRAPER_NAME, exc)
            return False
        from common.storage import save_json_atomic
        save_json_atomic(result["dump"], dump_path)

        log_progress(logger, "[%s] Dump generated via Selenium: %s", self.SCRAPER_NAME, dump_path)
        return True

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

        self._validate_complete_dump(blob)
        self._index_event_source(blob)
        self._events_loaded = True
        self._loaded_dump_path = dump_path
        self._loaded_dump_mtime = mtime
        self._event_source_label = f"dump cache ({dump_path})"

        logger.info(
            "[%s] Loaded %s raw event object(s) from dump: %s",
            self.SCRAPER_NAME,
            len(self._all_events_cache),
            dump_path,
        )

    def _load_events_from_blob(self, blob: Any, source_label: str) -> None:
        self._index_event_source(blob)
        self._events_loaded = True
        self._loaded_dump_path = None
        self._loaded_dump_mtime = None
        self._event_source_label = source_label
        logger.info(
            "[%s] Loaded %s raw event object(s) from %s.",
            self.SCRAPER_NAME,
            len(self._all_events_cache),
            source_label,
        )

    @staticmethod
    def _event_records(payload):
        from common.collection import validated_records
        if isinstance(payload, dict):
            key = next((key for key in ("eventlist", "events", "data", "payload") if key in payload), None)
            if key is None:
                raise ValueError("Play365 response has no recognized events list")
            return Play365BaseScraper._event_records(payload[key])
        return validated_records(payload, source="Play365")

    def _validate_complete_dump(self, blob):
        if not isinstance(blob, dict):
            raise ValueError("Play365 dump must be an object")
        completion = blob.get("completion")
        if not isinstance(completion, dict) or completion.get("complete") is not True:
            raise ValueError("Play365 dump has no verified completion metadata; regenerate with the current exporter")
        expected = [abbr for abbr, _ in ALL_50_STATES] if self.EXPORT_SUPPORTS_STATE_FILTER else [""]
        completed = completion.get("completed_states")
        if not isinstance(completed, list) or any(not isinstance(s, str) for s in completed) or sorted(completed) != sorted(expected):
            raise ValueError("Play365 dump does not cover the expected export states")
        domain = blob.get("domain")
        if not isinstance(domain, str) or domain.rstrip("/") != self.DOMAIN.rstrip("/"):
            raise ValueError("Play365 dump belongs to a different provider domain")
        if not isinstance(blob.get("seed_params"), dict) or not isinstance(blob.get("pages"), list):
            raise ValueError("Play365 dump requires seed_params and pages")
        pages_by_state = {state: [] for state in expected}
        for page in blob["pages"]:
            if not isinstance(page, dict) or not isinstance(page.get("state"), str) or page["state"] not in pages_by_state:
                raise ValueError("Play365 dump has an unexpected page state")
            number = page.get("page")
            if type(number) is not int or not 1 <= number <= 200:
                raise ValueError("Play365 dump has an invalid page number")
            self._event_records(page.get("payload"))
            pages_by_state[page["state"]].append(number)
        for numbers in pages_by_state.values():
            if numbers != list(range(1, len(numbers) + 1)):
                raise ValueError("Play365 dump has missing, repeated, or unordered pages")

    def _index_event_source(self, blob: Any) -> None:
        self._all_events_cache = list(_walk_event_objects(blob))
        by_state = {}
        for event in self._all_events_cache:
            state = clean_text(_first_non_empty(event.get("state"))).upper()
            if not state:
                venues = event.get("venues") or []
                if venues:
                    state = clean_text(_first_non_empty(venues[0].get("state"))).upper()
            by_state.setdefault(state, []).append(event)
        self._events_by_state = by_state

    def _extract_hidden_inputs(self, html: str) -> Dict[str, str]:
        out: Dict[str, str] = {}
        if not html:
            return out

        for match in re.finditer(r'<input[^>]+type=["\']hidden["\'][^>]*>', html, flags=re.IGNORECASE):
            tag = match.group(0)
            name_match = re.search(r'name=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
            value_match = re.search(r'value=["\']([^"\']*)["\']', tag, flags=re.IGNORECASE)
            if not name_match:
                continue
            name = clean_text(name_match.group(1))
            value = clean_text(value_match.group(1) if value_match else "")
            if name and value:
                out[name] = value

        meta_match = re.search(
            r'<meta[^>]+name=["\']csrf-token["\'][^>]+content=["\']([^"\']+)["\']',
            html,
            flags=re.IGNORECASE,
        )
        if meta_match and "_token" not in out:
            out["_token"] = clean_text(meta_match.group(1))

        return out

    def _build_direct_api_params(self, *, page: int, state: str = "") -> Dict[str, str]:
        params = dict(self.EXPORT_EXTRA_PARAMS or {})
        params["page"] = str(page)
        params["layout"] = self.EXPORT_LAYOUT_DEFAULT or "medium"
        params["past_events"] = params.get("past_events", "false")
        params["events_exits"] = ""

        if self.EXPORT_SUPPORTS_STATE_FILTER and state:
            params["state"] = state
        else:
            params.pop("state", None)

        if self.FORCE_SPORT_ID is not None:
            params["sport_id"] = str(self.FORCE_SPORT_ID)

        return params

    def _try_load_events_from_api(self) -> bool:
        if not self.DIRECT_API_ENABLED:
            return False

        ajax_url = f"{self.DOMAIN.rstrip('/')}/ajax-events"
        session = requests.Session()
        pages = []
        export_states = [abbr for abbr, _ in ALL_50_STATES] if self.EXPORT_SUPPORTS_STATE_FILTER else [""]
        hidden_params: Dict[str, str] = {}
        made_request = False
        deadline = time.monotonic() + 600

        try:
            try:
                bootstrap = session.get(self.DOMAIN, headers=self.DIRECT_API_HEADERS, timeout=30)
                bootstrap.raise_for_status()
                hidden_params = self._extract_hidden_inputs(bootstrap.text)
            except Exception as e:
                logger.info("[%s] Bootstrap request for direct API seed did not succeed: %s", self.SCRAPER_NAME, e)

            # Public cached pages can contain another session's stale token.
            # Match the frontend's refresh step and retain its session cookies.
            refreshed = session.get(f"{self.DOMAIN.rstrip('/')}/refresh-csrf",
                                    headers=self.DIRECT_API_HEADERS, timeout=(10, 30))
            refreshed.raise_for_status()
            token = refreshed.json().get("csrf_token")
            if not isinstance(token, str) or not token.strip():
                raise ValueError("Play365 CSRF refresh returned no token")
            hidden_params["_token"] = token

            log_progress(logger, "[%s] Attempting direct API pull.", self.SCRAPER_NAME)

            for state in export_states:
                page = 1
                seen_pages = set()
                while True:
                    if page > 200 or time.monotonic() > deadline:
                        raise RuntimeError("Play365 source pagination/time budget exceeded")
                    params = self._build_direct_api_params(page=page, state=state)
                    for key, value in hidden_params.items():
                        if key not in params:
                            params[key] = value
                    if self.EXPORT_HTTP_METHOD.upper() == "POST":
                        if self.EXPORT_TOKEN_PARAM and self.EXPORT_TOKEN_PARAM not in params and hidden_params.get(self.EXPORT_TOKEN_PARAM):
                            params[self.EXPORT_TOKEN_PARAM] = hidden_params[self.EXPORT_TOKEN_PARAM]
                        resp = session.post(ajax_url, data=params, headers=self.DIRECT_API_HEADERS, timeout=30)
                    else:
                        resp = session.get(ajax_url, params=params, headers=self.DIRECT_API_HEADERS, timeout=30)
                    made_request = True
                    resp.raise_for_status()
                    data = resp.json()

                    eventlist = self._event_records(data)

                    if not eventlist:
                        break
                    fingerprint = hashlib.sha256(json.dumps(eventlist, sort_keys=True).encode()).hexdigest()
                    if fingerprint in seen_pages:
                        raise RuntimeError("Play365 API repeated a nonempty events page")
                    seen_pages.add(fingerprint)

                    pages.append(
                        {
                            "state": state if self.EXPORT_SUPPORTS_STATE_FILTER else "",
                            "page": page,
                            "url": resp.url,
                            "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "payload": data,
                        }
                    )
                    page += 1

            blob = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source": ajax_url,
                "domain": self.DOMAIN,
                "forced_sport_id": self.FORCE_SPORT_ID,
                "states": [abbr for abbr, _ in ALL_50_STATES],
                "seed_params": self._build_direct_api_params(page=1),
                "pages": pages,
            }
            self._load_events_from_blob(blob, "direct API")
            log_progress(
                logger,
                "[%s] Direct API pull succeeded%s.",
                self.SCRAPER_NAME,
                f" with {len(pages)} page(s)" if pages else " with no events returned",
            )
            return True
        except Exception as e:
            logger.warning("[%s] Direct API pull failed: %s", self.SCRAPER_NAME, e)
            return False
        finally:
            session.close()

    def _prompt_manual_fallback(self) -> bool:
        print(
            f"\nAutomatic pulling method failed for {self.SCRAPER_NAME}.\n"
            "Extract manually using a browser script?\n"
            "1) Yes\n"
            "2) No\n"
        )
        while True:
            choice = input("Enter choice [1-2]: ").strip()
            if choice == "1":
                return True
            if choice == "2":
                return False
            print("Invalid choice. Please enter 1 or 2.")

    def _ensure_dump_exists(
        self,
        dump_path: str,
        driver=None,
        manual_workspace_root: Optional[str] = None,
        *,
        prompt_for_manual_fallback: bool = True,
    ) -> str:
        if self._dump_is_fresh(dump_path):
            return dump_path

        if self._try_export_dump_with_selenium(driver, dump_path):
            return dump_path

        active_dump_path = dump_path
        if manual_workspace_root:
            os.makedirs(manual_workspace_root, exist_ok=True)
            self._write_manual_readme(manual_workspace_root)
            active_dump_path = self._manual_dump_path(manual_workspace_root)

        logger.warning("[%s] Automatic pulling failed. No dump is available at: %s", self.SCRAPER_NAME, active_dump_path)

        if prompt_for_manual_fallback and not self._prompt_manual_fallback():
            raise RuntimeError(f"{self.SCRAPER_NAME} manual extraction declined by user.")

        log_progress(logger, "[%s] Switching to manual browser-script fallback.", self.SCRAPER_NAME)

        script_path = self._write_export_script(active_dump_path)
        logger.warning("[%s] Browser export script generated at: %s", self.SCRAPER_NAME, script_path)

        try:
            while True:
                input(
                    f"\n{self.SCRAPER_NAME} manual extraction selected.\n"
                    f"Run the generated browser script from: {script_path}\n"
                    f"Place/save the resulting {self.DUMP_FILENAME} in: {os.path.dirname(active_dump_path)}\n"
                    "Press Enter after the dump file is ready..."
                )

                if self._dump_is_fresh(active_dump_path) or (
                    self._try_recover_downloaded_dump(active_dump_path) and self._dump_is_fresh(active_dump_path)
                ):
                    break

                print(
                    f"\n{self.DUMP_FILENAME} was not found yet.\n"
                    "1) Retry after placing the dump file\n"
                    "2) Cancel\n"
                )
                retry_choice = input("Enter choice [1-2]: ").strip()
                if retry_choice == "1":
                    continue
                raise FileNotFoundError(
                    f"{self.SCRAPER_NAME} dump still not found after prompt: {active_dump_path}"
                )
        except EOFError:
            pass

        if not self._dump_is_fresh(active_dump_path) and not (
            self._try_recover_downloaded_dump(active_dump_path) and self._dump_is_fresh(active_dump_path)
        ):
            raise FileNotFoundError(
                f"{self.SCRAPER_NAME} dump still not found after prompt: {active_dump_path}"
            )
        return active_dump_path

    def _dump_is_fresh(self, path):
        if not os.path.isfile(path):
            return False
        age_limit = getattr(self, "MAX_DUMP_AGE_SECONDS", 24 * 60 * 60)
        if time.time() - os.path.getmtime(path) > age_limit:
            logger.warning("[%s] Ignoring a manual dump older than %.1f hours", self.SCRAPER_NAME, age_limit / 3600)
            return False
        try:
            from datetime import datetime, timezone
            with open(path, encoding="utf-8") as handle:
                blob = json.load(handle)
            generated = blob.get("generated_at") if isinstance(blob, dict) else None
            if generated:
                stamp = datetime.fromisoformat(str(generated).replace("Z", "+00:00"))
                stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
                if (datetime.now(timezone.utc) - stamp).total_seconds() > age_limit:
                    return False
            return True
        except (ValueError, OSError, TypeError):
            return False

    # -------------------------------------------------------------------------
    # BaseScraper interface
    # -------------------------------------------------------------------------

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        dump_path, params = self._parse_base_url(base_url)
        target_state = clean_text(params.get("state")).upper()
        allow_manual_fallback = kwargs.get("allow_manual_fallback", True)
        manual_workspace_root = kwargs.get("manual_workspace_root")
        skip_direct_api = kwargs.get("skip_direct_api", False)
        prompt_for_manual_fallback = kwargs.get("prompt_for_manual_fallback", True)

        if not target_state:
            raise ValueError(f"{self.SCRAPER_NAME} base_url is missing ?state=XX")

        if not self._events_loaded:
            api_loaded = False
            if not skip_direct_api:
                api_loaded = self._try_load_events_from_api()
                if api_loaded:
                    self._events_loaded = True

            if not api_loaded:
                if self._dump_is_fresh(dump_path):
                    try:
                        self._load_dump_if_needed(dump_path)
                        api_loaded = True
                    except (ValueError, TypeError, OSError) as exc:
                        logger.warning("[%s] Fresh dump is unusable: %s", self.SCRAPER_NAME, type(exc).__name__)
            if not api_loaded:
                if not allow_manual_fallback:
                    raise Play365ManualFallbackRequired(self.SCRAPER_NAME)
                resolved_dump_path = self._ensure_dump_exists(
                    dump_path,
                    driver=driver,
                    manual_workspace_root=manual_workspace_root,
                    prompt_for_manual_fallback=prompt_for_manual_fallback,
                )
                self._load_dump_if_needed(resolved_dump_path)

        self._last_collection_source = self._event_source_label or "cache"
        self.events_by_url.clear()

        for event in self._events_by_state.get(target_state, []):
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
            "[%s] Returning %s event link(s) for state %s from %s.",
            self.SCRAPER_NAME,
            len(links),
            target_state,
            self._last_collection_source,
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


class Play365GetScraper(Play365BaseScraper):
    # Keep the import/class names (and ledger identities) while matching the
    # providers' current POST-only acquisition protocol.
    EXPORT_HTTP_METHOD = "POST"


class Play365GetAllScraper(Play365GetScraper):
    FORCE_SPORT_ID = None
    EXPORT_SUPPORTS_STATE_FILTER = False


class Play365PostScraper(Play365BaseScraper):
    FORCE_SPORT_ID = None
    EXPORT_HTTP_METHOD = "POST"
    EXPORT_SUPPORTS_STATE_FILTER = False
    EXPORT_TOKEN_PARAM = "_token"
