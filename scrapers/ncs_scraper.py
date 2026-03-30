# scrapers/ncs_scraper.py

import logging
import time
import re
from datetime import datetime
from typing import List, Dict, Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException
)

from common.config import OUTPUT_COLUMNS, VALID_LEVELS
from .base_scraper import BaseScraper

logger = logging.getLogger(__name__)


class NCSScraper(BaseScraper):
    """
    NCSScraper implements the BaseScraper interface for playncs.com.
    It collects event links and extracts event details from multiple sub-pages:
      - Divisions (/Divisions/...)
      - Locations (/Locations/...)
      - Contacts (/Contacts/...)
    """
    SCRAPER_NAME = "NCS"

    def collect_event_links(
        self,
        driver,
        base_url: str,
        wait_time: int = 15,
        pause: int = 2
    ) -> List[str]:
        """
        Collect event links by following the Next button until it's disabled.
        For each page:
          - Wait for ".media-body .h4 a" elements to appear, then parse their hrefs.
          - Look at <li class="next"> in <ul class="pagination">. If disabled, stop.

        Returns a list of absolute URLs to each event's /Details/ page.
        """
        event_links: List[str] = []
        current_url = base_url
        page_number = 1

        while True:
            logger.info(f"Scraping page {page_number}: {current_url}")

            try:
                driver.get(current_url)
                WebDriverWait(driver, wait_time).until(
                    EC.presence_of_all_elements_located((By.CSS_SELECTOR, ".media-body .h4 a"))
                )
            except TimeoutException:
                logger.warning(f"Timeout while loading page {page_number}. Stopping pagination.")
                break
            except WebDriverException as e:
                logger.error(f"WebDriverException on page {page_number}: {e}")
                break

            soup = BeautifulSoup(driver.page_source, "html.parser")
            anchors = soup.select(".media-body .h4 a")
            new_count = 0

            for a in anchors:
                href = a.get("href", "")
                if href.startswith("/"):
                    full_url = "https://playncs.com" + href
                else:
                    full_url = href

                # Normalize to canonical casing so downstream parsing/replacements work.
                full_url = self._canonicalize_event_url(full_url)

                if full_url not in event_links:
                    event_links.append(full_url)
                    new_count += 1

            logger.info(
                f"Found {new_count} new event links on page {page_number} "
                f"(total {len(event_links)})."
            )

            next_li = soup.select_one("ul.pagination li.next")
            if not next_li or "disabled" in next_li.get("class", []):
                logger.info("Next button missing or disabled. End of pagination.")
                break

            next_a = next_li.find("a")
            if not next_a:
                logger.info("No anchor in next li. End of pagination.")
                break

            next_href = next_a.get("href", "")
            next_href = self._preserve_base_query_params(next_href, base_url)
            if not next_href:
                logger.info("Next href empty. End of pagination.")
                break

            current_url = next_href
            page_number += 1
            time.sleep(pause)

        logger.info(f"Total event links collected: {len(event_links)}")
        return event_links

    def get_event_key(self, url: str) -> str:
        """
        Extract a unique event key (name) from an NCS URL.
        Prefer extracting a human-readable name from the URL path after /Details/<id>/.
        """
        url = self._canonicalize_event_url(url)

        match = re.search(r"/Details/\d+/([^/]+)/?$", url, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace("-", " ").title()

        match = re.search(r"/event/([^/]+)/?$", url, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace("-", " ").title()

        match = re.search(r"/events/([^/]+)/?$", url, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace("-", " ").title()

        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        event_id = qs.get("eventid", [None])[0]
        if event_id:
            return f"Event {event_id}"

        return "Unknown Event"

    def extract_event_details(
        self,
        url: str,
        driver,
        max_retries: int = 1,
        **kwargs
    ) -> Dict:
        """
        Extract details for a single NCS event from the 3 sub-pages:
          - /Divisions/ (Age groups & competition levels)
          - /Locations/ (Venue addresses)
          - /Contacts/  (Contact name, phone, email)

        Notes:
          - URL casing matters for our string replace logic, so we canonicalize URLs.
          - We wait for page-specific content selectors; soft-timeout returns best-effort HTML.
        """
        logger.info(f"Extracting event details from: {url}")
        retries = 0
        wait_time = kwargs.get("wait_time", 15)

        while retries < max_retries:
            try:
                details_url = self._force_details_url(url)

                # Build sub-page URLs using canonical /Details/ segment
                divisions_url = details_url.replace("/Details/", "/Divisions/")
                locations_url = details_url.replace("/Details/", "/Locations/")
                contacts_url = details_url.replace("/Details/", "/Contacts/")

                # 1) Details page
                details_soup = self._get_soup(
                    driver,
                    details_url,
                    wait_time=wait_time,
                    wait_css="h1.mv-sm",
                    soft_timeout=True
                )
                tournament_name, start_date, end_date = self._parse_details_page(details_soup)

                # 2) Divisions page
                divisions_soup = self._get_soup(
                    driver,
                    divisions_url,
                    wait_time=wait_time,
                    wait_css="div.division",
                    soft_timeout=True
                )
                age_groups, comp_levels = self._parse_divisions(divisions_soup, VALID_LEVELS)

                # 3) Locations page
                locations_soup = self._get_soup(
                    driver,
                    locations_url,
                    wait_time=wait_time,
                    wait_css="ul.list-sites",
                    soft_timeout=True
                )
                multiple_locs, venue, addr, city, state_full, zip_code = self._parse_locations(locations_soup)

                # 4) Contacts page
                contacts_soup = self._get_soup(
                    driver,
                    contacts_url,
                    wait_time=wait_time,
                    wait_css="ul.list-contacts",
                    soft_timeout=True
                )
                contact_name, phone_number, email = self._parse_contacts(contacts_soup)

                data = {
                    "Tournament-name": tournament_name,
                    "Start-date": start_date,
                    "End-date": end_date,
                    "Are-there-multiple_locations": multiple_locs,
                    "Address": addr,
                    "Address-2": "",
                    "Venue": venue,
                    "City": city,
                    "State-FullName": state_full,
                    "Zip": zip_code,
                    "Guaranteed-games": "",
                    "Gate-fees": "NA",
                    "Hotel-required": "NA",
                    "Tournament-type": "Team",
                    "Tournament-surface": "NA",
                    "Contact-name": contact_name,
                    "Phone-number": phone_number,
                    "Email": email,
                    "Website-url": details_url,
                    "More-information-and-additional-tournament-locations": ""
                }

                # Age groups & entry fees columns
                for i in range(1, 14):
                    data[f"Age-groups.{i}"] = age_groups[i - 1] if i - 1 < len(age_groups) else ""
                    data[f"Entry-fee.{i}"] = ""

                # Competition levels columns
                for i in range(1, 10):
                    data[f"Competition-level.{i}"] = comp_levels[i - 1] if i - 1 < len(comp_levels) else ""
                data["Select All Competition Levels That Apply."] = ""

                # Ensure all columns exist
                for col in OUTPUT_COLUMNS:
                    if col not in data:
                        data[col] = ""

                logger.debug(f"Extracted data from {details_url}: {data}")
                return data

            except (TimeoutException, NoSuchElementException) as e:
                logger.error(f"Timeout/NoSuchElement error at {url}: {e}")
                retries += 1
                time.sleep(3)
                continue
            except WebDriverException as e:
                logger.error(f"WebDriverException at {url}: {e}")
                retries += 1
                time.sleep(3)
                continue
            except Exception as e:
                logger.error(f"Unexpected error extracting details at {url}: {e}")
                return None

        logger.error(f"Failed to extract data for URL: {url} after {max_retries} retries.")
        return None

    # ----------------------------------------------------------
    #     HELPER METHODS
    # ----------------------------------------------------------

    def _preserve_base_query_params(self, next_url: str, base_url: str) -> str:
        """
        Ensure pagination URLs keep the same filter query params as base_url (e.g., state=AZ).

        PlayNCS pagination sometimes returns links like '/baseball/Events?page=2'
        which drop important filters such as 'state'. This merges any missing
        query params from base_url into next_url.
        """
        if not next_url:
            return next_url

        # Make absolute (PlayNCS uses relative hrefs often)
        absolute = next_url
        if not absolute.lower().startswith(("http://", "https://")):
            absolute = urljoin("https://playncs.com", next_url)

        n = urlparse(absolute)
        b = urlparse(base_url)

        nq = parse_qs(n.query, keep_blank_values=True)
        bq = parse_qs(b.query, keep_blank_values=True)

        changed = False
        # Carry forward any missing base filters (state, etc.)
        for k, v in bq.items():
            if k not in nq:
                nq[k] = v
                changed = True

        if not changed:
            return absolute

        new_query = urlencode(nq, doseq=True)
        return urlunparse((n.scheme, n.netloc, n.path, n.params, new_query, n.fragment))


    def _canonicalize_event_url(self, url: str) -> str:
        """
        Force the canonical casing for NCS event routes so our case-sensitive
        string operations (.replace('/Details/', ...)) work even if upstream
        lowercased URLs.
        """
        u = (url or "").strip()
        if not u:
            return u

        # Ensure /baseball/Events/
        u = re.sub(r"/baseball/events/", "/baseball/Events/", u, flags=re.IGNORECASE)

        # Ensure route segment casing under /Events/
        u = re.sub(r"/Events/details/", "/Events/Details/", u, flags=re.IGNORECASE)
        u = re.sub(r"/Events/divisions/", "/Events/Divisions/", u, flags=re.IGNORECASE)
        u = re.sub(r"/Events/locations/", "/Events/Locations/", u, flags=re.IGNORECASE)
        u = re.sub(r"/Events/contacts/", "/Events/Contacts/", u, flags=re.IGNORECASE)

        return u

    def _force_details_url(self, url: str) -> str:
        """
        If the user-provided link is /Divisions/..., /Locations/..., or /Contacts/...,
        convert it to /Details/... so we have a stable 'base' event page.
        Also canonicalizes URL casing.
        """
        new_url = self._canonicalize_event_url(url)

        if "/Divisions/" in new_url:
            new_url = new_url.replace("/Divisions/", "/Details/")
        if "/Locations/" in new_url:
            new_url = new_url.replace("/Locations/", "/Details/")
        if "/Contacts/" in new_url:
            new_url = new_url.replace("/Contacts/", "/Details/")

        return new_url

    def _get_soup(
        self,
        driver,
        page_url: str,
        wait_time: int = 15,
        wait_css: Optional[str] = None,
        soft_timeout: bool = True
    ) -> BeautifulSoup:
        """
        Load page_url in driver and return BeautifulSoup of page_source.

        If wait_css is provided, wait for that selector to appear.
        If soft_timeout=True, a TimeoutException is logged and we return best-effort HTML.
        """
        page_url = self._canonicalize_event_url(page_url)
        driver.get(page_url)

        try:
            if wait_css:
                WebDriverWait(driver, wait_time).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_css))
                )
            else:
                WebDriverWait(driver, wait_time).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
        except TimeoutException:
            if soft_timeout:
                logger.warning(f"Timed out waiting for selector '{wait_css}' on {page_url}. Continuing with best-effort HTML.")
            else:
                raise

        return BeautifulSoup(driver.page_source, "html.parser")

    def _parse_details_page(self, soup: BeautifulSoup):
        """
        Parse the main details page for:
          - Tournament name
          - Date range -> start_date/end_date

        If no year is present, default to current year.
        """
        tournament_name = "N/A"
        start_date = ""
        end_date = ""

        h1_tag = soup.select_one("h1.mv-sm strong")
        if not h1_tag:
            h1_tag = soup.select_one("h1.mv-sm")
        if h1_tag:
            tournament_name = h1_tag.get_text(strip=True)

        date_tags = soup.select("hgroup > h3.inline-block.m0")
        raw = date_tags[-1].get_text(strip=True) if date_tags else ""

        if raw:
            raw_clean = raw.replace(",", " ").strip()

            year_match = re.search(r"\b(20\d{2})\b", raw_clean)
            year = year_match.group(1) if year_match else str(datetime.now().year)

            raw_no_year = re.sub(r"\b20\d{2}\b", "", raw_clean).strip()
            raw_no_year = re.sub(r"\s+", " ", raw_no_year).strip()

            m = re.match(r"([A-Za-z]+)\s+(\d+)-(\d+)$", raw_no_year)
            if m:
                month = m.group(1)
                day1 = m.group(2)
                day2 = m.group(3)
                start_date = f"{month} {day1} {year}"
                end_date = f"{month} {day2} {year}"
            elif " - " in raw_no_year:
                parts = raw_no_year.split(" - ", 1)
                if len(parts) == 2:
                    start_date = parts[0].strip() + f" {year}"
                    end_date = parts[1].strip() + f" {year}"
            elif "-" in raw_no_year:
                parts = raw_no_year.split("-", 1)
                if len(parts) == 2:
                    start_date = parts[0].strip() + f" {year}"
                    end_date = parts[1].strip() + f" {year}"
            else:
                start_date = raw_no_year.strip() + f" {year}"
                end_date = raw_no_year.strip() + f" {year}"
        else:
            start_date = "N/A"
            end_date = "N/A"

        return tournament_name, start_date, end_date

    def _parse_divisions(self, soup: BeautifulSoup, valid_levels: List[str]):
        """
        Return (age_groups, comp_levels).

        For each <div class="division">, parse:
          - Age group: leading digits + 'U' (e.g. 10U)
          - Competition level: words matching VALID_LEVELS
        """
        age_groups: List[str] = []
        comp_levels: List[str] = []
        valid_levels_upper = {lvl.upper() for lvl in valid_levels}

        for div in soup.select("div.division"):
            txt = div.get_text(strip=True).upper()
            age_match = re.match(r"^(\d+U)", txt)
            if age_match:
                age_groups.append(age_match.group(1))

            for w in txt.split():
                if w in valid_levels_upper:
                    nice = w.capitalize()
                    if nice not in comp_levels:
                        comp_levels.append(nice)

        return age_groups, comp_levels

    def _parse_locations(self, soup: BeautifulSoup):
        """
        Return (multiple_locs, venue, address_line1, city, state_full, zip_code).

        Prefer stable data attributes:
          - data-line1
          - data-line2  (e.g. "Tucson, AZ 85743")
          - data-state
        Fallback to parsing <address> <div> lines if needed.
        """
        site_items = [li for li in soup.select("ul.list-sites li") if li.get("data-title", "").strip()]

        multiple_locs = "Yes" if len(site_items) > 1 else "No"

        venue = "N/A"
        address_line1 = "N/A"
        city = "N/A"
        state_full = "N/A"
        zip_code = "N/A"

        if site_items:
            first_li = site_items[0]

            h5_venue = first_li.select_one("h5.title")
            if h5_venue:
                venue = h5_venue.get_text(strip=True)

            # Prefer data attributes
            line1 = (first_li.get("data-line1") or "").strip()
            line2 = (first_li.get("data-line2") or "").strip()
            state_attr = (first_li.get("data-state") or "").strip()

            if line1:
                address_line1 = line1

            if line2:
                m = re.match(r"^(.*),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", line2)
                if m:
                    city = m.group(1).strip()
                    state_full = m.group(2).strip()
                    zip_code = m.group(3).strip()

            # Fallback: <address> parsing if zip not found yet
            if zip_code == "N/A":
                addr_tag = first_li.select_one("address")
                if addr_tag:
                    lines = addr_tag.select("div")
                    if len(lines) >= 2:
                        maybe_addr1 = lines[0].get_text(strip=True)
                        maybe_city_state_zip = lines[1].get_text(strip=True)

                        if maybe_addr1:
                            address_line1 = maybe_addr1

                        m = re.match(r"^(.*),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", maybe_city_state_zip)
                        if m:
                            city = m.group(1).strip()
                            state_full = m.group(2).strip()
                            zip_code = m.group(3).strip()

            if state_full == "N/A" and state_attr:
                state_full = state_attr

        # Only override venue label if there are truly multiple locations
        if len(site_items) > 1:
            venue = "Multiple Locations"

        return multiple_locs, venue, address_line1, city, state_full, zip_code

    def _parse_contacts(self, soup: BeautifulSoup):
        """
        Return (contact_name, phone_number, email).

        Parse first <ul class="list-contacts"><li> entry.
        """
        contact_name = "N/A"
        phone_number = "N/A"
        email = "N/A"

        list_items = soup.select("ul.list-contacts li")
        if list_items:
            first_li = list_items[0]

            h5_tag = first_li.select_one("h5.title")
            if h5_tag:
                contact_name = h5_tag.get_text(strip=True)

            addr_tag = first_li.select_one("address")
            if addr_tag:
                mail_link = addr_tag.select_one("a[href^='mailto:']")
                if mail_link:
                    email = mail_link.get("href", "").replace("mailto:", "").strip()

                phone_link = addr_tag.select_one("a[href^='tel:']")
                if phone_link:
                    raw_phone = phone_link.get("href", "").replace("tel:", "").strip()
                    digits = re.sub(r"\D", "", raw_phone)
                    if len(digits) == 10:
                        phone_number = re.sub(r"(\d{3})(\d{3})(\d{4})", r"\1-\2-\3", digits)
                    else:
                        phone_number = raw_phone

        return contact_name, phone_number, email
