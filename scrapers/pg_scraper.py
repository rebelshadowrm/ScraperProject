from datetime import datetime
import logging
import time
import re
from urllib.parse import urlparse, parse_qs, urljoin
from bs4 import BeautifulSoup, Tag
import requests
from requests.adapters import HTTPAdapter
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
        groups = {}
        current_page = 1
        logger.info(f"Loading base URL: {base_url}")
        driver.get(base_url)
        while True:
            logger.info(f"Processing page {current_page}")
            try:
                # Wait for the events table to be present
                WebDriverWait(driver, wait_time).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "table.rgMasterTable"))
                )
            except TimeoutException:
                logger.info("Timeout waiting for events table.")
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            group_headers = soup.find_all("tr", class_="rgGroupHeader")
            logger.info(f"Found {len(group_headers)} group headers on page {current_page}")
            if not group_headers:
                logger.info("No group headers found, ending pagination.")
                break

            for gh in group_headers:
                a_tag = gh.find("a", href=True)
                if not a_tag:
                    continue
                group_url = urljoin(base_url, a_tag["href"].rstrip('.'))
                parsed = urlparse(group_url)
                qs = parse_qs(parsed.query)
                gid = qs.get("gid", [None])[0]
                if gid and gid not in groups:
                    groups[gid] = group_url
                    logger.info(f"Found grouped event link: {group_url}")

            try:
                # Locate the Next Page button and check if it is clickable
                next_button = WebDriverWait(driver, wait_time).until(
                    EC.element_to_be_clickable((By.XPATH, "//button[@title='Next Page']"))
                )
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
                logger.error(f"Error during pagination: {e}")
                break

        logger.info(f"Collected {len(groups)} unique group entries across {current_page} pages.")
        return list(groups.values())

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
        - For grouped pages: ALWAYS use requests (server-rendered) to avoid webdriver hangs.
          If requests fails, raise so the caller can backoff/retry (do NOT fall back to Selenium).
        - For other pages: use Selenium, but detect session-death and raise a dedicated error
          so the outer runner can restart the driver and retry the current URL.
        """
        url = url.rstrip("/.")
        is_grouped = ("groupedevents.aspx" in url.lower())

        if is_grouped:
            soup = self._get_soup_requests(url)
            if soup is None:
                raise RuntimeError(f"requests fetch failed for grouped page: {url}")
            return soup

        try:
            if driver is None:
                raise WebDriverDisconnectedError("Selenium driver is None.")

            if hasattr(driver, "set_page_load_timeout"):
                driver.set_page_load_timeout(55)
            if hasattr(driver, "set_script_timeout"):
                driver.set_script_timeout(30)

            driver.get(url)
            WebDriverWait(driver, wait_time).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            return BeautifulSoup(driver.page_source, "html.parser")

        except (WebDriverException, TimeoutException) as e:
            if _is_driver_disconnected_exception(e):
                raise WebDriverDisconnectedError(str(e)) from e

            # Try to salvage whatever loaded
            try:
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
        year_match = re.search(r"(\d{4})", end_str)
        year = year_match.group(1) if year_match else str(datetime.now().year)
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
                start_date, end_date = self._parse_date_range(date_str, year)

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
            logger.error(f"Error loading detail page {detail_url}: {e}")
            return {"contact_name": "", "email": "", "phone": "", "guaranteed_games": ""}
        
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
        
        logger.debug(f"Extracted contact info for event {event_id}")
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
        attempts = 0
        while attempts < max_retries:
            attempts += 1
            try:
                soup = self._get_soup(driver, url, kwargs.get("wait_time", 30))

                # Avoid touching driver.current_url for request-fetched grouped pages (or dead sessions)
                if driver is not None and "groupedevents.aspx" not in url.lower():
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
                    return self._parse_single_event_page(driver, url)

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
                    row["State-FullName"] = best_loc.get("State-FullName", first_event.get("state", ""))
                    row["Zip"] = best_loc.get("Zip", "")
                else:
                    row["Address"] = ""
                    row["City"] = first_event.get("city", "")
                    row["State-FullName"] = first_event.get("state", "")
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
                logger.exception(f"Unexpected error at {url}: {e}")
                return {}
        logger.info(f"Failed to extract data for URL: {url} after {max_retries} retries.")
        return {}

    def _parse_single_event_page(self, driver, url: str) -> dict:
        url = url.rstrip('/.')
        soup = self._get_soup(driver, url)
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

        date_elem = soup.find(id="ContentTopLevel_ContentPlaceHolder1_lblEventDateNew")
        if not date_elem:
            date_elem = soup.find(id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblDatesNew")
        if date_elem:
            date_text = date_elem.get_text(strip=True)
            start_date, end_date = self._parse_date_range(date_text, date_text)
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
                row["City"] = best_loc.get("City", "")
                row["State-FullName"] = best_loc.get("State-FullName", "")
                row["Zip"] = best_loc.get("Zip", "")
            else:
                row["Address"] = ""
                row["City"] = ""
                row["State-FullName"] = ""
                row["Zip"] = ""
            row["Are-there-multiple_locations"] = "No"

            contact_info = self._parse_contact_info(driver, event_id)
            row["Email"] = contact_info.get("email", "")
            row["Contact-name"] = contact_info.get("contact_name", "")
            row["Phone-number"] = contact_info.get("phone", "")
            row["Guaranteed-games"] = contact_info.get("guaranteed_games", "")
        else:
            row["Are-there-multiple_locations"] = "No"
            row["Guaranteed-games"] = ""

        logger.info(f"Fallback single event parser extracted data from URL: {url}")
        return row
