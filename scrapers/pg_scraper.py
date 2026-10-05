from datetime import datetime
import logging
import base64
import time
import re
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup, Tag
import requests
from requests.adapters import HTTPAdapter
from common.driver_manager import is_driver_connection_error
from urllib3.util.retry import Retry
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException
from us import states  # pip install us
from typing import List, Dict, Tuple
from common.config import OUTPUT_COLUMNS, VALID_LEVELS
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)
# --------------------------------------------------------------------
# Selenium/WebDriver resilience helpers
# --------------------------------------------------------------------
class WebDriverDisconnectedError(RuntimeError):
    """Raised when the Selenium session is no longer connected/valid."""

def _is_driver_disconnected_exception(exc: BaseException) -> bool:
    if is_driver_connection_error(exc):
        return True
    msg = str(exc) if exc is not None else ""
    msg_l = msg.lower()
    needles = [
        "tried to run command without establishing a connection",
        "invalid session id",
        "disconnected",
        "not reachable",
        "connection refused",
        "connection reset",
        "chrome not reachable",
        "unable to connect to renderer",
        "no such window",
        "target window already closed",
        "session deleted because of page crash",
        "tab crashed",
    ]
    return any(n in msg_l for n in needles)


# --------------------------------------------------------------------
# ADVANCED REGEX for best-effort address parsing
# --------------------------------------------------------------------
ADDRESS_REGEX = re.compile(
    r"""^
    (.*?\b(?:Rd|Road|Dr|Drive|St|Street|Lane|Ln|
            Blvd|Parkway|Pkwy|Highway|Hwy|Way|Cir|
            Circle|Ct|Court|Ave|Avenue)\b
            (?:\s+\S+){0,2}?)   # allow up to 2 tokens after the street type (like "90A")
    \s+
    (.*?),                     # city portion (until the comma)
    \s+([A-Z]{2})             # state abbr
    \s+(\d{5})                 # zip code
    $""",
    re.IGNORECASE | re.VERBOSE
)

def parse_address(address_str: str) -> Dict[str, str]:
    address_str = address_str.strip()
    match = ADDRESS_REGEX.match(address_str)
    if match:
        street_part = match.group(1).strip()
        city_part   = match.group(2).strip()
        state_abbr  = match.group(3).upper()
        zip_code    = match.group(4)
        st = states.lookup(state_abbr)
        state_full = st.name if st else state_abbr
        return {
            "Address": street_part,
            "City": city_part,
            "State-FullName": state_full,
            "Zip": zip_code
        }
    else:
        # Attempt a fallback: try to extract state and zip from the address
        fallback_match = re.search(r"([A-Z]{2})\s+(\d{5})", address_str)
        if fallback_match:
            state_abbr = fallback_match.group(1)
            zip_code = fallback_match.group(2)
            st = states.lookup(state_abbr)
            state_full = st.name if st else state_abbr
            return {
                "Address": address_str,
                "City": "",
                "State-FullName": state_full,
                "Zip": zip_code
            }
        else:
            # If nothing else, return the raw address with blanks for others.
            return {
                "Address": address_str,
                "City": "",
                "State-FullName": "",
                "Zip": ""
            }

def score_location(loc: dict) -> int:
    """
    Returns an integer score representing how 'complete' the address is.
    +1 if there's a zip
    +1 if there's a city
    +1 if there's a state
    +1 if the address field contains digits (suggesting a street number)
    """
    score = 0
    if loc.get("Zip"):
        score += 1
    if loc.get("City"):
        score += 1
    if loc.get("State-FullName"):
        score += 1
    # If the address field has digits, it likely has a street number
    if re.search(r"\d", loc.get("Address", "")):
        score += 1
    return score

def clean_tournament_name(original_name: str) -> str:
    """
    1) Remove a leading pattern like "2025 8U " if present.
    2) Remove any parenthetical text (e.g., "(OPEN)", "(MAJOR)", "(AA)") from the string.
    """
    # 1) Remove leading year & age group, e.g. "2025 8U Some Name" => "Some Name"
    pattern_year_age = re.compile(r"^(?:\d{4}\s+)?\d+U\s+(.*)$")
    match_year_age = pattern_year_age.match(original_name)
    if match_year_age:
        original_name = match_year_age.group(1).strip()

    # 2) Remove anything in parentheses (like "(OPEN)", "(MAJOR)", etc.)
    #    This will remove them wherever they appear in the name.
    original_name = re.sub(r"\s*\(.+?\)", "", original_name).strip()

    return original_name



def extract_competition_levels_from_parentheses(text: str) -> List[str]:
    """Extract competition-level tokens from parenthesized segments only.

    This avoids substring issues like 'A' matching inside 'AA' because we only
    accept whole tokens found inside parentheses, e.g. '(AA)' -> 'AA'.

    Tokens are returned in original order (deduped, case-normalized to upper).
    If VALID_LEVELS is provided, tokens are filtered by exact membership.
    """
    if not text:
        return []
    # Capture simple tokens inside parentheses, e.g. (AA), (OPEN), (MAJOR), (CP)
    raw_tokens = re.findall(r"\(([A-Za-z0-9+]+)\)", text)
    if not raw_tokens:
        return []

    valid = {lvl.upper() for lvl in VALID_LEVELS} if "VALID_LEVELS" in globals() else set()
    out: List[str] = []
    for tok in raw_tokens:
        t = tok.strip().upper()
        if not t:
            continue
        if valid and t not in valid:
            continue
        if t not in out:
            out.append(t)
    return out


class PGScraper(BaseScraper):
    SCRAPER_NAME = "PerfectGame"
    REFRESH_KNOWN_FUTURE_EVENTS = True
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"

    def _state_from_selected_location(self, selected_location: str) -> str:
        label = (selected_location or "").strip()
        if not label:
            return ""
        m = re.match(r"^(.*?)\s*\(", label)
        state_label = m.group(1).strip() if m else label
        state_obj = states.lookup(state_label)
        return state_obj.name if state_obj else state_label


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A small, resilient HTTP session for pages that don't require JS rendering.
        self._http = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.6,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)

    def collect_event_links(self, driver, base_url="https://www.perfectgame.org/Schedule/", wait_time=15, pause=3) -> List[str]:
        from common.collection import CollectedLinks
        complete, error = True, ""
        seen_pages = set()
        groups = {}
        skipped_groups = set()
        current_page = 1
        deadline = time.monotonic() + 600
        logger.info(f"Loading base URL: {base_url}")
        driver.get(base_url)
        # The provider now redirects some retired schedule listings to its new
        # search application. That application has no rgMasterTable; use its
        # fully paginated API with the original state/category filters instead.
        if isinstance(getattr(driver, "current_url", None), str) and urlparse(driver.current_url).hostname == "search.perfectgame.org":
            logger.info("Legacy schedule redirected to current public search; using future API discovery.")
            return self._collect_future_api_links(base_url)
        while True:
            if current_page > 200 or time.monotonic() > deadline:
                complete, error = False, "PerfectGame page/time budget exceeded"
                break
            logger.info(f"Processing page {current_page}")
            try:
                # Wait for the events table to be present
                WebDriverWait(driver, wait_time).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table.rgMasterTable"))
                )
            except TimeoutException:
                if (parse_qs(urlparse(base_url).query).get("st")
                        and isinstance(getattr(driver, "current_url", None), str)
                        and urlparse(driver.current_url).hostname == "search.perfectgame.org"):
                    logger.info("Legacy schedule redirected while waiting; finishing discovery through the future API.")
                    break
                logger.info("Timeout waiting for events table.")
                complete, error = False, "PerfectGame listing timeout"
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            group_headers = soup.find_all("tr", class_="rgGroupHeader")
            logger.info(f"Found {len(group_headers)} group headers on page {current_page}")
            if not group_headers:
                text = soup.get_text(" ", strip=True).lower()
                if not any(marker in text for marker in ("no events found", "no records to display")):
                    complete, error = False, "PerfectGame has no group headers or explicit empty state"
                break

            fingerprint = tuple(sorted(a["href"] for header in group_headers for a in header.find_all("a", href=True)))
            if not fingerprint or fingerprint in seen_pages:
                complete, error = False, "PerfectGame missing group links or repeated listing identities"
                break
            seen_pages.add(fingerprint)

            for gh in group_headers:
                a_tag = gh.find("a", href=True)
                if not a_tag:
                    continue
                group_url = urljoin(base_url, a_tag["href"].rstrip('.'))
                parsed = urlparse(group_url)
                qs = parse_qs(parsed.query)
                gid = qs.get("gid", [None])[0]
                if gid and gid not in groups and gid not in skipped_groups:
                    if self._group_listing_is_non_future(gh):
                        skipped_groups.add(gid)
                        continue
                    groups[gid] = group_url
                    logger.info(f"Found grouped event link: {group_url}")

            try:
                next_buttons = driver.find_elements(By.XPATH, "//button[@title='Next Page']")
                if not next_buttons:
                    logger.info("Next page button not present. Ending pagination.")
                    break

                next_button = next_buttons[0]
                btn_class = next_button.get_attribute("class")
                btn_onclick = next_button.get_attribute("onclick")
                if ("disabled" in btn_class) or (btn_onclick and btn_onclick.strip() == "return false;"):
                    logger.info("Next page button is disabled. Ending pagination.")
                    break

                # Store the current table element to ensure we wait for a page change
                prev_table = driver.find_element(By.CSS_SELECTOR, "table.rgMasterTable")
                try:
                    driver.execute_script("arguments[0].click();", next_button)
                except StaleElementReferenceException:
                    logger.warning("Stale element reference when clicking next button. Retrying click.")
                    next_button = driver.find_element(By.XPATH, "//button[@title='Next Page']")
                    driver.execute_script("arguments[0].click();", next_button)

                # Wait until the previous table becomes stale, confirming a new page is loaded
                WebDriverWait(driver, wait_time + 5).until(EC.staleness_of(prev_table))
                time.sleep(pause)  # Extra pause to allow the page to load fully
                current_page += 1
            except Exception as e:
                if is_driver_connection_error(e):
                    raise
                if (isinstance(e, TimeoutException) and parse_qs(urlparse(base_url).query).get("st")
                        and isinstance(getattr(driver, "current_url", None), str)
                        and urlparse(driver.current_url).hostname == "search.perfectgame.org"):
                    logger.info("Legacy pagination redirected to current search; retaining collected groups and completing through the future API.")
                    break
                logger.error(f"Error during pagination: {e}")
                complete, error = False, str(e)
                break

        logger.info(f"Collected {len(groups)} unique group entries across {current_page} pages.")
        links = CollectedLinks(groups.values(), complete=complete, error=error, pages=current_page,
                               non_future_skipped=len(skipped_groups))
        # Keep legacy discovery, including retired categories, while adding the
        # current public search's open-ended future coverage for active categories.
        if parse_qs(urlparse(base_url).query).get("st"):
            additional = self._collect_future_api_links(base_url)
            links.extend(url for url in additional if url not in links)
            links.pages += additional.pages
            if not additional.complete:
                links.complete = False
                links.error = "; ".join(filter(None, (links.error, additional.error)))
        return links

    @staticmethod
    def _group_listing_is_non_future(header):
        starts = []
        for row in header.next_siblings:
            if not isinstance(row, Tag):
                continue
            if 'rgGroupHeader' in row.get('class', []):
                break
            if not row.find('a', href=re.compile(r'/Events/Default\.aspx\?event=', re.I)):
                continue
            values = row.find_all('input', id=re.compile(r'hfStartDate$'))
            if not values:
                return False
            for value in values:
                try:
                    starts.append(datetime.strptime(value.get('value', '').split()[0], '%m/%d/%Y').date())
                except (ValueError, IndexError):
                    return False
        return bool(starts) and all(start <= datetime.now().date() for start in starts)

    def _collect_future_api_links(self, base_url):
        from common.collection import CollectedLinks, validated_records
        from datetime import timedelta
        result = CollectedLinks()
        try:
            query = parse_qs(urlparse(base_url).query)
            decode = lambda key: base64.b64decode(query[key][0], validate=True).decode("ascii")
            state = decode("st")
            if not states.lookup(state):
                raise ValueError("PerfectGame saved state filter is invalid")
            # Confirmed from matching live API events and legacy child type IDs.
            active_types = {3: "PGBA Affiliated", 6: "WWBA Tournament", 7: "BCS Tournament",
                            12: "Super25", 14: "PG Youth Tournament"}
            selected = [active_types[value] for value in map(int, decode("ty").split(",")) if value in active_types]
            if not selected:
                raise ValueError("PerfectGame saved filter has no supported active tournament categories")
            params = {"dateFrom": (datetime.now().date() + timedelta(days=1)).isoformat(),
                      "sort": "dateAsc", "states[0]": state, "sportType": "Baseball", "limit": 100}
            params.update({f"eventTypes[{index}]": value for index, value in enumerate(selected)})
            seen, seen_group_ids, page, passes = set(), set(), 1, 1
            deadline = time.monotonic() + 600
            source_total, processed_records = None, 0
            while True:
                if page > 200 or result.pages >= 200 or time.monotonic() > deadline:
                    raise ValueError("PerfectGame public API page/time budget exceeded")
                response = self._http.get("https://tpa2.perfectgame.org/api/search/events",
                                          params={**params, "page": page}, timeout=(10, 30))
                response.raise_for_status()
                payload = response.json()
                records = validated_records(payload, field="data", source="PerfectGame public search")
                result.pages += 1
                total, total_pages = payload.get("totalItems"), payload.get("totalPages")
                if type(total) is not int or total < 0 or type(total_pages) is not int or total_pages < 0:
                    raise ValueError("PerfectGame public API returned invalid totals")
                if source_total is not None and source_total != total:
                    raise ValueError("PerfectGame public API changed its total during pagination")
                source_total = total
                if type(payload.get("currentPage")) is not int or payload["currentPage"] != page:
                    raise ValueError("PerfectGame public API returned the wrong page")
                following = payload.get("nextPage")
                if following is not None and (type(following) is not int or following != page + 1):
                    raise ValueError("PerfectGame public API returned invalid pagination")
                ids = tuple(record.get("eventGroupId") for record in records)
                if any(type(value) is not int or value <= 0 for value in ids):
                    raise ValueError("PerfectGame public API returned a missing group identity")
                fingerprint = tuple(sorted(ids))
                if len(set(ids)) != len(ids) or (ids and fingerprint in seen):
                    raise ValueError("PerfectGame public API repeated event groups")
                if not records and following is not None:
                    raise ValueError("PerfectGame public API returned an empty intermediate page")
                seen.add(fingerprint)
                seen_group_ids.update(ids)
                if len(seen_group_ids) > source_total:
                    raise ValueError("PerfectGame public API changed group identities during pagination")
                processed_records += len(records)
                for gid in ids:
                    url = f"https://www.perfectgame.org/Schedule/GroupedEvents.aspx?gid={gid}"
                    if url not in result:
                        result.append(url)
                if following is None:
                    if processed_records != source_total or page != max(1, total_pages):
                        raise ValueError("PerfectGame public API ended before its advertised coverage")
                    if len(seen_group_ids) != source_total:
                        if passes >= 3:
                            raise ValueError("PerfectGame public API omitted groups after bounded pagination reconciliation")
                        logger.warning("PerfectGame pagination overlapped; reconciling distinct group coverage (%d/%d).", len(seen_group_ids), source_total)
                        passes += 1
                        page, processed_records = 1, 0
                        seen.clear()
                        continue
                    break
                page = following
        except Exception as exc:
            result.complete = False
            result.error = f"PerfectGame open-ended discovery failed: {exc}"
            logger.error("%s", result.error)
        return result

    def get_event_key(self, url: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        gid = qs.get("gid", [None])[0]
        if gid:
            gid = gid.rstrip('.')
        return gid if gid else url


    def _get_soup_requests(self, url: str, timeout=(15, 45)) -> BeautifulSoup | None:
        """
        Fetch HTML via requests for pages that don't require JS rendering.
        Returns BeautifulSoup on success; None on network/HTTP failure.

        Notes:
        - We keep this conservative: return None on hard blocks (403/429), so the caller
          can decide whether to backoff/retry.
        """
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
            resp = self._http.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            status = resp.status_code
            if status in (403, 429):
                logger.warning(f"[requests] Blocked (HTTP {status}) for URL: {url}")
                return None
            if status >= 400:
                logger.warning(f"[requests] HTTP {status} for URL: {url}")
                return None
            html = resp.text or ""
            if not html.strip():
                return None
            return BeautifulSoup(html, "html.parser")
        except Exception as e:
            logger.warning(f"[requests] Failed to fetch URL: {url} ({e})")
            return None


    def _get_soup(self, driver, url: str, wait_time=15) -> BeautifulSoup:
        """
        Returns a parsed BeautifulSoup for the URL.

        Strategy:
        - For grouped pages: prefer requests for server-rendered data. On failure,
          try the existing browser once, with bounded navigation timeouts.
        - For other pages: use Selenium, but detect session-death and raise a dedicated error
          so the outer runner can restart the driver and retry the current URL.
        """
        url = url.rstrip("/.")
        self._last_soup_used_browser = False
        is_grouped = ("groupedevents.aspx" in url.lower())

        if is_grouped:
            soup = self._get_soup_requests(url)
            if soup is not None:
                return soup
            logger.info("Grouped HTTP fetch failed; trying bounded browser navigation: %s", url)

        try:
            if driver is None:
                raise WebDriverDisconnectedError("Selenium driver is None.")

            if hasattr(driver, "set_page_load_timeout"):
                driver.set_page_load_timeout(55)
            if hasattr(driver, "set_script_timeout"):
                driver.set_script_timeout(30)

            driver.get(url)
            self._last_soup_used_browser = True
            WebDriverWait(driver, wait_time).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            return BeautifulSoup(driver.page_source, "html.parser")

        except (WebDriverException, TimeoutException) as e:
            if _is_driver_disconnected_exception(e):
                raise WebDriverDisconnectedError(str(e)) from e

            # Try to salvage whatever loaded
            try:
                self._last_soup_used_browser = True
                driver.execute_script("window.stop();")
                html = driver.page_source
                if html:
                    return BeautifulSoup(html, "html.parser")
            except Exception:
                pass

            # Last resort: requests (some pages are still server rendered)
            soup = self._get_soup_requests(url)
            if soup is not None:
                return soup
            raise

        except Exception as e:
            if _is_driver_disconnected_exception(e):
                raise WebDriverDisconnectedError(str(e)) from e
            raise

    def _format_phone(self, phone: str) -> str:
        phone = phone.strip()
        digits = re.sub(r"\D", "", phone)
        if len(digits) == 10:
            return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return phone

    def _parse_date_range(self, date_str: str, end_str: str) -> Tuple[str, str]:
        logger.debug(f"Parsing date range from '{date_str}' and '{end_str}'")
        from common.date_utils import explicit_year_range
        explicit = explicit_year_range(date_str)
        if explicit:
            return tuple(value.strftime("%m/%d/%Y") for value in explicit)
        year_match = re.search(r"(\d{4})", end_str)
        year = year_match.group(1) if year_match else str(datetime.now().year)
        date_str = date_str.replace("\u2013", "-").replace("\u2014", "-")
        trailing_year = re.search(r",?\s+(\d{4})$", date_str)
        if trailing_year:
            year = trailing_year.group(1)
            date_str = date_str[:trailing_year.start()].strip()
        if "-" in date_str:
            parts = date_str.split("-")
            start_part = parts[0].strip()
            end_part = parts[1].strip()
            if re.match(r"^\d", end_part):
                m = re.match(r"([A-Za-z]+)", start_part)
                month = m.group(1) if m else ""
                end_part = f"{month} {end_part}"
            start_date = f"{start_part} {year}"
            end_date = f"{end_part} {year}"
            # The hidden endpoint date anchors the end year for winter leagues.
            # Only use rollover when the source supplies a complete numeric date.
            endpoint = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})(?:\s|$)", end_str)
            if endpoint:
                end_anchor = datetime.strptime("/".join(endpoint.groups()), "%m/%d/%Y")
                parsed_start = datetime.strptime(start_date, "%b %d %Y")
                parsed_end = datetime.strptime(end_date, "%b %d %Y")
                if (parsed_end.month, parsed_end.day) != (end_anchor.month, end_anchor.day):
                    raise ValueError("PerfectGame displayed end date conflicts with hidden endpoint date")
                start_year = end_anchor.year - int((parsed_start.month, parsed_start.day) > (parsed_end.month, parsed_end.day))
                start_date = f"{start_part} {start_year}"
                end_date = f"{end_part} {end_anchor.year}"
            logger.debug(f"Parsed start_date: {start_date}, end_date: {end_date}")
            return (start_date, end_date)
        else:
            date_full = f"{date_str} {year}"
            logger.debug(f"Single date provided; using: {date_full}")
            return (date_full, date_full)

    def _parse_group_listing_text(self, text: str) -> Dict[str, str] | None:
        """Parse the visible anchor text used on newer grouped schedule pages.

        Example:
            'Feb 28 - Mar 1 2026 7U PG CLASH OF THE CHAMPIONS (AA) Phenix City, AL Phenix City Baseball Complex 7U (AA) (CP)'
        """
        text = (text or "").strip()
        if not text:
            return None

        # Date range at the beginning
        dm = re.match(
            r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})\s*-\s*(?:(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+)?(\d{1,2})\s+(\d{4})\s+(.+)$",
            text,
        )
        if not dm:
            return None

        m1, d1, m2, d2, year, rest = dm.groups()
        m2 = m2 or m1

        start_date = f"{m1} {d1} {year}"
        end_date = f"{m2} {d2} {year}"
        # Find 'City, ST' in the remainder.
        # NOTE: We intentionally avoid matching from the start of the tournament title (which can include "City, ST")
        # by requiring city words to look like normal Title Case words (e.g., "Phenix City") rather than ALL CAPS.
        city_pat = r"[A-Z][a-z][A-Za-z\.'-]*(?:\s+[A-Z][a-z][A-Za-z\.'-]*){0,3}"
        cs = re.search(rf"\s+({city_pat}),\s*([A-Z]{{2}})\s+", rest)
        if cs:
            name_part = rest[: cs.start()].strip()
            city = cs.group(1).strip()
            state_abbr = cs.group(2).strip()
            after_cs = rest[cs.end() :].strip()
        else:
            name_part = rest.strip()
            city = ""
            state_abbr = ""
            after_cs = ""
        # Venue tends to be after city/state, and often ends with repeated age/level tokens
        venue = after_cs
        if venue:
            venue = re.sub(r"\s+\d+U\b.*$", "", venue).strip()
            venue = re.sub(r"\s*(\(.+?\)\s*)+$", "", venue).strip()

        # Age group appears in the name
        age_group = ""
        am = re.search(r"(\d+U)", name_part)
        if am:
            age_group = am.group(1)

        # Competition levels: parse ONLY parenthesized tokens (exact-token matching)
        comp_levels = extract_competition_levels_from_parentheses(text)
        comp_level = ",".join(comp_levels)

        st = states.lookup(state_abbr) if state_abbr else None
        state_full = st.name if st else state_abbr

        return {
            "tournament_name": clean_tournament_name(name_part),
            "start_date": start_date,
            "end_date": end_date,
            "age_group": age_group,
            "competition_level": comp_level,
            "city": city,
            "state": state_full,
            "ballpark": venue,
        }

    def _parse_grouped_page(self, driver, soup: BeautifulSoup) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []

        # ------------------------------
        # NEW (2026+) grouped layout: links to child events
# NEW (2026+) grouped layout: cards linking to child events
        # ------------------------------
        # Example markup (simplified):
        # <a ... href="../Events/Default.aspx?event=147967">
        #   <div class="pgds-EventCard ...">
        #     <span id="..._lblEventDate_0">Feb 28 - Mar 1</span>
        #     <span id="..._lblEventName_0">2026 7U ... (AA)</span>
        #     <span id="..._lblCityState_0">Phenix City, AL</span>
        #     <span id="..._lblBallPark_0">...</span>
        #     <span id="..._lblDivision_0">7U (AA) (CP)</span>
        #   </div>
        # </a>

        card_anchors = soup.select("a[href*='/Events/Default.aspx?event='] div.pgds-EventCard")
        if card_anchors:
            seen_event_ids = set()
            for card in card_anchors:
                if not isinstance(card, Tag):
                    continue
                a = card.find_parent("a")
                if not a or not isinstance(a, Tag):
                    continue
                href = a.get("href", "")
                if not href:
                    continue
                event_url = urljoin("https://www.perfectgame.org", href)
                qs = parse_qs(urlparse(event_url).query)
                event_id = qs.get("event", [""])[0]
                if not event_id or event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)

                # Extract fields directly from the card spans (more reliable than plain text parsing)
                date_el = card.find("span", id=re.compile(r"repSchedule_lblEventDate_\d+"))
                name_el = card.find("span", id=re.compile(r"repSchedule_lblEventName_\d+"))
                city_state_el = card.find("span", id=re.compile(r"repSchedule_lblCityState_\d+"))
                ballpark_el = card.find("span", id=re.compile(r"repSchedule_lblBallPark_\d+"))
                division_el = card.find("span", id=re.compile(r"repSchedule_lblDivision_\d+"))

                date_str = date_el.get_text(" ", strip=True) if date_el else ""
                raw_name = name_el.get_text(" ", strip=True) if name_el else ""
                city_state = city_state_el.get_text(" ", strip=True) if city_state_el else ""
                ballpark_name = ballpark_el.get_text(" ", strip=True) if ballpark_el else ""
                division_text = division_el.get_text(" ", strip=True) if division_el else ""

                # Infer year from the event name (often starts with "2026 ...")
                year = ""
                ym = re.search(r"\b(20\d{2})\b", raw_name)
                if ym:
                    year = ym.group(1)
                else:
                    year = str(datetime.now().year)

                # Dates: the card usually omits the year, so pass the inferred year as end_str
                end_input = card.find("input", id=re.compile(r"repSchedule_hlEndDate_\d+"))
                if end_input is None and date_el is not None:
                    # Current cards keep hidden dates beside the card, sharing
                    # its repeater index rather than nesting them inside it.
                    endpoint_id = date_el.get("id", "").replace("lblEventDate_", "hlEndDate_")
                    end_input = soup.find("input", id=endpoint_id)
                endpoint = end_input.get("value", "") if end_input else ""
                start_date, end_date = self._parse_date_range(date_str, endpoint or year)

                tournament_name = clean_tournament_name(raw_name) if raw_name else ""

                # Age group + competition levels come from the division line (e.g. "7U (AA) (CP)")
                age_group = ""
                comp_levels: List[str] = []
                dm_age = re.match(r"(\d+U)", division_text)
                if dm_age:
                    age_group = dm_age.group(1)

                # Collect competition levels from parenthetical tokens only (exact-token matching)
                comp_levels = extract_competition_levels_from_parentheses(division_text)

                comp_level_joined = ",".join(comp_levels)

                # City/state normalization
                city, state_val = "", ""
                if city_state:
                    parts = [p.strip() for p in city_state.split(",")]
                    if len(parts) >= 2:
                        city = parts[0]
                        state_abbr = parts[1]
                        st = states.lookup(state_abbr)
                        state_val = st.name if st else state_abbr

                results.append({
                    "tournament_name": tournament_name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "age_group": age_group,
                    "competition_level": comp_level_joined,
                    "event_id": event_id,
                    "city": city,
                    "state": state_val,
                    "ballpark": ballpark_name,
                })

            logger.info(f"Parsed {len(results)} child events from grouped page (card layout).")
            return results

        # Fallback for any future variations: attempt to parse from link text
        link_tags = soup.select("a[href*='/Events/Default.aspx?event=']")
        candidate_links = []
        for a in link_tags:
            if not isinstance(a, Tag):
                continue
            href = a.get("href", "")
            txt = a.get_text(" ", strip=True)
            if not href or not txt:
                continue
            if not re.match(r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}", txt):
                continue
            candidate_links.append((href, txt))

        if candidate_links:
            seen_event_ids = set()
            for href, txt in candidate_links:
                event_url = urljoin("https://www.perfectgame.org", href)
                qs = parse_qs(urlparse(event_url).query)
                event_id = qs.get("event", [""])[0]
                if not event_id or event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)

                parsed = self._parse_group_listing_text(txt)
                if not parsed:
                    continue
                parsed["event_id"] = event_id
                results.append(parsed)

            logger.info(f"Parsed {len(results)} child events from grouped page (link text layout).")
            return results


        # ------------------------------
        # LEGACY grouped layout (hidden inputs + EventBox)
        # ------------------------------
        hidden_date_inputs = soup.find_all("input", id=re.compile(r"hlEventDate_\d+"))
        logger.info(f"Found {len(hidden_date_inputs)} hidden date inputs on grouped page.")
        if not hidden_date_inputs:
            return results

        for date_input in hidden_date_inputs:
            try:
                if not isinstance(date_input, Tag):
                    continue
                id_attr = date_input.get("id", "")
                m = re.search(r"hlEventDate_(\d+)$", id_attr)
                if not m:
                    continue
                idx = m.group(1)
                end_input = soup.find("input", id=f"ContentTopLevel_ContentPlaceHolder1_repSchedule_hlEndDate_{idx}")
                eventid_input = soup.find("input", id=f"ContentTopLevel_ContentPlaceHolder1_repSchedule_hlEventID_{idx}")
                if not eventid_input:
                    continue
                date_str = date_input.get("value", "").strip()
                end_str = end_input.get("value", "").strip() if end_input else ""
                event_id = eventid_input.get("value", "").strip()
                start_date, end_date = self._parse_date_range(date_str, end_str)

                box = soup.select_one(
                    f'div.EventBox[data-eventid="{event_id}"], div.EndedEventBox[data-eventid="{event_id}"]'
                )
                if not box:
                    continue

                name_element = box.find("span", id=re.compile(r"lblEventName_" + re.escape(idx)))
                raw_tournament_name = name_element.get_text(strip=True) if name_element else ""
                tournament_name = clean_tournament_name(raw_tournament_name)

                division_element = box.find("span", id=re.compile(r"lblDivision_" + re.escape(idx)))
                division_text = division_element.get_text(strip=True) if division_element else ""
                age_group, comp_level = "", ""
                dm_div = re.match(r"(\d+U)(?:\s*\((.+?)\))?", division_text)
                if dm_div:
                    age_group = dm_div.group(1)
                    comp_level = dm_div.group(2) if dm_div.group(2) else ""

                city_state_element = box.find("span", id=re.compile(r"lblCityState_" + re.escape(idx)))
                city_state = city_state_element.get_text(strip=True) if city_state_element else ""
                city, state_val = "", ""
                if city_state:
                    parts = [p.strip() for p in city_state.split(",")]
                    if len(parts) >= 2:
                        city = parts[0]
                        state_abbr = parts[1]
                        state_val = states.lookup(state_abbr).name if states.lookup(state_abbr) else state_abbr

                ballpark_element = box.find("span", id=re.compile(r"lblBallPark_" + re.escape(idx)))
                ballpark_name = ballpark_element.get_text(strip=True) if ballpark_element else ""

                event_data = {
                    "tournament_name": tournament_name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "age_group": age_group,
                    "competition_level": comp_level,
                    "event_id": event_id,
                    "city": city,
                    "state": state_val,
                    "ballpark": ballpark_name,
                }
                results.append(event_data)
            except Exception as ex:
                logger.debug(f"Exception processing hidden date input {id_attr}: {ex}")
        logger.debug(f"Final parsed event data: {results}")
        return results


        for date_input in hidden_date_inputs:
            try:
                if not isinstance(date_input, Tag):
                    continue
                id_attr = date_input.get("id", "")
                m = re.search(r"hlEventDate_(\d+)$", id_attr)
                if not m:
                    continue
                idx = m.group(1)
                end_input = soup.find("input", id=f"ContentTopLevel_ContentPlaceHolder1_repSchedule_hlEndDate_{idx}")
                eventid_input = soup.find("input", id=f"ContentTopLevel_ContentPlaceHolder1_repSchedule_hlEventID_{idx}")
                if not eventid_input:
                    continue
                date_str = date_input.get("value", "").strip()
                end_str = end_input.get("value", "").strip() if end_input else ""
                event_id = eventid_input.get("value", "").strip()
                start_date, end_date = self._parse_date_range(date_str, end_str)

                box = soup.select_one(
                    f'div.EventBox[data-eventid="{event_id}"], div.EndedEventBox[data-eventid="{event_id}"]'
                )
                if not box:
                    continue

                name_element = box.find("span", id=re.compile(r"lblEventName_" + re.escape(idx)))
                raw_tournament_name = name_element.get_text(strip=True) if name_element else ""
                tournament_name = clean_tournament_name(raw_tournament_name)

                division_element = box.find("span", id=re.compile(r"lblDivision_" + re.escape(idx)))
                division_text = division_element.get_text(strip=True) if division_element else ""
                age_group, comp_level = "", ""
                # Updated regex: capture age group even if competition level is missing
                dm_div = re.match(r"(\d+U)(?:\s*\((.+?)\))?", division_text)
                if dm_div:
                    age_group = dm_div.group(1)
                    comp_level = dm_div.group(2) if dm_div.group(2) else ""

                city_state_element = box.find("span", id=re.compile(r"lblCityState_" + re.escape(idx)))
                city_state = city_state_element.get_text(strip=True) if city_state_element else ""
                city, state_val = "", ""
                if city_state:
                    parts = [p.strip() for p in city_state.split(",")]
                    if len(parts) >= 2:
                        city = parts[0]
                        state_abbr = parts[1]
                        state_val = states.lookup(state_abbr).name if states.lookup(state_abbr) else state_abbr

                ballpark_element = box.find("span", id=re.compile(r"lblBallPark_" + re.escape(idx)))
                ballpark_name = ballpark_element.get_text(strip=True) if ballpark_element else ""

                event_data = {
                    "tournament_name": tournament_name,
                    "start_date": start_date,
                    "end_date": end_date,
                    "age_group": age_group,
                    "competition_level": comp_level,
                    "event_id": event_id,
                    "city": city,
                    "state": state_val,
                    "ballpark": ballpark_name,
                }
                results.append(event_data)
            except Exception as ex:
                logger.debug(f"Exception processing hidden date input {id_attr}: {ex}")
        logger.debug(f"Final parsed event data: {results}")
        return results

    def _parse_contact_info(self, driver, event_id: str, wait_time=15) -> Dict[str, str]:
        detail_url = f"https://www.perfectgame.org/Events/Default.aspx?event={event_id}"
        logger.debug(f"Loading detail page for contact info: {detail_url}")
        try:
            detail_soup = self._get_soup(driver, detail_url, wait_time)
        except Exception as e:
            if is_driver_connection_error(e):
                raise
            logger.warning("Contact detail navigation failed; checking its public HTTP source: %s", detail_url)
            detail_soup = None
        recognized = self._is_event_detail_soup(detail_soup)
        contact = self._parse_contact_soup(detail_soup) if recognized else {
            "contact_name": "", "email": "", "phone": "", "guaranteed_games": ""
        }
        if not recognized or not any(contact[key] for key in ("contact_name", "email", "phone")):
            fallback = self._get_soup_requests(detail_url, timeout=(10, 30))
            if self._is_event_detail_soup(fallback):
                recovered = self._parse_contact_soup(fallback)
                contact = {key: contact[key] or recovered[key] for key in contact}
                logger.info("Contact details verified against public HTTP source: %s", detail_url)
                return contact
            if not recognized:
                if driver is not None:
                    # An isolated navigation timeout can leave an incomplete
                    # body while HTTP is blocked. Retry this page once, keeping
                    # the same verification guard and browser time limits.
                    try:
                        retry_soup = self._get_soup(driver, detail_url, wait_time)
                    except Exception as exc:
                        if is_driver_connection_error(exc):
                            raise
                        retry_soup = None
                    if self._is_event_detail_soup(retry_soup):
                        logger.info("Contact detail verified after bounded browser retry: %s", detail_url)
                        return self._parse_contact_soup(retry_soup)
                raise ValueError(f"Unable to verify PerfectGame contact detail page for event {event_id}")
        return contact

    @staticmethod
    def _is_event_detail_soup(soup):
        if soup is None:
            return False
        title = soup.find(id=re.compile(r"EventHeader.*lblEventName|lblEventNameNew$", re.I))
        return bool(title and title.get_text(" ", strip=True))

    def _parse_contact_soup(self, detail_soup) -> Dict[str, str]:
        contact_div = detail_soup.find("div", id=re.compile(r"pnl(ContactPG|TournyDirector)"))
        
        if contact_div:
            name_elements = contact_div.find_all("span", id=re.compile(r"lblDirectorName"))
            email_elements = contact_div.find_all("a", href=re.compile(r"^mailto:"))
            phone_elements = contact_div.find_all("span", id=re.compile(r"lblDirectorPhone"))
        else:
            name_elements = detail_soup.find_all("span", id=re.compile(r"lblDirectorName"))
            email_elements = detail_soup.find_all("a", href=re.compile(r"^mailto:"))
            phone_elements = detail_soup.find_all("span", id=re.compile(r"lblDirectorPhone"))
        
        contact_name = name_elements[0].get_text(strip=True) if name_elements and isinstance(name_elements[0], Tag) else ""
        email = email_elements[0].get_text(strip=True) if email_elements and isinstance(email_elements[0], Tag) else ""
        phone = self._format_phone(phone_elements[0].get_text(strip=True)) if phone_elements and isinstance(phone_elements[0], Tag) else ""
        
        # Extract guaranteed games information
        gg_elem = detail_soup.find("span", id="ContentTopLevel_ContentPlaceHolder1_lblGamesGuaranteed")
        if gg_elem:
            gg_text = gg_elem.get_text(" ", strip=True)
            m = re.search(r"(\d+)", gg_text)
            gg = m.group(1) if m else ""
        else:
            gg = ""
        
        return {"contact_name": contact_name, "email": email, "phone": phone, "guaranteed_games": gg}

    def _parse_locations_page(self, soup: BeautifulSoup) -> Dict[str, any]:
        results = []
        address_spans = soup.find_all("span", id=re.compile(r"rptLocations_lblBallParkAddresses_\d+"))
        for addr_span in address_spans:
            raw_address_text = addr_span.get_text(strip=True)
            names_id = addr_span["id"].replace("Addresses", "Names")
            venue_span = soup.find("span", id=names_id)
            venue_text = venue_span.get_text(strip=True) if venue_span else ""

            parsed_addr = parse_address(raw_address_text)
            loc_info = {
                "Venue": venue_text,
                "Address": parsed_addr["Address"],
                "City": parsed_addr["City"],
                "State-FullName": parsed_addr["State-FullName"],
                "Zip": parsed_addr["Zip"],
            }
            results.append(loc_info)
        return {"locations": results}

    def extract_event_details(self, url: str, driver, max_retries: int = 1, **kwargs) -> dict:
        url = url.rstrip('/.')  # remove trailing slash or period
        logger.info(f"Extracting PerfectGame events from URL: {url}")
        selected_state_fallback = self._state_from_selected_location(kwargs.get("selected_location", ""))
        requested_group = "groupedevents.aspx" in url.lower()
        attempts = 0
        while attempts < max_retries:
            attempts += 1
            try:
                soup = self._get_soup(driver, url, kwargs.get("wait_time", 30))

                # Avoid touching driver.current_url for request-fetched grouped pages (or dead sessions)
                if driver is not None and ("groupedevents.aspx" not in url.lower() or getattr(self, "_last_soup_used_browser", False)):
                    try:
                        final_url = driver.current_url.rstrip('/.')
                        if final_url != url:
                            logger.info(f"URL redirected to: {final_url}")
                            url = final_url
                    except Exception as e:
                        if _is_driver_disconnected_exception(e):
                            raise WebDriverDisconnectedError(str(e)) from e
                        # otherwise ignore; url remains the requested one
                events = self._parse_grouped_page(driver, soup)
                if not events:
                    logger.info("No grouped event data found; falling back to single event parser.")
                    return self._parse_single_event_page(
                        driver,
                        url,
                        selected_state_fallback=selected_state_fallback,
                        soup=soup,
                        group_context=requested_group and "/events/default.aspx" in url.lower(),
                    )

                age_groups = []
                comp_levels = []
                valid_levels = {lvl.upper() for lvl in VALID_LEVELS}
                for ev in events:
                    age = ev.get("age_group", "")
                    level = ev.get("competition_level", "")
                    if age and age not in age_groups:
                        age_groups.append(age)
                    # competition_level may be a comma-separated list (e.g. "AA,CP") for card-based grouped pages
                    if level:
                        for tok in [t.strip() for t in re.split(r"[,/|]", level) if t.strip()]:
                            tok_u = tok.upper()
                            if tok_u in valid_levels and tok_u not in comp_levels:
                                comp_levels.append(tok_u)

                first_event = events[0]
                common_event_id = first_event.get("event_id", "")
                if not common_event_id:
                    logger.info("No event ID found in first event.")
                    return {}

                unique_venues = set()
                for e in events:
                    city_str = e.get("city", "").strip()
                    ballpark_str = e.get("ballpark", "").strip()
                    if ballpark_str:
                        unique_venues.add((city_str, ballpark_str))
                multiple_venues = (len(unique_venues) > 1)

                row = {col: "" for col in OUTPUT_COLUMNS}
                # Clean the tournament name so we remove leading year/age group AND parentheses
                row["Tournament-name"] = clean_tournament_name(first_event.get("tournament_name", ""))
                row["Start-date"] = first_event.get("start_date", "")
                row["End-date"] = first_event.get("end_date", "")

                for j in range(1, 14):
                    row[f"Age-groups.{j}"] = age_groups[j - 1] if j - 1 < len(age_groups) else ""
                    row[f"Entry-fee.{j}"] = ""
                for j in range(1, 10):
                    row[f"Competition-level.{j}"] = comp_levels[j - 1] if j - 1 < len(comp_levels) else ""
                row["Select All Competition Levels That Apply."] = ""

                row["Gate-fees"] = "NA"
                row["Hotel-required"] = "NA"
                row["Tournament-type"] = "Team"
                row["Tournament-surface"] = "NA"

                common_contact = self._parse_contact_info(driver, common_event_id)
                row["Contact-name"] = common_contact.get("contact_name", "")
                row["Email"] = common_contact.get("email", "")
                row["Phone-number"] = common_contact.get("phone", "")
                row["Guaranteed-games"] = common_contact.get("guaranteed_games", "")
                row["Website-url"] = url
                row["More-information-and-additional-tournament-locations"] = ""

                row["Are-there-multiple_locations"] = "Yes" if multiple_venues else "No"
                row["Venue"] = "Multiple Venues" if multiple_venues else first_event.get("ballpark", "")

                locations_url = f"https://www.perfectgame.org/Events/Locations.aspx?event={common_event_id}"
                logger.info(f"Loading locations page: {locations_url}")
                loc_soup = self._get_soup(driver, locations_url, kwargs.get("wait_time", 30))
                loc_data = self._parse_locations_page(loc_soup)
                all_locs = loc_data.get("locations", [])

                best_loc = None
                best_score = -1
                for loc in all_locs:
                    s = score_location(loc)
                    if s > best_score:
                        best_score = s
                        best_loc = loc

                if best_loc:
                    row["Address"] = best_loc.get("Address", "")
                    row["City"] = best_loc.get("City", first_event.get("city", ""))
                    row["State-FullName"] = (
                        best_loc.get("State-FullName")
                        or first_event.get("state", "")
                        or selected_state_fallback
                    )
                    row["Zip"] = best_loc.get("Zip", "")
                else:
                    row["Address"] = ""
                    row["City"] = first_event.get("city", "")
                    row["State-FullName"] = first_event.get("state", "") or selected_state_fallback
                    row["Zip"] = ""

                logger.info(f"Extracted event details from URL: {url}")
                return row
            except (TimeoutException, NoSuchElementException) as e:
                logger.warning(f"Timeout/element error at {url}: {e}")
                time.sleep(3)
            except WebDriverException as e:
                # If the webdriver session is gone, bubble up so the runner can restart.
                if _is_driver_disconnected_exception(e):
                    raise WebDriverDisconnectedError(str(e)) from e
                logger.warning(f"WebDriver error at {url}: {e}")
                time.sleep(3)
            except Exception as e:
                from common.source_exclusions import SourceEventIncomplete
                if isinstance(e, SourceEventIncomplete):
                    raise
                if isinstance(e, WebDriverDisconnectedError) or is_driver_connection_error(e):
                    raise
                logger.exception(f"Unexpected error at {url}: {e}")
                return {}
        logger.info(f"Failed to extract data for URL: {url} after {max_retries} retries.")
        return {}

    def _parse_single_event_page(self, driver, url: str, selected_state_fallback: str = "", soup=None, group_context=False) -> dict:
        url = url.rstrip('/.')
        if soup is None:
            soup = self._get_soup(driver, url)
            if driver is not None and "groupedevents.aspx" not in url.lower():
                final_url = driver.current_url.rstrip('/.')
                if final_url != url:
                    logger.info(f"Fallback: URL redirected to: {final_url}")
                    url = final_url

        row = {col: "" for col in OUTPUT_COLUMNS}

        header_a = soup.find("a", id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventNameNew")
        raw_tournament_name = header_a.get_text(strip=True) if header_a else ""
        row["Tournament-name"] = clean_tournament_name(raw_tournament_name)

        age_group, comp_level = "", ""
        # Updated regex to capture age group even without a competition level.
        dm = re.search(r"(\d+U)(?:\s*\((.+?)\))?", raw_tournament_name)
        if dm:
            age_group = dm.group(1)
            comp_level = dm.group(2) if dm.group(2) else ""
        row["Age-groups.1"] = age_group
        row["Competition-level.1"] = comp_level
        if group_context and not comp_level:
            levels = extract_competition_levels_from_parentheses(raw_tournament_name)
            for index, level in enumerate(levels[:9], 1):
                row[f"Competition-level.{index}"] = level
        if group_context and soup.find(id=re.compile(r"pnlTournaments$")):
            # Preserve the grouped export contract when the provider now serves
            # one explicitly recognized team tournament at its child URL.
            row["Tournament-type"] = "Team"
            row["Tournament-surface"] = "NA"

        date_elem = soup.find(id="ContentTopLevel_ContentPlaceHolder1_lblEventDateNew")
        if not date_elem:
            date_elem = soup.find(id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblDatesNew")
        if date_elem:
            date_text = date_elem.get_text(strip=True)
            if date_text.strip().upper() in {"DATE TBD", "DATES TBD", "TBD"}:
                from common.source_exclusions import SourceEventIncomplete
                raise SourceEventIncomplete("PerfectGame explicitly publishes DATE TBD; a future start cannot be verified")
            year_source = date_text if re.search(r"\b20\d{2}\b", date_text) else raw_tournament_name
            start_date, end_date = self._parse_date_range(date_text, year_source)
            row["Start-date"] = start_date
            row["End-date"] = end_date
        else:
            row["Start-date"] = ""
            row["End-date"] = ""
        row["Website-url"] = url

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        event_id = qs.get("event", [""])[0]

        if event_id:
            locations_url = f"https://www.perfectgame.org/Events/Locations.aspx?event={event_id}"
            logger.info(f"Fallback: loading locations page: {locations_url}")
            loc_soup = self._get_soup(driver, locations_url)
            loc_data = self._parse_locations_page(loc_soup)
            all_locs = loc_data.get("locations", [])
            best_loc = None
            best_score = -1
            for loc in all_locs:
                s = score_location(loc)
                if s > best_score:
                    best_score = s
                    best_loc = loc
            if best_loc:
                row["Address"] = best_loc.get("Address", "")
                if group_context:
                    row["Venue"] = best_loc.get("Venue", "")
                row["City"] = best_loc.get("City", "")
                row["State-FullName"] = best_loc.get("State-FullName", "") or selected_state_fallback
                row["Zip"] = best_loc.get("Zip", "")
            else:
                row["Address"] = ""
                row["City"] = ""
                row["State-FullName"] = selected_state_fallback
                row["Zip"] = ""
            row["Are-there-multiple_locations"] = "No"

            contact_info = self._parse_contact_soup(soup)
            row["Email"] = contact_info.get("email", "")
            row["Contact-name"] = contact_info.get("contact_name", "")
            row["Phone-number"] = contact_info.get("phone", "")
            row["Guaranteed-games"] = contact_info.get("guaranteed_games", "")
        else:
            row["Are-there-multiple_locations"] = "No"
            row["Guaranteed-games"] = ""

        logger.info(f"Fallback single event parser extracted data from URL: {url}")
        return row
