import logging
import time
import requests
from urllib.parse import urlparse, parse_qs
import re
from datetime import datetime, timedelta
from typing import List, Dict
from us import states  # pip install us
from common.config import OUTPUT_COLUMNS, VALID_LEVELS  # Assumes these are defined in your project
from scrapers.base_scraper import BaseScraper
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# Selenium imports for waiting and DOM extraction.
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By

logger = logging.getLogger(__name__)

class PSTScraper(BaseScraper):
    """
    PSTScraper implements the BaseScraper for Premier Sports Tournaments events.
    It uses a POST endpoint for the main event data and a GET call to the schedule endpoint
    to retrieve competition level details. If the API POST fails (returns empty data),
    it falls back to extracting event links from the DOM using Selenium.
    """
    SCRAPER_NAME = "PST"
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = False
    LOCATIONS_SOURCE = "config"
    GEOCODE_TIMEOUT_SECONDS = 5
    API_BASE_URL = "https://registrations.premiersportstournaments.com/store/events"
    EVENT_BASE_URL = "https://registrations.premiersportstournaments.com/store/event"
    SCHEDULE_BASE_URL = "https://registrations.premiersportstournaments.com/store/event/schedule/search"

    def __init__(self):
        self.geolocator = Nominatim(user_agent="my_app")
        self.reverse_func = RateLimiter(self.geolocator.reverse, min_delay_seconds=1)
        self.events_by_url = {}  # Cache event details keyed by event URL.
        self.store_org_id = None
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "x-requested-with": "XMLHttpRequest",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/132.0.0.0 Safari/537.36"),
            "Origin": "https://registrations.premiersportstournaments.com"
        })

    def _build_future_only_payload(self, page: int) -> Dict[str, str | int]:
        tomorrow_dt = datetime.now() + timedelta(days=1)
        tomorrow = f"{tomorrow_dt.month}/{tomorrow_dt.day}/{tomorrow_dt.year}"
        return {
            "StartDateString": tomorrow,
            "EndDateString": "",
            "Gender": "-1",
            "SearchToken": "",
            "Page": page,
            "sportType": "",
        }

    # -------------------------------------------------------------------------
    # Reverse Geocoding Helper
    # -------------------------------------------------------------------------
    def reverse_geocode(self, lat: float, lon: float) -> Dict[str, str]:
        """
        Reverse geocodes (lat, lon) using Nominatim via geopy.
        Returns a dict with keys: 'venue_name', 'street', 'city', 'state', 'postal_code'.
        """
        # Use the pre-initialized reverse geocoding function.
        try:
            location = self.reverse_func((lat, lon), language="en", timeout=self.GEOCODE_TIMEOUT_SECONDS)
        except Exception as e:
            logger.warning("Reverse geocode failed for (%s, %s): %s", lat, lon, e)
            return {}

        if not location:
            return {}

        raw_data = location.raw
        address = raw_data.get("address", {})

        house_number = address.get("house_number", "")
        road = address.get("road") or address.get("footway") or ""
        street = f"{house_number} {road}".strip() if (house_number or road) else ""

        city = address.get("city") or address.get("town") or address.get("village") or address.get("hamlet") or ""
        state = address.get("state", "")
        postal_code = address.get("postcode", "")

        venue_name = address.get("attraction") or address.get("building") or address.get("public_building") or address.get("amenity") or ""

        return {
            "venue_name": venue_name,
            "street": street,
            "city": city,
            "state": state,
            "postal_code": postal_code,
        }

    
    def build_address_and_venue(self, event: dict) -> Dict[str, str]:
        location_name = event.get("Location") or ""
        street = event.get("StreetAddress") or ""
        city = event.get("City") or ""
        state_abbr = event.get("StateRegion") or ""
        postal = event.get("PostalCode") or ""
        lat = event.get("Latitude")
        lon = event.get("Longitude")

        needs_geocode = (not street) or (not city) or (not state_abbr) or (not postal)
        geocode_data = {}
        if needs_geocode and lat and lon:
            geocode_data = self.reverse_geocode(lat, lon)

        # Prefer the event's "Location" for the venue name if available.
        final_venue = location_name.strip() or geocode_data.get("venue_name", "")

        final_street = street.strip() or geocode_data.get("street", "")
        final_city = city.strip() or geocode_data.get("city", "")
        final_state_abbr = state_abbr.strip() or geocode_data.get("state", "")
        final_postal = postal.strip() or geocode_data.get("postal_code", "")

        # Convert state abbreviation to full name using 'us' library
        state_full = ""
        if final_state_abbr:
            state_obj = states.lookup(final_state_abbr)
            state_full = state_obj.name if state_obj else final_state_abbr

        return {
            "Venue": final_venue,
            "Address": final_street,
            "Address-2": event.get("ExtendedAddress", ""),
            "City": final_city,
            "State-FullName": state_full,
            "Zip": final_postal,
        }



    # -------------------------------------------------------------------------
    # Main Methods
    # -------------------------------------------------------------------------
    def collect_event_links(self, driver, base_url: str) -> List[str]:
        """
        Loads the store page, clicks the "Find Events" link,
        transfers cookies from Selenium to the requests session,
        then tries to retrieve event links via the API POST.
        If the POST returns no valid JSON, it falls back to extracting
        event links from the DOM based on the page’s actual structure.
        """
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        store_org_id = qs.get("storeorganizationid", [None])[0]
        if not store_org_id:
            logger.error("Store organization id not found in base_url.")
            return []
        self.store_org_id = store_org_id

        # Update session headers.
        self.session.headers.update({
            "Referer": f"https://registrations.premiersportstournaments.com/store?storeorganizationid={store_org_id}",
            "DNT": "1"
        })

        pre_get_url = f"https://registrations.premiersportstournaments.com/store?storeorganizationid={store_org_id}"
        logger.info(f"Loading store page: {pre_get_url}")
        try:
            driver.get(pre_get_url)
            # Wait for the "Find Events" link
            find_events_button = WebDriverWait(driver, 30).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "a[href='#events'].btn.btn-primary"))
            )
            logger.info("Clicking 'Find Events' link.")
            find_events_button.click()
            WebDriverWait(driver, 30).until(EC.url_contains("#events"))
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, ".card-widget"))
            )
            logger.info(f"Page URL after clicking: {driver.current_url}")

            # Transfer cookies
            selenium_cookies = driver.get_cookies()
            for cookie in selenium_cookies:
                self.session.cookies.set(cookie['name'], cookie['value'])

        except Exception as e:
            logger.error(f"Error during page load/click: {e}")

        all_event_urls = []
        current_page = 1
        while True:
            api_url = f"{self.API_BASE_URL}?storeorganizationid={store_org_id}"
            payload = self._build_future_only_payload(current_page)
            try:
                response = self.session.post(api_url, json=payload)
                response.raise_for_status()
                if not response.text.strip():
                    raise ValueError("Empty API response")
                data = response.json()
            except (requests.RequestException, ValueError) as e:
                logger.error(f"API error on page {current_page}: {e}")
                logger.info("Falling back to extracting event links from the DOM.")
                all_event_urls = self._extract_event_links_from_dom(driver)
                break

            results = data.get("Results", [])
            logger.info(f"API page {current_page} returned {len(results)} events.")
            if not results:
                logger.info("No events returned via API – last page reached.")
                break

            for event in results:
                event_id = event.get("Id")
                if not event_id:
                    continue
                event_url = f"{self.EVENT_BASE_URL}?eventid={event_id}"
                all_event_urls.append(event_url)
                self.events_by_url[event_url] = event

            current_page += 1
            time.sleep(0.5)

        return all_event_urls

    def get_event_key(self, url: str) -> str:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        event_id = qs.get("eventid", [None])[0]
        if event_id:
            return f"Event {event_id}"

        fragment = parsed.fragment
        if "?" in fragment:
            fragment = fragment.split("?", 1)[1]
        frag_qs = parse_qs(fragment)
        event_id_fragment = frag_qs.get("eventid", [None])[0]
        if event_id_fragment:
            return f"Event {event_id_fragment}"

        return "Unknown Event"

    def _extract_event_links_from_dom(self, driver) -> List[str]:
        """
        Fallback: Extract event links directly from the DOM using the provided HTML structure.
        Each event is contained in a div with class "card-widget".
        The event link is in an <a> tag whose href starts with "#event?eventid=".
        """
        links = []
        try:
            event_cards = driver.find_elements(By.CSS_SELECTOR, "div.card-widget")
            for card in event_cards:
                try:
                    anchor = card.find_element(By.CSS_SELECTOR, "a[href^='#event?eventid=']")
                    href = anchor.get_attribute("href")
                    if href:
                        links.append(href)
                except Exception as inner_e:
                    logger.warning(f"Could not extract link from a card: {inner_e}")
        except Exception as e:
            logger.error(f"Error extracting event links from DOM: {e}")
        return links

    def extract_event_details(self, url: str, driver) -> Dict:
        """
        Extract event details for a given event URL.
        Uses cached API data and retrieves competition levels via the schedule endpoint.
        """
        event = self.events_by_url.get(url)
        if not event:
            logger.error(f"Event details for URL {url} not found in cache.")
            return None

        def safe_get(key):
            return event.get(key) or ""

        def format_date(date_str: str) -> str:
            # If date_str is empty, return a default value
            if not date_str:
                return "N/A"
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S.%f")
                # Format date as "Jan 21 2025"
                return dt.strftime("%b %d %Y")
            except Exception:
                return "N/A"

        start_date = format_date(safe_get("StartDate"))
        end_date = format_date(safe_get("EndDate"))
        tournament_name = safe_get("Name")
        game_guarantee = safe_get("GamesGuaranteed")
        website_url = safe_get("Website") or safe_get("OrganizationWebsite")
        tournament_type = safe_get("Type")
        contact_name = safe_get("ContactName")
        email = safe_get("ContactEmail")
        # Normalize phone number using our helper function.
        phone_number = self._normalize_phone_number(safe_get("ContactPhone"))

        # Build final address/venue fields
        addr_info = self.build_address_and_venue(event)

        # Age groups
        raw_age_str = safe_get("YouthAgeGradesMale") or safe_get("YouthAgeGradesBoth")
        age_groups = self._parse_youth_age_grades(raw_age_str) if raw_age_str else []

        data = {
            "Tournament-name": tournament_name,
            "Start-date": start_date,
            "End-date": end_date,
            "Are-there-multiple_locations": "No",
            "Address": addr_info["Address"],
            "Address-2": addr_info["Address-2"],
            "Venue": addr_info["Venue"],
            "City": addr_info["City"],
            "State-FullName": addr_info["State-FullName"],
            "Zip": addr_info["Zip"],
            "Guaranteed-games": game_guarantee,
            "Gate-fees": "NA",
            "Hotel-required": "NA",
            "Tournament-type": "Team",
            "Tournament-surface": "NA",
            "Contact-name": contact_name,
            "Phone-number": phone_number,
            "Email": email,
            "Website-url": website_url,
            "More-information-and-additional-tournament-locations": ""
        }

        # Age groups
        for i in range(1, 11):
            data[f"Age-groups.{i}"] = age_groups[i - 1] if i - 1 < len(age_groups) else ""

        # Fetch competition levels
        competition_levels = []
        event_id = safe_get("Id")
        seen_levels = set()
        if event_id and self.store_org_id:
            schedule_url = f"{self.SCHEDULE_BASE_URL}?eventId={event_id}&storeorganizationid={self.store_org_id}"
            logger.info(f"Fetching schedule data for event {event_id} from {schedule_url}")
            try:
                schedule_response = self.session.get(schedule_url)
                schedule_response.raise_for_status()
                schedule_data = schedule_response.json()
                teams = schedule_data.get("Teams", [])
                for team in teams:
                    raw_division = (team.get("Division") or "").strip()
                    if not raw_division:
                        continue

                    # Look for any recognized level in the raw division string
                    found_level = None
                    for lvl in VALID_LEVELS:
                        # Compare in a case-insensitive way, or refine the matching logic as needed
                        if lvl.lower() in raw_division.lower():
                            found_level = lvl
                            break

                    # If we found a valid level and haven't seen it yet, add it
                    if found_level and found_level not in seen_levels:
                        if found_level.lower() != "a":
                            seen_levels.add(found_level)
                            competition_levels.append(found_level)
            except Exception as e:
                logger.error(f"Error fetching competition levels for event {event_id}: {e}")

        # Construct the direct event URL and override 'Website-url'
        event_page_url = (
            f"https://registrations.premiersportstournaments.com/"
            f"store?storeorganizationid={self.store_org_id}"
            f"#event?eventid={event_id}"
        )
        data["Website-url"] = event_page_url        

        # Now 'competition_levels' is an ordered list of unique levels that were found.
        # Populate them into Competition-level.1 ... Competition-level.10 columns
        for i in range(1, 11):
            data[f"Competition-level.{i}"] = competition_levels[i - 1] if i - 1 < len(competition_levels) else ""
        data["Select All Competition Levels That Apply."] = ""

        # Ensure all columns exist
        for col in OUTPUT_COLUMNS:
            if col not in data:
                data[col] = ""
        return data
    
    def _normalize_phone_number(self, phone: str) -> str:
        """
        Normalizes a phone number to the format xxx-xxx-xxxx.
        If multiple numbers are provided (separated by '/', commas, or semicolons), uses the first valid one.
        If no valid 10-digit number is found, returns the original string.
        """
        if not phone:
            return ""

        # Split on common delimiters if multiple numbers are present.
        for part in re.split(r"[\/,;]", phone):
            cleaned = re.sub(r"[^\d]", "", part)
            if len(cleaned) == 10:
                return f"{cleaned[:3]}-{cleaned[3:6]}-{cleaned[6:]}"
        # Fallback: try cleaning the whole string
        cleaned = re.sub(r"[^\d]", "", phone)
        if len(cleaned) == 10:
            return f"{cleaned[:3]}-{cleaned[3:6]}-{cleaned[6:]}"
        return phone   

    def _parse_youth_age_grades(self, age_str: str) -> List[str]:
        """
        Parses the YouthAgeGrades string into a list of individual age groups.
        Examples:
            "Boys: 18U, 16U-15U" --> ["15U", "16U", "18U"]
            "Boys: 14U-9U"      --> ["9U", "10U", "11U", "12U", "13U", "14U"]
            "Boys & Girls"      --> [] (no numeric ages found)
        """
        if not any(char.isdigit() for char in age_str):
            return []

        # Remove leading descriptors if present
        for prefix in ["Boys:", "Girls:", "Coed:", "Youth:"]:
            if age_str.startswith(prefix):
                age_str = age_str[len(prefix):]

        tokens = age_str.split(',')
        age_nums = set()
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            if '-' in token:
                parts = token.split('-')
                if len(parts) == 2:
                    try:
                        start = int(''.join(filter(str.isdigit, parts[0])))
                        end = int(''.join(filter(str.isdigit, parts[1])))
                        lo = min(start, end)
                        hi = max(start, end)
                        for n in range(lo, hi + 1):
                            age_nums.add(n)
                    except ValueError:
                        continue
            else:
                try:
                    n = int(''.join(filter(str.isdigit, token)))
                    age_nums.add(n)
                except ValueError:
                    continue
        return [f"{num}U" for num in sorted(age_nums)]
