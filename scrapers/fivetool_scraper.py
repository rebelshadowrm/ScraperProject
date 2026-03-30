import logging
import time
import requests
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from typing import List, Dict
import re
from us import states  # pip install us
from common.config import OUTPUT_COLUMNS, VALID_LEVELS  # Assumes these are defined in your project
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


def parse_age_groups(label: str) -> List[str]:
    """
    Given a division label (e.g. "15U/16U" or "Pitcher Only"),
    return a list of valid age groups (8u through 18u).
    - Splits on slashes, commas, plus signs, and whitespace.
    - Normalizes 'U' to lowercase 'u'.
    - Ensures each age is between 8u and 18u.
    """
    # Split on '/', ',', '+', or whitespace.
    parts = re.split(r'[\/,\+\s]+', label.strip())
    valid_ages = []
    for part in parts:
        # Normalize to something like "15u"
        match = re.match(r'^(\d{1,2})[Uu]$', part.strip())
        if match:
            age_num = int(match.group(1))
            if 8 <= age_num <= 18:
                valid_ages.append(f"{age_num}u")
    return valid_ages


class FiveToolScraper(BaseScraper):
    """
    FiveToolScraper implements the BaseScraper for FiveTool events.
    It uses FiveTool’s JSON API to collect event links and details.
    """
    SCRAPER_NAME = "FiveTool"
    BATCH_DELAY_SECONDS = 0
    API_BASE_URL = "https://events.fivetool.org/ajax-events"
    EVENT_BASE_URL = "https://events.fivetool.org/events/"

    def __init__(self):
        # Cache event details keyed by the full event URL.
        self.events_by_url = {}

    def collect_event_links(self, driver, base_url: str) -> List[str]:
        """
        Collect event links from the FiveTool API.
        The base_url (from locations.json) is expected to include a query
        parameter for the state (e.g. "?state=CA"). The Selenium driver parameter
        is ignored.
        """
        # Extract the state from the base_url.
        state = "CA"  # default
        parsed = urlparse(base_url)
        qs = parse_qs(parsed.query)
        state = qs.get("state", [state])[0]

        all_event_urls = []
        page = 1
        while True:
            params = {
                "page": page,
                "past_events": "false",  # adjust if needed
                "state": state
            }
            try:
                response = requests.get(self.API_BASE_URL, params=params)
                response.raise_for_status()
                data = response.json()
                event_list = data.get("eventlist", [])
                logger.info(f"Page {page} returned {len(event_list)} events.")
                if not event_list:
                    break
                for event in event_list:
                    url_key = event.get("url_key", "")
                    if url_key:
                        event_url = f"{self.EVENT_BASE_URL}{url_key}"
                        all_event_urls.append(event_url)
                        # Cache the full event details for later lookup.
                        self.events_by_url[event_url] = event
                page += 1
            except requests.RequestException as e:
                logger.error(f"Error fetching page {page}: {e}")
                break
            time.sleep(0.5)
        return all_event_urls

    def extract_event_details(self, url: str, driver) -> Dict:
        """
        Extract event details for a given event URL.
        This method looks up the event's JSON details (cached during collect_event_links)
        and maps the fields to the standard output format.
        The Selenium driver parameter is ignored.
        """
        event = self.events_by_url.get(url)
        if not event:
            logger.error(f"Event details for URL {url} not found in cached data.")
            return None

        # Convert dates from YYYY-MM-DD to the valid format "Jun 14 2025"
        def format_date(date_str: str) -> str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d")
                return dt.strftime("%b %d %Y")
            except Exception:
                return date_str

        start_date = format_date(event.get("start_date", ""))
        end_date = format_date(event.get("end_date", ""))

        tournament_name = event.get("name", "")
        game_guarantee = event.get("game_guarantee", "")
        website_url = url  # Already constructed in collect_event_links

        # Extract venue details with fallback.
        venue_info = self.extract_venue_info(event)
        are_multiple_venues = "Yes" if event.get("venues") and len(event["venues"]) > 1 else "No"

        # Extract contact details (first contact if available).
        contact_name = ""
        email = ""
        phone_number = ""
        contacts = event.get("contacts", [])
        if contacts:
            contact = contacts[0]
            contact_name = f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip()
            email = contact.get("email", "")
            phone_number = contact.get("telephone", "")

        tournament_type = "Team"

        # Derive age groups and competition levels from divisions.
        age_groups_set = set()
        competition_levels = []
        divisions = event.get("divisions", [])

        for div in divisions:
            label = div.get("label", "").strip()
            # Parse out valid age groups from the label:
            valid_ages = parse_age_groups(label)
            for age in valid_ages:
                age_groups_set.add(age)

            # For competition levels, keep them if they are not empty
            level = div.get("level", "").strip()
            if level and level not in competition_levels:
                competition_levels.append(level)

        # Convert the set to a sorted list so the columns fill in an orderly fashion.
        # For example, 8u, 9u, 10u, ...
        age_groups_list = sorted(age_groups_set, key=lambda x: int(x.replace("u", "")))

        data = {
            "Tournament-name": tournament_name,
            "Start-date": start_date,
            "End-date": end_date,
            "Are-there-multiple_locations": are_multiple_venues,
            "Address": venue_info.get("Address", ""),
            "Address-2": "",
            "Venue": "Multiple Locations" if are_multiple_venues == "Yes" else venue_info.get("Venue Name", ""),
            "City": venue_info.get("City", ""),
            "State-FullName": venue_info.get("State", ""),
            "Zip": venue_info.get("Postal Code", ""),
            "Guaranteed-games": game_guarantee,
            "Gate-fees": "NA",
            "Hotel-required": "NA",
            "Tournament-type": tournament_type,
            "Tournament-surface": "NA",
            "Contact-name": contact_name,
            "Phone-number": phone_number,
            "Email": email,
            "Website-url": website_url,
            "More-information-and-additional-tournament-locations": ""
        }

        # Fill in age groups (up to 10).
        for i in range(1, 11):
            data[f"Age-groups.{i}"] = age_groups_list[i - 1] if i - 1 < len(age_groups_list) else ""

        # Fill in competition levels (up to 10).
        for i in range(1, 11):
            data[f"Competition-level.{i}"] = competition_levels[i - 1] if i - 1 < len(competition_levels) else ""

        data["Select All Competition Levels That Apply."] = ""

        # Ensure all output columns exist.
        for col in OUTPUT_COLUMNS:
            if col not in data:
                data[col] = ""

        return data

    def get_event_key(self, url: str) -> str:
        """
        Given a Five Tool event URL, return a unique key that identifies the event.
        Typically, the URL format is: https://events.fivetool.org/events/<slug>
        """
        match = re.search(r'/events/([^/]+)/?$', url)
        if match:
            return match.group(1)
        return url

    def extract_venue_info(self, event: Dict) -> Dict:
        """
        Extract venue details with fallback logic.
        Primary: Use the first venue in the "venues" array.
        Fallback: Use the top-level "city", "state", and "display_location" fields.
        """
        if event.get("venues") and len(event["venues"]) > 0:
            venue = event["venues"][0]
            address = venue.get("address1", "")
            city = venue.get("city", "")
            state_val = venue.get("state", "")
            postal_code = venue.get("postal_code", "")
            venue_name = venue.get("name", "")
        else:
            address = ""
            city = event.get("city", "")
            state_val = event.get("state", "")
            postal_code = ""
            venue_name = event.get("display_location", "")

        if state_val:
            state_obj = states.lookup(state_val)
            state_full = state_obj.name if state_obj else state_val
        else:
            state_full = ""

        return {
            "Venue Name": venue_name,
            "Address": address,
            "City": city,
            "State": state_full,
            "Postal Code": postal_code
        }
