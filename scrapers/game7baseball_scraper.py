import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlencode, urlparse, urlunparse

import requests
import us
from bs4 import BeautifulSoup

from common.config import OUTPUT_COLUMNS
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)

BASE = "https://www.game7baseball.com"

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _http_get(url: str, timeout: int = 30) -> str:
    resp = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text


def _soup(url: str) -> BeautifulSoup:
    return BeautifulSoup(_http_get(url), "html.parser")


def _mmddyyyy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _state_abbr_from_base_url(base_url: str) -> str:
    qs = parse_qs(urlparse(base_url).query)
    return (qs.get("state", [""])[0] or "").strip().upper()


def _state_full_name(abbr_or_name: str) -> str:
    s = (abbr_or_name or "").strip()
    if not s:
        return ""
    st = us.states.lookup(s)
    return st.name if st else s


def _parse_date_range(raw: str, year: int) -> Tuple[Optional[date], Optional[date]]:
    s = " ".join((raw or "").split()).strip()
    if not s:
        return None, None

    s = s.replace("\u2013", "-").replace("\u2014", "-")

    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2})$", s)
    if m:
        m1 = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        m2 = MONTHS.get(m.group(3).lower()[:3])
        d2 = int(m.group(4))
        if m1 and m2:
            return date(year, m1, d1), date(year, m2, d2)

    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})$", s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        d2 = int(m.group(3))
        if mon:
            return date(year, mon, d1), date(year, mon, d2)

    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        if mon:
            d = date(year, mon, d1)
            return d, d

    return None, None


def _preserve_base_query_params(next_url: str, base_url: str) -> str:
    if not next_url:
        return next_url

    absolute = next_url
    if not absolute.lower().startswith(("http://", "https://")):
        absolute = urljoin(BASE, next_url)

    n = urlparse(absolute)
    b = urlparse(base_url)
    nq = parse_qs(n.query, keep_blank_values=True)
    bq = parse_qs(b.query, keep_blank_values=True)

    changed = False
    for k, v in bq.items():
        if k not in nq:
            nq[k] = v
            changed = True

    if not changed:
        return absolute

    new_query = urlencode(nq, doseq=True)
    return urlunparse((n.scheme, n.netloc, n.path, n.params, new_query, n.fragment))


def _extract_age_tokens(text: str) -> List[str]:
    out: List[str] = []
    seen = set()
    for match in re.finditer(r"\b(\d{1,2})\s*U\b", text or "", flags=re.IGNORECASE):
        token = f"{int(match.group(1))}u"
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return re.sub(r"(\d{3})(\d{3})(\d{4})", r"\1-\2-\3", digits)
    return (raw or "").strip()


def _extract_guaranteed_games(text: str) -> str:
    match = re.search(r"\b(\d+)\s*game\s+guarantee\b", text or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _extract_comp_levels(*texts: str) -> List[str]:
    levels: List[str] = []

    def add(level: str) -> None:
        if level not in levels:
            levels.append(level)

    direct_lines: List[str] = []
    for text in texts:
        if not text:
            continue
        direct_lines.extend(part.strip() for part in text.split("|") if part.strip())

    for raw_line in direct_lines:
        upper = re.sub(r"\s+", " ", raw_line.upper()).strip()
        upper = upper.replace("Â", " ")

        # Skip narrative lines that mention teams playing up or other prose.
        if "PLAY UP" in upper or "TEAMS WELCOME" in upper:
            continue

        if re.search(r"\bREC\s*,\s*A\s*,\s*AA\s+TEAMS\s+ONLY\b", upper):
            add("Rec")
            add("A")
            add("AA")
            continue

        if re.search(r"\bSINGLE\s+A\b", upper):
            add("A")
            continue

        if re.search(r"\bA\s*/\s*AA\s*(?:&|AND)\s*OPEN\b", upper):
            add("A")
            add("AA")
            add("Open")
            continue

        if re.search(r"\bAA\s*(?:&|AND)\s*AAA\s*/\s*OPEN\b", upper):
            add("AA")
            add("AAA")
            add("Open")
            continue

        if re.search(r"\bAA\s*(?:&|AND)\s*OPEN\b", upper):
            add("AA")
            add("Open")
            continue

        if re.search(r"\bA\s*/\s*AA\b", upper) or re.search(r"\bAA\s*/\s*A\b", upper):
            add("A")
            add("AA")
            continue

        if re.search(r"\bAAA\s*/\s*OPEN\b", upper):
            add("AAA")
            add("Open")
            continue

        if re.search(r"\bOPEN\s+DIVISION(?:S)?\b", upper) or re.search(r"\b\d{1,2}U\s+OPEN\s+DIVISION\b", upper):
            add("Open")
            continue

    return levels


def _is_placeholder_city(value: str) -> bool:
    cleaned = re.sub(r"[^a-z]", "", (value or "").strip().lower())
    return cleaned in {"middle", "midtn", "midt n", "midtennessee"}


def _parse_maps_query_city_state_zip(href: str) -> Tuple[str, str, str]:
    if not href:
        return "", "", ""
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    q = (query.get("q") or [""])[0]
    q = q.replace("USA", "").strip(" ,")
    match = re.search(r"([^,]+),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", q)
    if not match:
        return "", "", ""
    return match.group(1).strip(), match.group(2).strip(), match.group(3).strip()


def _contacts_url(details_url: str) -> str:
    match = re.search(r"/Events/Details/(\d+)(/[^/?#]*)?", details_url, flags=re.IGNORECASE)
    if not match:
        return ""
    slug = match.group(2) or ""
    return f"{BASE}/baseball/Events/Contacts/{match.group(1)}{slug}"


class Game7BaseballScraper(BaseScraper):
    SCRAPER_NAME = "Game7Baseball"
    REQUIRES_DRIVER = False
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"

    def __init__(self):
        self.listing_meta: Dict[str, Dict[str, Any]] = {}

    def _page_has_no_events(self, soup: BeautifulSoup) -> bool:
        text = soup.get_text(" ", strip=True).lower()
        return "no events found" in text

    def _listing_items(self, soup: BeautifulSoup) -> List[BeautifulSoup]:
        return soup.select("div.media-list.media-list-events > div.media")

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        state_abbr = _state_abbr_from_base_url(base_url)
        page = 1
        out: List[str] = []
        seen = set()
        self.listing_meta = {}

        while True:
            url = base_url if page == 1 else f"{base_url}&page={page}" if "?" in base_url else f"{base_url}?page={page}"
            soup = _soup(url)

            if self._page_has_no_events(soup):
                logger.info("[Game7Baseball] No events found for state=%s page=%s", state_abbr, page)
                break

            items = self._listing_items(soup)
            if not items:
                logger.info("[Game7Baseball] No event cards found for state=%s page=%s", state_abbr, page)
                break

            new_links = 0
            for media in items:
                body = media.select_one("div.media-body")
                if not body:
                    continue

                title_link = body.select_one("div.h4 a[href*='/baseball/Events/Details/']")
                if not title_link:
                    continue

                full = urljoin(BASE, title_link.get("href", "").strip())
                if not full or full in seen:
                    continue

                info_h6 = body.select("div.h6")
                location_text = info_h6[0].get_text(" ", strip=True) if info_h6 else ""
                age_text = info_h6[-1].get_text(" ", strip=True) if len(info_h6) > 1 else ""
                date_el = body.select("div.h4")
                date_text = date_el[1].get_text(" ", strip=True) if len(date_el) > 1 else ""
                tags = [span.get_text(" ", strip=True) for span in body.select("ul.tags span.label")]

                location_main = ""
                venue_hint = ""
                if "|" in location_text:
                    left, right = location_text.split("|", 1)
                    location_main = left.strip()
                    venue_hint = right.strip()
                else:
                    location_main = location_text.strip()

                self.listing_meta[full] = {
                    "state": state_abbr,
                    "location_main": location_main,
                    "venue_hint": venue_hint,
                    "listing_date_raw": date_text,
                    "listing_age_groups": _extract_age_tokens(age_text),
                    "tags": tags,
                }
                seen.add(full)
                out.append(full)
                new_links += 1

            logger.info("[Game7Baseball] state=%s page=%s new=%s total=%s", state_abbr, page, new_links, len(out))

            next_li = soup.select_one("ul.pagination li.next")
            if not next_li or "disabled" in (next_li.get("class") or []):
                break
            next_a = next_li.find("a", href=True)
            if not next_a:
                break
            next_href = _preserve_base_query_params(next_a.get("href", ""), base_url)
            if not next_href:
                break

            parsed = urlparse(next_href)
            qs = parse_qs(parsed.query)
            try:
                page = int((qs.get("page") or [page + 1])[0])
            except Exception:
                page += 1

        logger.info("[Game7Baseball] Collected %d events for state=%s", len(out), state_abbr)
        return out

    def _detail_urls(self, details_url: str) -> Tuple[str, str, str]:
        return (
            details_url,
            re.sub(r"/Events/Details/", "/Events/Divisions/", details_url, flags=re.IGNORECASE),
            re.sub(r"/Events/Details/", "/Events/Locations/", details_url, flags=re.IGNORECASE),
        )

    def _parse_details_page(self, soup: BeautifulSoup, selected_state_abbr: str) -> Dict[str, str]:
        tournament_name = ""
        start_date = ""
        end_date = ""
        city = ""
        state_full = _state_full_name(selected_state_abbr)
        venue_hint = ""
        contact_name = ""
        phone = ""
        email = ""
        notes = ""
        tournament_profile = ""
        event_info_bullets = ""

        hgroup = soup.select_one("hgroup")
        if hgroup:
            h1 = hgroup.select_one("h1")
            if h1:
                tournament_name = h1.get_text(" ", strip=True)

            h3_texts = [h.get_text(" ", strip=True) for h in hgroup.select("h3")]
            if h3_texts:
                location_line = h3_texts[0]
                if "," in location_line:
                    city_part, state_part = location_line.rsplit(",", 1)
                    city = city_part.strip()
                    state_full = _state_full_name(state_part.strip())
                if len(h3_texts) > 1 and h3_texts[1].strip("| ").strip():
                    venue_hint = h3_texts[1].strip("| ").strip()
                if h3_texts:
                    date_raw = h3_texts[-1].strip()
                    year = date.today().year
                    start_d, end_d = _parse_date_range(date_raw, year)
                    if start_d:
                        start_date = _mmddyyyy(start_d)
                    if end_d:
                        end_date = _mmddyyyy(end_d)

        details_body = soup.select_one("#event-details-body")
        if details_body:
            unlabeled_lists = []
            for ul in details_body.find_all("ul", recursive=True):
                prev_p = ul.find_previous_sibling("p")
                prev_label = prev_p.get_text(" ", strip=True).lower() if prev_p else ""
                items = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
                if not items:
                    continue
                if "tournament contact" in prev_label:
                    continue
                if "tournament profile" in prev_label:
                    continue
                if "tournament fee" in prev_label:
                    continue
                if "gate fee" in prev_label:
                    continue
                unlabeled_lists.extend(items)
            if unlabeled_lists:
                event_info_bullets = " | ".join(item for item in unlabeled_lists if item)

        for p in soup.find_all("p"):
            label = p.get_text(" ", strip=True).lower()
            if "tournament profile" in label:
                ul = p.find_next_sibling("ul")
                if ul:
                    profile_parts = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
                    tournament_profile = " | ".join(part for part in profile_parts if part)
                continue

            if "tournament contact" in label:
                ul = p.find_next_sibling("ul")
                if ul:
                    items = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
                    if items:
                        contact_name = items[0].strip()
                    for item in items[1:]:
                        if "email" in item.lower():
                            em = re.search(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", item)
                            if em:
                                email = em.group(1)
                        elif "cell" in item.lower() or "phone" in item.lower():
                            phone = _normalize_phone(item)
                continue

            txt = p.get_text(" ", strip=True)
            if "tournament format" in txt.lower():
                ul = p.find_next_sibling("ul")
                if ul:
                    notes_parts = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
                    notes = " | ".join(part for part in notes_parts if part)
                    break

        return {
            "Tournament-name": tournament_name,
            "Start-date": start_date,
            "End-date": end_date,
            "City": city,
            "State-FullName": state_full,
            "VenueHint": venue_hint,
            "Contact-name": contact_name,
            "Phone-number": phone,
            "Email": email,
            "Notes": notes,
            "TournamentProfile": tournament_profile,
            "EventInfoBullets": event_info_bullets,
        }

    def _parse_contacts_page(self, soup: BeautifulSoup) -> Dict[str, str]:
        contact_name = ""
        phone = ""
        email = ""

        for item in soup.select("ul.list-contacts > li"):
            candidate_name = (item.select_one("h5.title") or item.select_one(".title"))
            candidate_name_text = candidate_name.get_text(" ", strip=True) if candidate_name else ""

            mailto = item.select_one('a[href^="mailto:"]')
            tel = item.select_one('a[href^="tel:"]')
            candidate_email = ""
            candidate_phone = ""

            if mailto:
                href = mailto.get("href", "")
                candidate_email = href.replace("mailto:", "", 1).strip() or mailto.get_text(" ", strip=True)
            if tel:
                candidate_phone = _normalize_phone(tel.get_text(" ", strip=True) or tel.get("href", "").replace("tel:", "", 1))

            if candidate_email or candidate_phone:
                contact_name = candidate_name_text
                email = candidate_email
                phone = candidate_phone
                break

        return {
            "Contact-name": contact_name,
            "Phone-number": phone,
            "Email": email,
        }

    def _parse_divisions_page(self, soup: BeautifulSoup) -> Dict[str, Any]:
        age_groups: List[str] = []
        entry_fees: Dict[str, str] = {}
        gate_fees: List[str] = []
        venue_names: List[str] = []

        for panel in soup.select("#divisionsContainer div.panel"):
            division_el = panel.select_one("div.division")
            if not division_el:
                continue

            division_text = division_el.get_text(" ", strip=True)
            ages = _extract_age_tokens(division_text)
            if not ages:
                continue
            age = ages[0]
            if age not in age_groups:
                age_groups.append(age)

            amt_el = panel.select_one("div.amount")
            if amt_el:
                amt_text = amt_el.get_text(" ", strip=True)
                amt_match = re.search(r"\$([0-9][0-9,]*)", amt_text)
                if amt_match:
                    entry_fees[age] = amt_match.group(1).replace(",", "")

            for small in panel.select("div.locations div.small.text-muted"):
                small_text = small.get_text(" ", strip=True)
                if "gate fee" in small_text.lower() and small_text not in gate_fees:
                    gate_fees.append(small_text)

            for li in panel.select("div.locations ul li"):
                venue_text = li.get_text(" ", strip=True)
                if venue_text and venue_text not in venue_names:
                    venue_names.append(venue_text)

        return {
            "AgeGroups": age_groups,
            "EntryFees": entry_fees,
            "GateFees": gate_fees,
            "VenueNames": venue_names,
        }

    def _parse_locations_page(self, soup: BeautifulSoup, selected_state_abbr: str) -> Dict[str, Any]:
        items = soup.select("ul.list-sites li[data-title]")
        parsed_locations: List[Dict[str, str]] = []

        for li in items:
            title = (li.get("data-title") or "").strip()
            line1 = (li.get("data-line1") or "").strip()
            line2 = (li.get("data-line2") or "").strip()
            state_abbr = (li.get("data-state") or selected_state_abbr).strip().upper()

            city = ""
            zip_code = ""
            m = re.match(r"^(.*?),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", line2)
            if m:
                city = m.group(1).strip()
                zip_code = m.group(3).strip()

            maps_city = ""
            maps_state_abbr = ""
            maps_zip = ""
            directions_link = li.select_one("a[href*='maps.google.com']")
            if directions_link:
                maps_city, maps_state_abbr, maps_zip = _parse_maps_query_city_state_zip(
                    directions_link.get("href", "")
                )

            if _is_placeholder_city(city) and maps_city:
                city = maps_city
            if maps_zip and not zip_code:
                zip_code = maps_zip
            if maps_state_abbr:
                state_abbr = maps_state_abbr

            parsed_locations.append(
                {
                    "Venue": title,
                    "Address": line1,
                    "City": city,
                    "State-FullName": _state_full_name(state_abbr),
                    "Zip": zip_code,
                }
            )

        non_tbd = [loc for loc in parsed_locations if loc.get("Venue", "").strip().lower() != "tbd"]
        primary = non_tbd[0] if non_tbd else (parsed_locations[0] if parsed_locations else {})
        multiple = "Yes" if len(non_tbd or parsed_locations) > 1 else "No"

        extras = []
        for loc in non_tbd[1:] if non_tbd else parsed_locations[1:]:
            parts = [
                loc.get("Venue", "").strip(),
                loc.get("Address", "").strip(),
                f"{loc.get('City', '').strip()}, {loc.get('State-FullName', '').strip()} {loc.get('Zip', '').strip()}".strip(),
            ]
            extras.append(" - ".join(part for part in parts if part))

        return {
            "MultipleLocations": multiple,
            "Primary": primary,
            "Extras": extras,
        }

    def extract_event_details(self, url: str, driver, **kwargs) -> Dict:
        selected_state_abbr = (self.listing_meta.get(url, {}).get("state") or "").strip().upper()
        details_url, divisions_url, locations_url = self._detail_urls(url)

        details = self._parse_details_page(_soup(details_url), selected_state_abbr)
        contacts = {"Contact-name": "", "Phone-number": "", "Email": ""}
        contacts_url = _contacts_url(details_url)
        if contacts_url:
            try:
                contacts = self._parse_contacts_page(_soup(contacts_url))
            except Exception:
                contacts = {"Contact-name": "", "Phone-number": "", "Email": ""}
        divisions = self._parse_divisions_page(_soup(divisions_url))
        locations = self._parse_locations_page(_soup(locations_url), selected_state_abbr)
        listing = self.listing_meta.get(url, {})

        row = {col: "" for col in OUTPUT_COLUMNS}

        primary = locations.get("Primary", {})
        venue = (
            primary.get("Venue", "")
            or details.get("VenueHint", "")
            or listing.get("venue_hint", "")
        )

        row["Tournament-name"] = details.get("Tournament-name", "")
        row["Start-date"] = details.get("Start-date", "")
        row["End-date"] = details.get("End-date", "")
        row["Are-there-multiple_locations"] = locations.get("MultipleLocations", "No")
        row["Venue"] = venue
        row["Address"] = primary.get("Address", "")
        row["Address-2"] = ""
        row["City"] = primary.get("City", "") or details.get("City", "")
        row["State-FullName"] = primary.get("State-FullName", "") or details.get("State-FullName", "")
        row["Zip"] = primary.get("Zip", "")

        gate_fee = divisions.get("GateFees", [])
        row["Gate-fees"] = "Yes" if gate_fee else "NA"
        row["Hotel-required"] = "Yes" if any("stay to play" in tag.lower() and "no stay to play" not in tag.lower() for tag in listing.get("tags", [])) else "NA"
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "Turf" if any("turf" in tag.lower() for tag in listing.get("tags", [])) else "NA"
        row["Guaranteed-games"] = _extract_guaranteed_games(details.get("Notes", ""))
        row["Contact-name"] = contacts.get("Contact-name", "") or details.get("Contact-name", "")
        row["Phone-number"] = contacts.get("Phone-number", "") or details.get("Phone-number", "")
        row["Email"] = contacts.get("Email", "") or details.get("Email", "")
        row["Website-url"] = details_url

        more_info_parts = []
        extras = locations.get("Extras", [])
        if extras:
            more_info_parts.append("; ".join(extras))
        if details.get("Notes", ""):
            more_info_parts.append(details["Notes"])
        row["More-information-and-additional-tournament-locations"] = " | ".join(part for part in more_info_parts if part)

        age_groups = divisions.get("AgeGroups") or listing.get("listing_age_groups") or []
        entry_fees = divisions.get("EntryFees") or {}
        for idx, age in enumerate(age_groups[:13], start=1):
            row[f"Age-groups.{idx}"] = age
            row[f"Entry-fee.{idx}"] = entry_fees.get(age, "")

        comp_levels = _extract_comp_levels(
            details.get("TournamentProfile", ""),
            details.get("EventInfoBullets", ""),
            details.get("Tournament-name", ""),
        )
        for i in range(1, 10):
            row[f"Competition-level.{i}"] = comp_levels[i - 1] if i - 1 < len(comp_levels) else ""
        row["Select All Competition Levels That Apply."] = ""

        return row

    def get_event_key(self, url: str) -> str:
        return url
