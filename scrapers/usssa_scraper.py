# scrapers/usssa_scraper.py

import logging
import time
import re
import json
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import List, Dict, Tuple
from bs4 import BeautifulSoup
from us import states  # Ensure you have 'us' library installed: pip install us
from common.config import OUTPUT_COLUMNS, VALID_LEVELS
from common.collection import CollectedLinks
from common.validation import parse_event_date
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)

from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)

class USSSAScraper(BaseScraper):
    """
    USSSAScraper implements the BaseScraper for USSSA events.
    """
    SCRAPER_NAME = "USSSA"
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"

    def _repair_dates_from_event_schema(self, soup, name, start, end):
        """Use published ISO dates only when the visible range fails validation."""
        parsed_start, parsed_end = parse_event_date(start), parse_event_date(end)
        if parsed_start and parsed_end and parsed_start <= parsed_end:
            return start, end
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.get_text())
            except (ValueError, TypeError):
                continue
            nodes = payload if isinstance(payload, list) else [payload]
            if isinstance(payload, dict) and isinstance(payload.get("@graph"), list):
                nodes += payload["@graph"]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                types = node.get("@type", [])
                types = [types] if isinstance(types, str) else types
                if not isinstance(types, list) or "Event" not in types:
                    continue
                if unescape(str(node.get("name", ""))).strip().casefold() != name.strip().casefold():
                    continue
                try:
                    explicit_start = datetime.strptime(node["startDate"], "%Y-%m-%d").date()
                    explicit_end = datetime.strptime(node["endDate"], "%Y-%m-%d").date()
                except (KeyError, ValueError, TypeError):
                    continue
                if explicit_end >= explicit_start:
                    return explicit_start.strftime("%m/%d/%Y"), explicit_end.strftime("%m/%d/%Y")
        return start, end

    def _state_from_usssa_host(self, url: str) -> str:
        try:
            host = (urlparse(url).hostname or "").lower()
        except Exception:
            return ""

        m = re.match(r"^([a-z]{2})baseball\.usssa\.com$", host)
        if not m:
            return ""

        state_abbr = m.group(1).upper()
        state_obj = states.lookup(state_abbr)
        return state_obj.name if state_obj else ""

    def _parse_venue_address(self, venue_name: str, address_full: str) -> Dict[str, str]:
        venue_name = (venue_name or "").strip()
        address_full = (address_full or "").strip()
        if not address_full:
            return {
                "Venue": venue_name,
                "Address": "",
                "City": "",
                "State": "",
                "Zip Code": "",
            }

        parts = [p.strip() for p in address_full.split(",") if p.strip()]
        if parts and venue_name and parts[0].lower() == venue_name.lower():
            parts = parts[1:]

        if len(parts) >= 4:
            zip_code = parts[-1]
            state_abbr = parts[-2]
            city = parts[-3]
            address = ", ".join(parts[:-3]).strip()
            state_obj = states.lookup(state_abbr)
            state_full = state_obj.name if state_obj else state_abbr
            return {
                "Venue": venue_name,
                "Address": address,
                "City": city,
                "State": state_full,
                "Zip Code": zip_code,
            }

        return {
            "Venue": venue_name,
            "Address": address_full,
            "City": "",
            "State": "",
            "Zip Code": "",
        }

    def _page_has_no_events(self, soup: BeautifulSoup) -> bool:
        error_box = soup.select_one("div.events-error")
        if not error_box:
            return False

        text = error_box.get_text(" ", strip=True).lower()
        return "no events found" in text

    def _wait_for_listing_state(self, driver, wait_time: int = 30):
        return WebDriverWait(driver, wait_time).until(
            lambda d: (
                d.find_elements(By.CSS_SELECTOR, 'a[href*="/event/"]')
                or d.find_elements(By.CSS_SELECTOR, "div.pagination.initial_query_pagenav")
                or d.find_elements(By.CSS_SELECTOR, "div.events-error")
            )
        )

    def _national_api(self, driver, action, params=None):
        response = driver.execute_async_script("""
            const action = arguments[0], params = arguments[1], done = arguments[2];
            const abort = new AbortController();
            const timeout = setTimeout(() => abort.abort(), 20000);
            const options = {signal: abort.signal};
            if (params !== null) {
                options.method = 'POST';
                options.headers = {'Content-Type': 'application/x-www-form-urlencoded'};
                options.body = new URLSearchParams({...params, token: window.apiAccessToken || ''});
            }
            fetch('/api/?action=' + encodeURIComponent(action), options)
                .then(async r => ({ok: r.ok, status: r.status, data: await r.json()}))
                .then(done).catch(() => done({ok: false, error: 'National source request failed'}))
                .finally(() => clearTimeout(timeout));
        """, action, params)
        if not isinstance(response, dict) or not response.get("ok"):
            raise ValueError("National USSSA source request failed")
        return response.get("data")

    def _verify_redirected_empty(self, driver, base_url):
        """Verify an unavailable state site against every published national season."""
        state_name = self._state_from_usssa_host(base_url)
        if not state_name:
            raise ValueError("Unrecognized USSSA state redirect")
        driver.get("https://www.usssa.com/baseball/eventSearch/")
        if (urlparse(driver.current_url).hostname or "").lower() != "www.usssa.com":
            raise ValueError("National USSSA search redirected unexpectedly")
        deadline = time.monotonic() + 60
        seasons = self._national_api(driver, "getEventSearchSeasons")
        fields = self._national_api(driver, "getBasicEventSearchFieldsV11", {"sportID": "11"})
        baseball_seasons = seasons.get("11") if isinstance(seasons, dict) else None
        states_data = fields.get("states") if isinstance(fields, dict) else None
        if not isinstance(baseball_seasons, list) or not 0 < len(baseball_seasons) <= 100 or not isinstance(states_data, list):
            raise ValueError("Invalid national USSSA season/state metadata")
        matches = [s for s in states_data if isinstance(s, dict) and s.get("name") == state_name]
        if len(matches) != 1 or not str(matches[0].get("value", "")).isdigit():
            raise ValueError("National USSSA state filter unavailable")
        season_ids = []
        for season in baseball_seasons:
            if not isinstance(season, dict) or not str(season.get("value", "")).isdigit():
                raise ValueError("Invalid national USSSA season identifier")
            season_ids.append(str(season["value"]))
        if len(set(season_ids)) != len(season_ids):
            raise ValueError("Repeated national USSSA season identifier")
        start = (datetime.now() + timedelta(days=1)).strftime("%m/%d/%Y")
        for season_id in season_ids:
            if time.monotonic() >= deadline:
                raise ValueError("National USSSA verification budget exceeded")
            payload = self._national_api(driver, "eventSearchSimpleV11", {
                "sportID": "11", "seasonID": season_id, "regionID": str(matches[0]["value"]),
                "stateID": "", "startDate": start, "endDate": "", "classID": "", "statureID": "",
                "age": "", "zip": "", "mile": "", "director": "", "parkID": "",
            })
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise ValueError("Invalid national USSSA event response")
            if payload["results"]:
                raise ValueError("National USSSA source contains events; state detail discovery unavailable")
        logger.info("Verified no future USSSA events for %s across %d published seasons.", state_name, len(season_ids))
        return 0

    def _get_total_pages(self, driver, base_url: str, wait_time: int = 30) -> int:
        """
        Determine the total number of pages from the pagination section on the USSSA site.
        """
        try:
            driver.get(base_url)
            landed_url = getattr(driver, "current_url", "")
            if isinstance(landed_url, str) and (urlparse(landed_url).hostname or "").lower() in {"tournamentgear.com", "www.tournamentgear.com"}:
                return self._verify_redirected_empty(driver, base_url)
            self._wait_for_listing_state(driver, wait_time)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            self._first_listing_soup = soup

            if self._page_has_no_events(soup):
                logger.info("USSSA listing has no events for this location.")
                return 0

            pagination = soup.find('div', class_='pagination initial_query_pagenav')
            if pagination:
                # Find all <a> with class "page-numbers" that contain digit text
                page_numbers = [
                    int(link.text.strip())
                    for link in pagination.find_all('a', class_='page-numbers')
                    if link.text.strip().isdigit()
                ]
                if page_numbers:
                    return max(page_numbers)

            return 1  # Default to 1 if no pages found
        except TimeoutException:
            raise
        except Exception as e:
            logger.error(f"Error determining total pages: {e}")
            raise

    def collect_event_links(
        self,
        driver,
        base_url: str = "https://cabaseball.usssa.com/events/",
        total_pages: int = 11,
        wait_time: int = 30,
        pause: int = 3
    ) -> List[str]:
        """
        Collect event links from the specified USSSA base URL across multiple pages.
        """
        event_links = CollectedLinks()
        deadline = time.monotonic() + 600

        # Dynamically determine total pages
        self._first_listing_soup = None
        total_pages = self._get_total_pages(driver, base_url, wait_time)
        seen_links, seen_pages = set(), set()
        if total_pages > 200:
            event_links.complete = False
            event_links.error = "USSSA pagination exceeds 200-page budget"
            total_pages = 200
        logger.info(f"Total pages found: {total_pages}")

        if total_pages == 0:
            logger.info("No USSSA events found for %s.", base_url)
            return event_links

        for page_num in range(1, total_pages + 1):
            if time.monotonic() > deadline:
                event_links.complete = False
                event_links.error = "USSSA listing time budget exceeded"
                break
            event_links.pages = page_num
            parsed = urlparse(base_url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if page_num > 1:
                query["paged"] = [str(page_num)]
            url = urlunparse(parsed._replace(query=urlencode(query, doseq=True))) if page_num > 1 else base_url
            logger.info(f"Scraping page {page_num}: {url}")

            try:
                if page_num == 1 and self._first_listing_soup is not None:
                    soup = self._first_listing_soup
                else:
                    driver.get(url)
                    self._wait_for_listing_state(driver, wait_time)
                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                if self._page_has_no_events(soup):
                    logger.info("No USSSA events found on page %d for %s.", page_num, base_url)
                    break
                anchors = soup.find_all('a', href=True)
                fingerprint = tuple(sorted({a['href'] for a in anchors if '/event/' in a['href']
                                            and 'registration.usssa.com/event/' not in a['href']}))
                if not fingerprint or fingerprint in seen_pages:
                    event_links.complete, event_links.error = False, "USSSA missing links or repeated event page"
                    break
                seen_pages.add(fingerprint)

                for anchor in anchors:
                    href = anchor['href']
                    if "registration.usssa.com/event/" in href:
                        continue
                    if '/event/' in href and href not in seen_links:
                        seen_links.add(href)
                        event_links.append(href)
                        logger.debug(f"Found event link: {href}")

            except TimeoutException:
                logger.warning(f"Timeout while loading page {page_num}. Continuing to next page.")
                event_links.complete = False
                event_links.error = f"USSSA listing timeout on page {page_num}"
            except WebDriverException as e:
                logger.error(f"WebDriverException while loading page {page_num}: {e}")
                raise
            except Exception as e:
                from common.driver_manager import is_driver_connection_error
                if is_driver_connection_error(e):
                    raise
                logger.error(f"Error scraping page {page_num}: {e}")
                event_links.complete = False
                event_links.error = str(e)
            finally:
                time.sleep(pause)

        logger.info(f"Total event links collected: {len(event_links)}")
        return event_links


    def get_event_key(self, url: str) -> str:
        """
        Given a USSSA-like event URL, return a unique key to identify the event.
        e.g.,
        https://vabaseball.usssa.com/event/seb-rumble-in-the-south-2/
        -> "seb-rumble-in-the-south-2"
        or with query params:
        https://some.usssa.com/?eventid=12345
        -> "12345"
        """
        # 1. Check for query param 'eventid'
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        event_id = qs.get("eventid", [None])[0]
        if event_id:
            return event_id

        # 2. Check for /event/ or /events/ slug at the end of the path
        match = re.search(r'/(?:event|events)/([^/]+)/?$', url)
        if match:
            return match.group(1)

        # 3. Fallback: return the entire URL if neither pattern worked
        return url


    def _process_age_groups(self, age_group_str: str) -> List[str]:
        """
        Convert a range-based age group string like '8U - 14U' into a list
        of individual age groups: ['8U', '9U', ..., '14U'].
        """
        age_groups = []
        try:
            if '-' in age_group_str:
                start, end = age_group_str.split('-')
                start_num = int(re.findall(r'\d+', start)[0])
                end_num = int(re.findall(r'\d+', end)[0])
                for age in range(start_num, end_num + 1):
                    age_groups.append(f"{age}u")
            else:
                # Single age group
                m = re.search(r'\d+', age_group_str)
                if m:
                    age_groups.append(f"{m.group()}u")
        except Exception as e:
            logger.error(f"Error processing age groups '{age_group_str}': {e}")
        return age_groups

    def _extract_competition_levels_and_gg(self, driver, soup, VALID_LEVELS) -> Tuple[List[str], str]:
        """
        Extract competition levels and guaranteed games from the USSSA table layout.
        """
        competition_levels = []
        guaranteed_games = ""

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_all_elements_located((By.CSS_SELECTOR, "#division_event_tournament tr"))
            )

            division_table = driver.find_element(By.ID, "division_event_tournament")
            rows = division_table.find_elements(By.TAG_NAME, "tr")

            for row_index, row in enumerate(rows):
                try:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if cells:
                        # Remove <span> from the text
                        possible_span = cells[0].find_elements(By.TAG_NAME, "span")
                        span_text = possible_span[0].text if possible_span else ""
                        division_text = cells[0].text.replace(span_text, "").strip()
                        normalized_text = division_text.replace("\u00a0", " ").strip()

                        if "Under" in normalized_text:
                            _, comp_level = normalized_text.split(" Under", 1)
                            comp_level = comp_level.strip()
                            if comp_level in VALID_LEVELS and comp_level not in competition_levels:
                                competition_levels.append(comp_level)

                    gg_elements = row.find_elements(By.CLASS_NAME, "game-guarantee-td")
                    if gg_elements:
                        gg_value = gg_elements[0].text.strip()
                        if gg_value.isdigit():
                            guaranteed_games = gg_value
                except Exception as e:
                    from common.driver_manager import is_driver_connection_error
                    if is_driver_connection_error(e):
                        raise
                    logger.error(f"Error processing row {row_index}: {e}")
        except TimeoutException:
            # Possible fallback layout
            try:
                verbose_section = driver.find_element(By.CSS_SELECTOR, "div.event-list li div._item_text ul")
                items = verbose_section.find_elements(By.TAG_NAME, "li")
                for item in items:
                    text = item.text.strip()
                    for level in VALID_LEVELS:
                        if level in text and level not in competition_levels:
                            competition_levels.append(level)
            except NoSuchElementException:
                logger.warning("No competition levels found in fallback layout.")

        logger.debug(f"Competition levels: {competition_levels}, Guaranteed games: {guaranteed_games}")
        return competition_levels, guaranteed_games

    def extract_event_details(self, url: str, driver, max_retries: int = 1, **kwargs) -> Dict:
        """
        Extract details for a single USSSA event page.
        Returns a dictionary of event data fields, or None on failure.
        """
        logger.info(f"Extracting event details from: {url}")
        retries = 0

        while retries < max_retries:
            try:
                driver.get(url)
                main_tab = WebDriverWait(driver, 30).until(
                    EC.presence_of_element_located((By.CLASS_NAME, "event-list"))
                )
                driver.execute_script("arguments[0].scrollIntoView(true);", main_tab)
                WebDriverWait(driver, 5).until(lambda d: main_tab.is_displayed())

                # Attempt to locate Age Groups
                try:
                    age_groups_section = driver.find_element(By.CSS_SELECTOR, 'div.event-list ul li:nth-child(2) div._item_text')
                    age_groups_text = age_groups_section.text.strip()
                    age_groups = self._process_age_groups(age_groups_text)
                except NoSuchElementException:
                    age_groups = []
                    logger.warning(f"No Age Groups section found for URL: {url}")

                soup = BeautifulSoup(driver.page_source, 'html.parser')

                # Extract competition levels & guaranteed games
                competition_levels, guaranteed_games = self._extract_competition_levels_and_gg(driver, soup, VALID_LEVELS)

                # Extract tournament name
                tournament_name = ""
                tournament_name_tag = soup.find('h3', class_='_heading')
                if tournament_name_tag:
                    tournament_name = tournament_name_tag.get_text(strip=True)
                else:
                    logger.warning(f"Could not find tournament name for {url}")

                # Extract start and end dates
                start_date = end_date = ""
                date_element_tag = soup.find(class_='_value')
                date_element = date_element_tag.get_text(strip=True) if date_element_tag else ""
                if '-' in date_element:
                    parts = re.split(r"\s*-\s*", date_element, maxsplit=1)
                    if len(parts) == 2:
                        start_date = parts[0].strip()
                        end_date = parts[1].strip() or start_date
                    else:
                        start_date = end_date = date_element.replace("-", "").strip()

                    # Append the year if it isn't present in the substring
                    year_match = re.search(r'\d{4}', date_element)
                    year = year_match.group() if year_match else str(datetime.now().year)
                    if year not in start_date:
                        start_date = f"{start_date.strip()} {year}"
                    if year not in end_date:
                        end_date = f"{end_date.strip()} {year}"
                else:
                    date_value = date_element.strip()
                    year_match = re.search(r'\d{4}', date_value)
                    year = year_match.group() if year_match else str(datetime.now().year)
                    if date_value and year not in date_value:
                        date_value = f"{date_value} {year}"
                    start_date = end_date = date_value

                start_date, end_date = self._repair_dates_from_event_schema(
                    soup, tournament_name, start_date, end_date
                )

                # Extract venue details
                venues = []
                try:
                    venue_table = soup.find('table', id='list_view_wrapper')
                    if venue_table:
                        venue_rows = venue_table.find('tbody').find_all('tr')
                        for row in venue_rows:
                            cells = row.find_all('td')
                            if len(cells) >= 2:
                                venue_name = cells[0].get_text(strip=True)
                                address_full = cells[1].get_text(strip=True)
                                venues.append(self._parse_venue_address(venue_name, address_full))
                    else:
                        logger.warning(f"No venue table found for {url}")
                except Exception as e:
                    logger.error(f"Error extracting venue details for {url}: {e}")

                state_from_host = self._state_from_usssa_host(url)
                first_venue = venues[0] if venues else {
                    'Venue': 'N/A',
                    'Address': 'N/A',
                    'City': 'N/A',
                    'State': state_from_host or 'N/A',
                    'Zip Code': 'N/A'
                }
                if (not first_venue.get('State') or first_venue.get('State') == 'N/A') and state_from_host:
                    first_venue['State'] = state_from_host

                # Extract contact info
                contact_name = "N/A"
                email = "N/A"
                phone_number = "N/A"
                try:
                    contact_div = soup.find('div', class_='_winfo')
                    if contact_div:
                        # Contact Name
                        c_name_tag = contact_div.find('h3')
                        if c_name_tag:
                            contact_name = c_name_tag.get_text(strip=True)

                        # Email
                        email_tag = contact_div.find('a', href=re.compile(r'^mailto:'))
                        if email_tag:
                            email = email_tag.get('href', '').replace('mailto:', '').strip()

                        # Phone
                        phone_tag = contact_div.find('a', href=re.compile(r'^tel:'))
                        if phone_tag:
                            raw_phone = re.sub(r'\D', '', phone_tag.get('href', '').replace('tel:', ''))
                            # Format into xxx-xxx-xxxx
                            phone_number = re.sub(r'(\d{3})(\d{3})(\d{4})', r'\1-\2-\3', raw_phone)
                except Exception as e:
                    logger.error(f"Error extracting contact info for {url}: {e}")

                data = {
                    "Tournament-name": tournament_name,
                    "Start-date": start_date.strip(),
                    "End-date": end_date.strip(),
                    "Are-there-multiple_locations": "Yes" if len(venues) > 1 else "No",
                    "Address": first_venue.get('Address'),
                    "Address-2": "",
                    "Venue": "Multiple Locations" if len(venues) > 1 else first_venue.get('Venue'),
                    "City": first_venue.get('City'),
                    "State-FullName": first_venue.get('State'),
                    "Zip": first_venue.get('Zip Code'),
                    "Guaranteed-games": guaranteed_games,
                    "Gate-fees": "NA",
                    "Hotel-required": "NA",
                    "Tournament-type": "Team",
                    "Tournament-surface": "NA",
                    "Contact-name": contact_name,
                    "Phone-number": phone_number,
                    "Email": email,
                    "Website-url": url,
                    "More-information-and-additional-tournament-locations": ""
                }

                # Fill in age groups
                for i in range(1, 14):
                    age_key = f"Age-groups.{i}"
                    fee_key = f"Entry-fee.{i}"
                    data[age_key] = age_groups[i - 1] if i - 1 < len(age_groups) else ""
                    data[fee_key] = ""

                # Fill in competition levels
                for i in range(1, 10):
                    comp_key = f"Competition-level.{i}"
                    data[comp_key] = competition_levels[i - 1] if i - 1 < len(competition_levels) else ""

                data["Select All Competition Levels That Apply."] = ""

                # Ensure all columns exist in the final dict
                for col in OUTPUT_COLUMNS:
                    if col not in data:
                        data[col] = ""

                logger.debug(f"Extracted data from {url}: {data}")
                return data

            except (NoSuchElementException, TimeoutException) as e:
                logger.error(f"Timeout/NoSuchElement error at {url}: {e}")
                retries += 1
                time.sleep(5)
                continue
            except WebDriverException as e:
                logger.error(f"WebDriverException at {url}: {e}")
                from common.driver_manager import is_driver_connection_error
                if is_driver_connection_error(e):
                    raise
                retries += 1
                time.sleep(5)
                continue
            except Exception as e:
                from common.driver_manager import is_driver_connection_error
                if is_driver_connection_error(e):
                    raise
                logger.error(f"Unexpected error extracting details at {url}: {e}")
                return None

        logger.error(f"Failed to extract data for URL: {url} after {max_retries} retries.")
        return None
