# scrapers/grandslam_scraper.py

import logging
import re
import requests
from bs4 import BeautifulSoup
from datetime import date
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs, urljoin

import us

from scrapers.base_scraper import BaseScraper
from common.config import OUTPUT_COLUMNS

logger = logging.getLogger(__name__)

BASE = "https://www.grandslamtournaments.com"

# bump this if you ever need to prove the right file is loaded
GS_SCRAPER_VERSION = "v5.0 (details-page dates + badges ages/divisions + divisions fees + state fallback + case-insensitive urls)"

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
    r = requests.get(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.text


def _soup(url: str) -> BeautifulSoup:
    return BeautifulSoup(_http_get(url), "html.parser")


def _mmddyyyy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def _parse_state_from_carrier(base_url: str) -> str:
    qs = parse_qs(urlparse(base_url).query)
    return (qs.get("state", [""])[0] or "").strip().upper()


def _state_full_name(abbr_or_name: str) -> str:
    s = (abbr_or_name or "").strip()
    if not s:
        return ""
    st = us.states.lookup(s)
    return st.name if st else s


def _extract_guaranteed_games(text: str) -> str:
    """
    Return numeric string ONLY if explicitly present (e.g. '4 game guarantee').
    Otherwise return ''.
    """
    m = re.search(r"\b(\d+)\s*[- ]?\s*game\s+guarantee\b", text, flags=re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_year_from_text(text: str) -> Optional[int]:
    m = re.search(r"\b(20\d{2})\b", text)
    return int(m.group(1)) if m else None


def _parse_date_range(raw: str, year: int) -> Tuple[Optional[date], Optional[date]]:
    """
    Handles:
      "Feb 21-22"
      "Feb 28 - Mar 1"
      "Mar 7-8"
      "May 9"
      "Jul 20-26"
    """
    s = " ".join((raw or "").split()).strip()
    if not s:
        return None, None

    s = s.replace("\u2013", "-").replace("\u2014", "-")

    # "Feb 28 - Mar 1"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2})$", s)
    if m:
        m1 = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        m2 = MONTHS.get(m.group(3).lower()[:3])
        d2 = int(m.group(4))
        if m1 and m2:
            return date(year, m1, d1), date(year, m2, d2)

    # "Feb 21-22" OR "Jul 20-26"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})$", s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        d2 = int(m.group(3))
        if mon:
            return date(year, mon, d1), date(year, mon, d2)

    # "May 9"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", s)
    if m:
        mon = MONTHS.get(m.group(1).lower()[:3])
        d1 = int(m.group(2))
        if mon:
            d = date(year, mon, d1)
            return d, d

    return None, None


def _is_next_disabled(soup: BeautifulSoup) -> bool:
    ul = soup.select_one("ul.pagination")
    if not ul:
        return True
    lis = ul.find_all("li", recursive=False)
    if not lis:
        return True
    last_li = lis[-1]
    classes = last_li.get("class") or []
    return "disabled" in classes


def _derive_gs_related_urls(details_url: str) -> Tuple[str, str, str, str]:
    """
    Works for /Events/Details/ and /events/details/ etc.
    Returns (details_url, locations_url, contacts_url, divisions_url)
    """
    u = details_url
    locations_url = re.sub(r"/events/details/", "/events/locations/", u, flags=re.IGNORECASE)
    contacts_url = re.sub(r"/events/details/", "/events/contacts/", u, flags=re.IGNORECASE)
    divisions_url = re.sub(r"/events/details/", "/events/divisions/", u, flags=re.IGNORECASE)
    return u, locations_url, contacts_url, divisions_url


def _find_badges_container_from_eventinfo_anchor(event_info_a):
    node = event_info_a
    for _ in range(15):
        if not node or not getattr(node, "parent", None):
            break
        node = node.parent
        if getattr(node, "select_one", None):
            b = node.select_one("div.badges")
            if b is not None:
                return b
    return None


def _extract_ages_and_divisions_from_badges(badges_div):
    ages: List[str] = []
    divs: List[str] = []

    if not badges_div:
        return ages, divs

    for btn in badges_div.select("div.ages button.badge"):
        t = btn.get_text(" ", strip=True)
        if not t:
            continue
        m = re.match(r"^(\d{1,2})\s*U$", t.strip(), flags=re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if 4 <= n <= 20:
                ages.append(f"{n}u")

    for btn in badges_div.select("div.divisions button.badge"):
        t = btn.get_text(" ", strip=True)
        if not t:
            continue
        tl = re.sub(r"\s+", " ", t.strip().lower())
        if tl == "open":
            divs.append("Open")
        elif tl == "aa":
            divs.append("AA")
        elif tl == "aaa":
            divs.append("AAA")
        elif tl == "a":
            divs.append("A")
        elif tl == "all star":
            divs.append("All Star")
        else:
            divs.append(t.strip())

    def dedupe(seq: List[str]) -> List[str]:
        out: List[str] = []
        seen = set()
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return dedupe(ages), dedupe(divs)


def _normalize_age_token(s: str) -> str:
    """
    Convert '7U', '13U 90', '8U CP' -> '7u', '13u', '8u'
    """
    t = (s or "").strip()
    m = re.search(r"(\d{1,2})\s*U", t, flags=re.IGNORECASE)
    if not m:
        return ""
    return f"{int(m.group(1))}u"


def _extract_division_fees_from_divisions_page(html_text: str) -> Dict[str, str]:
    """
    Parses:
      <div class="division">7U-OPEN</div>
      <div class="amount">$1,295</div>

    Fixes comma amounts like $1,295 (previously parsed as '1').
    """
    soup = BeautifulSoup(html_text, "html.parser")
    fees: Dict[str, str] = {}

    for heading in soup.select("div.panel .panel-heading"):
        div_el = heading.select_one("div.division")
        amt_el = heading.select_one("div.amount")
        if not div_el or not amt_el:
            continue

        age_raw = div_el.get_text(" ", strip=True)
        age_key = _normalize_age_token(age_raw)
        if not age_key:
            continue

        amt_raw = amt_el.get_text(" ", strip=True)
        if "$" not in amt_raw:
            continue

        m = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]{2})?)", amt_raw)
        if not m:
            continue

        val_str = m.group(1).replace(",", "").strip()

        try:
            val = float(val_str)
        except ValueError:
            continue

        if val < 10:
            continue

        val_out = str(int(val)) if val.is_integer() else str(val)

        if age_key not in fees:
            fees[age_key] = val_out

    return fees


def _extract_locations_from_locations_page(html_text: str) -> List[Dict[str, str]]:
    """
    Best-effort parsing:
    - Find lines like "City, ST 12345"
    - Use previous line as street
    - Use previous previous as venue name
    """
    soup = BeautifulSoup(html_text, "html.parser")
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n")]
    lines = [ln for ln in lines if ln]

    locs: List[Dict[str, str]] = []
    for i, ln in enumerate(lines):
        m = re.search(r"\b([^,]+),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?\b", ln)
        if not m:
            continue

        city = m.group(1).strip()
        st = m.group(2).strip()
        z = m.group(3).strip()

        street = lines[i - 1].strip() if i - 1 >= 0 else ""
        venue = lines[i - 2].strip() if i - 2 >= 0 else ""

        if venue.lower() in {"locations & directions", "locations"}:
            venue = ""

        locs.append({
            "Venue": venue,
            "Address": street,
            "City": city,
            "State": st,
            "Zip": z,
        })

    out: List[Dict[str, str]] = []
    seen = set()
    for l in locs:
        key = (l.get("Venue", ""), l.get("Address", ""), l.get("City", ""), l.get("State", ""), l.get("Zip", ""))
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out


def _extract_contact_from_contacts_page(html_text: str) -> Dict[str, str]:
    soup = BeautifulSoup(html_text, "html.parser")
    text = soup.get_text("\n")

    em = re.search(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text)
    email = em.group(1) if em else ""

    pm = re.search(r"\b(\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4})\b", text)
    phone = pm.group(1) if pm else ""

    name = ""
    if email:
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        for idx, ln in enumerate(lines):
            if email in ln:
                for j in range(idx - 1, max(-1, idx - 8), -1):
                    cand = lines[j].strip()
                    if "@" in cand or re.search(r"\d", cand):
                        continue
                    if 2 <= len(cand.split()) <= 6 and len(cand) <= 70:
                        name = cand
                        break
                break

    return {"Contact-name": name, "Email": email, "Phone-number": phone}


def _extract_date_raw_from_details_soup(details_soup: BeautifulSoup) -> str:
    """
    Prefer the header date in the details page:
      <hgroup> ... <h3>Jul 20-26</h3> ...
    Returns raw like 'Jul 20-26' or '' if not found.
    """
    for h3 in details_soup.select("hgroup h3"):
        t = h3.get_text(" ", strip=True)
        if not t:
            continue
        if "," in t:
            # exclude "Panama City Beach, FL" lines
            continue
        if re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", t, flags=re.IGNORECASE) and re.search(r"\d", t):
            return t
    return ""


class GrandSlamScraper(BaseScraper):
    SCRAPER_NAME = "GrandSlam"
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"
    BATCH_DELAY_SECONDS = 0  # requests-based; no need to wait

    def __init__(self):
        # details_url -> metadata extracted from listing card
        self.listing_meta: Dict[str, Dict[str, Any]] = {}

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        logger.warning("[GrandSlam] Loaded %s", GS_SCRAPER_VERSION)

        state = _parse_state_from_carrier(base_url)
        if not state:
            raise ValueError("GrandSlam base_url must include ?state=XX")

        page = 1
        out: List[str] = []
        seen = set()

        while True:
            if page == 1:
                url = f"{BASE}/baseball/Events?state={state}"
            else:
                url = f"{BASE}/baseball/Events?page={page}&state={state}"

            soup = _soup(url)

            anchors = soup.find_all("a", href=True)
            page_links: List[Tuple[Any, str]] = []

            for a in anchors:
                href = a.get("href", "")
                if not href:
                    continue

                a_text = a.get_text(" ", strip=True).lower()
                a_classes = " ".join(a.get("class", [])).lower()

                is_event_info_button = ("event info" in a_text) or ("btn-muted" in a_classes and "btn" in a_classes)
                if not is_event_info_button:
                    continue

                if re.search(r"/baseball/events/details/\d+/", href, flags=re.IGNORECASE):
                    full = urljoin(BASE, href)
                    page_links.append((a, full))

            new_links = 0
            for event_info_a, full in page_links:
                if full in seen:
                    continue
                seen.add(full)
                out.append(full)
                new_links += 1

                meta: Dict[str, Any] = {
                    "state": state,
                    "ages": [],
                    "divisions": [],
                    "gg": "",
                }

                badges = _find_badges_container_from_eventinfo_anchor(event_info_a)
                ages, divs = _extract_ages_and_divisions_from_badges(badges)
                meta["ages"] = ages
                meta["divisions"] = divs

                if badges:
                    meta["gg"] = _extract_guaranteed_games(" ".join(list(badges.stripped_strings))) or ""

                self.listing_meta[full] = meta

            if new_links == 0:
                break
            if _is_next_disabled(soup):
                break

            page += 1

        logger.info("[GrandSlam] Collected %d events for state=%s", len(out), state)
        return out

    def extract_event_details(self, url: str, driver, **kwargs) -> Dict[str, str]:
        meta = self.listing_meta.get(url, {})
        selected_state_abbr = (meta.get("state") or "").strip().upper()

        details_url, locations_url, contacts_url, divisions_url = _derive_gs_related_urls(url)

        details_html = _http_get(details_url)
        details_soup = BeautifulSoup(details_html, "html.parser")
        details_text = details_soup.get_text("\n")

        name = ""
        h1 = details_soup.find("h1")
        if h1:
            name = h1.get_text(" ", strip=True)
        if not name:
            t = details_soup.find("title")
            if t:
                name = t.get_text(" ", strip=True)

        year = _parse_year_from_text(details_text) or date.today().year

        # SAFE DATE STRATEGY:
        # - get date from details header (hgroup h3)
        # - if missing, fallback to a page-text regex
        date_raw = _extract_date_raw_from_details_soup(details_soup)
        if not date_raw:
            dm = re.search(
                r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2}"
                r"(?:\s*-\s*(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?\s+)?\d{1,2})?\b",
                details_text,
                flags=re.IGNORECASE
            )
            date_raw = dm.group(0) if dm else ""

        start_d, end_d = _parse_date_range(date_raw, year) if date_raw else (None, None)

        notes = ""
        for line in [ln.strip() for ln in details_text.split("\n") if ln.strip()]:
            if len(line) >= 90 and "Entry Fee" not in line and "Register" not in line:
                notes = line
                break

        locs: List[Dict[str, str]] = []
        try:
            locs = _extract_locations_from_locations_page(_http_get(locations_url))
        except Exception as e:
            logger.warning("[GrandSlam] Locations parse failed for %s: %s", locations_url, e)
            locs = []

        contact = {"Contact-name": "", "Email": "", "Phone-number": ""}
        try:
            contact = _extract_contact_from_contacts_page(_http_get(contacts_url))
        except Exception as e:
            logger.warning("[GrandSlam] Contacts parse failed for %s: %s", contacts_url, e)

        division_fee_map: Dict[str, str] = {}
        try:
            division_fee_map = _extract_division_fees_from_divisions_page(_http_get(divisions_url))
        except Exception as e:
            logger.warning("[GrandSlam] Divisions parse failed for %s: %s", divisions_url, e)
            division_fee_map = {}

        row: Dict[str, str] = {k: "" for k in OUTPUT_COLUMNS}

        row["Tournament-name"] = (name or "").strip()
        if start_d:
            row["Start-date"] = _mmddyyyy(start_d)
        if end_d:
            row["End-date"] = _mmddyyyy(end_d)

        state_abbr_for_row = ""
        if locs:
            row["Venue"] = locs[0].get("Venue", "") or ""
            row["Address"] = locs[0].get("Address", "") or ""
            row["City"] = locs[0].get("City", "") or ""
            state_abbr_for_row = (locs[0].get("State", "") or "").strip().upper()
            row["Zip"] = locs[0].get("Zip", "") or ""

            if len(locs) > 1:
                row["Are-there-multiple_locations"] = "Yes"
                parts = []
                for l in locs:
                    nm = (l.get("Venue", "") or "").strip()
                    addr = (l.get("Address", "") or "").strip()
                    csz = f'{(l.get("City","") or "").strip()}, {(l.get("State","") or "").strip()} {(l.get("Zip","") or "").strip()}'.strip()
                    if nm and addr:
                        parts.append(f"{nm} - {addr} - {csz}")
                    elif addr:
                        parts.append(f"{addr} - {csz}")
                row["More-information-and-additional-tournament-locations"] = "; ".join([p for p in parts if p])
            else:
                row["Are-there-multiple_locations"] = "No"
        else:
            row["Are-there-multiple_locations"] = "No"
            state_abbr_for_row = selected_state_abbr

        row["State-FullName"] = _state_full_name(state_abbr_for_row or selected_state_abbr)

        ages = meta.get("ages") or []
        for idx, ag in enumerate(ages[:13], start=1):
            row[f"Age-groups.{idx}"] = ag
            row[f"Entry-fee.{idx}"] = division_fee_map.get(ag, "")

        divs = meta.get("divisions") or []
        for idx, dv in enumerate(divs[:9], start=1):
            row[f"Competition-level.{idx}"] = dv

        gg = meta.get("gg") or ""
        if not gg:
            gg = _extract_guaranteed_games(details_text) or ""
        row["Guaranteed-games"] = gg

        row["Contact-name"] = contact.get("Contact-name", "") or ""
        row["Email"] = contact.get("Email", "") or ""
        row["Phone-number"] = contact.get("Phone-number", "") or ""

        row["Website-url"] = details_url

        if notes:
            if row["More-information-and-additional-tournament-locations"]:
                row["More-information-and-additional-tournament-locations"] = (
                    row["More-information-and-additional-tournament-locations"] + " | " + notes
                ).strip(" |")
            else:
                row["More-information-and-additional-tournament-locations"] = notes

        for k in list(row.keys()):
            v = row.get(k)
            row[k] = "" if v is None else str(v)

        return row

    def get_event_key(self, url: str) -> str:
        return url
