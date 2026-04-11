import re
from datetime import date, datetime
from html import unescape
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from us import states

from common.config import OUTPUT_COLUMNS
from scrapers.base_scraper import BaseScraper

TRIPLE_CROWN_FEED_BASE = "https://components.triplecrownsports.com/Components/GetFilteredEventSchedule/?sportID=1&state="
TC_ALLOWED_HOSTS = {
    "www.triplecrownbaseball.com",
    "www.pathwaybaseball.com",
    "www.tcspringtraining.com",
    "www.omahaslumpbuster.com",
    "www.tcsocalbaseball.com",
    "www.tcmidwestbaseball.com",
    "www.tcarizonabaseball.com",
    "allstartournaments.com",
}
TC_WEBFLOW_HOSTS = {
    "www.triplecrownbaseball.com",
    "www.tcspringtraining.com",
    "www.omahaslumpbuster.com",
}
TC_WEEBLY_HOSTS = {
    "www.tcsocalbaseball.com",
    "www.tcmidwestbaseball.com",
    "www.tcarizonabaseball.com",
}


def _http_get(session: requests.Session, url: str, timeout: int = 30) -> str:
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _soup(session: requests.Session, url: str) -> BeautifulSoup:
    return BeautifulSoup(_http_get(session, url), "html.parser")


def _state_full_name(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    state_obj = states.lookup(raw)
    return state_obj.name if state_obj else raw


def _is_us_state_token(value: str) -> bool:
    return bool(states.lookup((value or "").strip()))


def _mmddyyyy(value: date) -> str:
    return value.strftime("%m/%d/%Y")


def _extract_phone(text: str) -> str:
    match = re.search(r"(?:\+?1[\s\-\.]?)?\(?(\d{3})\)?[\s\-\.]?(\d{3})[\s\-\.]?(\d{4})", text or "")
    if not match:
        return ""
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def _decode_cf_email(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        key = int(encoded[:2], 16)
        chars = []
        for idx in range(2, len(encoded), 2):
            chars.append(chr(int(encoded[idx:idx + 2], 16) ^ key))
        return "".join(chars)
    except Exception:
        return ""


def _extract_emails(soup: BeautifulSoup) -> List[str]:
    emails: List[str] = []
    seen = set()

    for link in soup.select("a[href^='mailto:']"):
        email = (link.get("href") or "")[7:].strip().lower()
        if email and email not in seen:
            seen.add(email)
            emails.append(email)

    for node in soup.select("[data-cfemail]"):
        decoded = _decode_cf_email((node.get("data-cfemail") or "").strip()).lower()
        if decoded and decoded not in seen:
            seen.add(decoded)
            emails.append(decoded)

    html = str(soup)
    for match in re.finditer(r"([A-Za-z0-9._%+\-]+)\s*</span>\s*<a[^>]*>\s*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", html, flags=re.I):
        email = f"{match.group(1).strip()}@{match.group(2).strip()}".lower()
        if email not in seen:
            seen.add(email)
            emails.append(email)

    text = soup.get_text(" ", strip=True)
    for match in re.finditer(r"([A-Za-z0-9._%+\-]+)\s*@\s*([A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text):
        email = f"{match.group(1)}@{match.group(2)}".lower()
        if email not in seen:
            seen.add(email)
            emails.append(email)
    for match in re.finditer(r"([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})", text):
        email = match.group(1).lower()
        if email not in seen:
            seen.add(email)
            emails.append(email)

    return emails


def _extract_age_groups(text: str) -> List[str]:
    normalized = (text or "").replace("\u2013", "-").replace("\u2014", "-").replace("thru", "-")
    normalized = re.sub(r"(\d{1,2})\s*'s\b", r"\1u", normalized, flags=re.I)
    normalized = re.sub(r"(\d{1,2})\s*s\b", r"\1u", normalized, flags=re.I)

    age_groups: List[str] = []
    seen = set()

    for match in re.finditer(r"\b(\d{1,2})\s*[uU]\s*-\s*(\d{1,2})\s*[uU]\b", normalized):
        start_age = int(match.group(1))
        end_age = int(match.group(2))
        low_age = min(start_age, end_age)
        high_age = max(start_age, end_age)
        for age in range(low_age, high_age + 1):
            if 6 <= age <= 18:
                token = f"{age}u"
                if token not in seen:
                    seen.add(token)
                    age_groups.append(token)

    for match in re.finditer(r"\b(\d{1,2})\s*[uU]\b", normalized):
        age = int(match.group(1))
        if 6 <= age <= 18:
            token = f"{age}u"
            if token not in seen:
                seen.add(token)
                age_groups.append(token)

    return age_groups


def _extract_guaranteed_games(text: str) -> str:
    if re.search(r"\b\d+\s*(?:or|/)\s*\d+\s*game", text or "", flags=re.I):
        return ""
    match = re.search(r"\b(\d+)\s*-\s*game\s+guarantee\b", text or "", flags=re.I)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d+)\s*game\s+guarantee\b", text or "", flags=re.I)
    if match:
        return match.group(1)
    return ""


def _parse_month_day_range(text: str, fallback_year: Optional[int] = None) -> Tuple[str, str]:
    value = " ".join((text or "").split()).strip()
    if not value:
        return "", ""
    value = value.replace("\u2013", "-").replace("\u2014", "-")
    value = re.sub(r"\bSept\b", "Sep", value, flags=re.I)
    value = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.I)
    year = fallback_year or date.today().year

    patterns = [
        (r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})$", ("%B %d %Y", 1, 2, 5), ("%B %d %Y", 3, 4, 5)),
        (r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2}),?\s*(\d{4})$", ("%B %d %Y", 1, 2, 4), ("%B %d %Y", 1, 3, 4)),
        (r"^([A-Za-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})$", ("%B %d %Y", 1, 2, None), ("%B %d %Y", 1, 3, None)),
        (r"^([A-Za-z]+)\s+(\d{1,2}),?\s*(\d{4})$", ("%B %d %Y", 1, 2, 3), ("%B %d %Y", 1, 2, 3)),
    ]

    for pattern, start_spec, end_spec in patterns:
        match = re.match(pattern, value, flags=re.I)
        if not match:
            continue

        def build_dt(spec: Tuple[str, int, int, Optional[int]]) -> str:
            _, month_idx, day_idx, year_idx = spec
            actual_year = int(match.group(year_idx)) if year_idx else year
            month_part = match.group(month_idx)
            day_part = match.group(day_idx)
            for fmt in ("%B %d %Y", "%b %d %Y"):
                try:
                    dt = datetime.strptime(f"{month_part} {day_part} {actual_year}", fmt).date()
                    return _mmddyyyy(dt)
                except ValueError:
                    continue
            raise ValueError(f"Unparseable month/day range: {value}")

        return build_dt(start_spec), build_dt(end_spec)

    return "", ""


def _parse_tc_row_dates(row: BeautifulSoup) -> Tuple[Optional[date], Optional[date]]:
    parts = [part.get_text(" ", strip=True) for part in row.select("div.event-date-container > div")]
    start_dt = None
    if len(parts) >= 3:
        try:
            start_dt = date(int(parts[2]), datetime.strptime(parts[1], "%b").month, int(parts[0]))
        except Exception:
            start_dt = None

    text = " ".join(row.get_text(" ", strip=True).split())
    match = re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*([A-Za-z]{3})\s+(\d{1,2})\s*-\s*(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*([A-Za-z]{3})\s+(\d{1,2})", text)
    if match:
        year = start_dt.year if start_dt else date.today().year
        try:
            parsed_start = date(year, datetime.strptime(match.group(1), "%b").month, int(match.group(2)))
            parsed_end = date(year, datetime.strptime(match.group(3), "%b").month, int(match.group(4)))
            return parsed_start, parsed_end
        except Exception:
            pass

    return start_dt, start_dt


def _extract_map_components(href: str) -> Dict[str, str]:
    parsed = urlparse(href)
    query = parse_qs(parsed.query).get("q", [""])[0]
    raw = unquote(query or "").strip()
    venue = ""

    if not raw and "/maps/place/" in parsed.path:
        place_part = parsed.path.split("/maps/place/", 1)[1].split("/@", 1)[0]
        venue = unquote(place_part or "").replace("+", " ").strip()

    match = re.search(r"^(.*?),\s*([A-Z]{2})\s*(\d{5})(?:-\d{4})?$", raw)
    if not match:
        return {"Venue": venue, "Address": "", "City": "", "State-FullName": "", "Zip": ""}

    address_city = match.group(1).strip()
    state_abbr = match.group(2).strip()
    zip_code = match.group(3).strip()

    if "," in address_city:
        address, city = [part.strip() for part in address_city.rsplit(",", 1)]
    else:
        tokens = address_city.split()
        suffixes = {
            "st", "street", "rd", "road", "ave", "avenue", "blvd", "boulevard", "dr", "drive",
            "ln", "lane", "way", "parkway", "pkwy", "court", "ct", "circle", "cir", "place",
            "pl", "trail", "trl", "highway", "hwy",
        }
        split_idx = -1
        for idx, token in enumerate(tokens):
            if token.strip(".,").lower() in suffixes:
                split_idx = idx
        if split_idx >= 0 and split_idx + 1 < len(tokens):
            address = " ".join(tokens[: split_idx + 1]).strip()
            city = " ".join(tokens[split_idx + 1 :]).strip()
        else:
            address = address_city
            city = ""

    return {
        "Venue": venue,
        "Address": address,
        "City": city,
        "State-FullName": _state_full_name(state_abbr),
        "Zip": zip_code,
    }


def _make_blank_row() -> Dict[str, str]:
    return {col: "" for col in OUTPUT_COLUMNS}


def _extract_comp_levels(text: str) -> List[str]:
    source = (text or "").upper()
    levels: List[str] = []

    def add(level: str) -> None:
        if level not in levels:
            levels.append(level)

    if "D1" in source or "DIVISION 1" in source:
        add("D1")
    if "D2" in source or "DIVISION 2" in source:
        add("D2")
    if re.search(r"\bAAA\b", source):
        add("AAA")
    if re.search(r"\bAA\b", source):
        add("AA")
    if re.search(r"\bOPEN\b", source):
        add("Open")
    if re.search(r"\bREC\b", source):
        add("Rec")
    return levels


def _extract_city_state(text: str) -> Tuple[str, str]:
    value = " ".join((text or "").split()).strip()
    if not value:
        return "", ""
    if "|" in value:
        value = value.split("|")[-1].strip()
    if "," in value:
        city, state_part = value.rsplit(",", 1)
        state_part = state_part.strip()
        if _is_us_state_token(state_part):
            return city.strip(), _state_full_name(state_part)
    return "", ""


def _normalize_case_token(token: str) -> str:
    if not token:
        return ""
    if token in {"BYU", "USA", "USSSA", "TC", "TCS", "JV"}:
        return token
    if "-" in token:
        return "-".join(_normalize_case_token(part) for part in token.split("-"))
    if "'" in token:
        return "'".join(_normalize_case_token(part) for part in token.split("'"))
    return token[:1].upper() + token[1:].lower()


def _normalize_display_text(value: str) -> str:
    text = " ".join((value or "").replace("\u200b", " ").split()).strip()
    if not text:
        return ""

    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return text

    has_weird_case = bool(re.search(r"[a-z][A-Z]|[A-Z][A-Z][a-z]", text))
    is_all_caps = letters and all(ch.isupper() for ch in letters)
    is_all_lower = letters and all(ch.islower() for ch in letters)
    if not has_weird_case and not is_all_caps and not is_all_lower:
        return text

    return " ".join(_normalize_case_token(token) for token in text.split())


def _normalize_city_value(value: str) -> str:
    city = _normalize_display_text(value)
    city = re.sub(r"\s*\([^)]*\)\s*$", "", city).strip()
    if " & " in city:
        city = city.split(" & ", 1)[0].strip()
    elif re.search(r"\band\b", city, flags=re.I):
        city = re.split(r"\band\b", city, maxsplit=1, flags=re.I)[0].strip()
    if re.search(r"\bcounty\b", city, flags=re.I):
        return ""
    return city


def _parse_us_city_state_zip(value: str) -> Tuple[str, str, str]:
    match = re.search(r"^(.*?),\s*([A-Za-z ]+|[A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?$", value or "")
    if not match:
        return "", "", ""
    state_token = match.group(2).strip()
    if not _is_us_state_token(state_token):
        return "", "", ""
    city = _normalize_city_value(match.group(1).strip())
    return city, _state_full_name(state_token), (match.group(3) or "").strip()


def _normalize_person_name(value: str) -> str:
    text = " ".join((value or "").split()).strip()
    if not text:
        return ""
    return " ".join(_normalize_case_token(token) for token in text.split())


def _looks_like_venue_blob(value: str) -> bool:
    text = " ".join((value or "").split()).strip()
    return len(text) > 80 and len(text.split()) >= 8


def _split_inline_us_address(value: str) -> Tuple[str, str, str, str]:
    text = " ".join((value or "").replace("\u200b", " ").split()).strip()
    if not text:
        return "", "", "", ""
    state_match = re.search(r"\b([A-Z]{2})(?:\s+(\d{5})(?:-\d{4})?)?$", text)
    if not state_match or not _is_us_state_token(state_match.group(1).strip()):
        return text, "", "", ""

    prefix = text[: state_match.start()].rstrip(" ,")
    zip_code = (state_match.group(2) or "").strip()
    suffixes = {
        "st", "street", "rd", "road", "ave", "avenue", "blvd", "boulevard", "dr", "drive",
        "ln", "lane", "way", "parkway", "pkwy", "court", "ct", "circle", "cir", "place",
        "pl", "trail", "trl", "highway", "hwy",
    }
    tokens = prefix.split()
    split_idx = -1
    for idx, token in enumerate(tokens):
        if token.strip(".,").lower() in suffixes:
            split_idx = idx
    if split_idx < 0 or split_idx + 1 >= len(tokens):
        return text, "", "", ""

    address = " ".join(tokens[: split_idx + 1]).strip()
    city = _normalize_city_value(" ".join(tokens[split_idx + 1 :]).strip(" ,"))
    return address, city, _state_full_name(state_match.group(1).strip()), zip_code


def _split_international_address(value: str) -> Tuple[str, str, str, str]:
    parts = [part.strip() for part in (value or "").split(",") if part.strip()]
    if len(parts) < 3:
        return "", "", "", ""

    country = parts[-1]
    locality = parts[-2]
    zip_match = re.search(r"^(.*?)(\d{4,10})$", locality)
    if zip_match:
        city = _normalize_city_value(zip_match.group(1).strip())
        zip_code = zip_match.group(2).strip()
    else:
        city = _normalize_city_value(locality)
        zip_code = ""
    address = ", ".join(parts[:-2]).strip()
    return address, city, country, zip_code


def _parse_facility_block(lines: List[str]) -> Dict[str, str]:
    clean_lines = []
    for line in lines:
        line = " ".join((line or "").replace("\u200b", " ").split()).strip()
        if not line or re.search(r"^\^?\d{1,2}u", line, flags=re.I):
            continue
        clean_lines.append(line)

    address = ""
    city = ""
    state_full = ""
    zip_code = ""

    if not clean_lines:
        return {"Address": "", "City": "", "State-FullName": "", "Zip": ""}

    if len(clean_lines) >= 2:
        city, state_full, zip_code = _parse_us_city_state_zip(clean_lines[1])
        if city or state_full or zip_code:
            address = clean_lines[0]
            return {
                "Address": address,
                "City": city,
                "State-FullName": state_full,
                "Zip": zip_code,
            }

    address, city, state_full, zip_code = _split_inline_us_address(clean_lines[0])
    if city or state_full or zip_code:
        return {
            "Address": address,
            "City": city,
            "State-FullName": state_full,
            "Zip": zip_code,
        }

    address, city, state_full, zip_code = _split_international_address(clean_lines[0])
    if city or state_full:
        return {
            "Address": address or clean_lines[0],
            "City": city,
            "State-FullName": state_full,
            "Zip": zip_code,
        }

    return {"Address": clean_lines[0], "City": "", "State-FullName": "", "Zip": ""}


def _city_key(value: str) -> str:
    return re.sub(r"[^a-z]", "", (value or "").lower())


def _normalize_text_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def _extract_location_line(text: str) -> str:
    match = re.search(r"Location[s]?:\s*([^\n]+)", text or "", flags=re.I)
    return match.group(1).strip() if match else ""


def _age_count_from_row(row: Dict[str, str]) -> int:
    return sum(1 for idx in range(1, 14) if (row.get(f"Age-groups.{idx}", "") or "").strip())


def _parse_location_hint(value: str) -> Dict[str, str]:
    text = " ".join((value or "").replace("\u200b", " ").split()).strip()
    if not text:
        return {"Venue": "", "City": "", "State-FullName": ""}

    match = re.match(r"^(.*?)\s*-\s*([^,]+),\s*([A-Za-z ]+|[A-Z]{2})$", text)
    if match and _is_us_state_token(match.group(3).strip()):
        return {
            "Venue": _normalize_display_text(match.group(1).strip()),
            "City": _normalize_city_value(match.group(2).strip()),
            "State-FullName": _state_full_name(match.group(3).strip()),
        }

    city, state = _extract_city_state(text)
    if city or state:
        return {"Venue": "", "City": _normalize_city_value(city), "State-FullName": state}

    return {"Venue": "", "City": "", "State-FullName": ""}


class TripleCrownScraper(BaseScraper):
    SCRAPER_NAME = "TripleCrown"
    REQUIRES_DRIVER = False
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        self.listing_meta: Dict[str, Dict[str, Any]] = {}

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        soup = _soup(self.session, base_url)
        today = date.today()
        urls: List[str] = []
        self.listing_meta = {}

        selected_state_abbr = parse_qs(urlparse(base_url).query).get("state", [""])[0].strip().upper()

        for row in soup.select("div.event-row"):
            link = row.select_one("a.event-name[href]")
            if not link:
                continue

            href = (link.get("href") or "").strip()
            if not href:
                continue

            parsed = urlparse(href)
            host = parsed.netloc.lower()
            if host not in TC_ALLOWED_HOSTS:
                continue

            start_dt, end_dt = _parse_tc_row_dates(row)
            if not start_dt or start_dt <= today:
                continue

            detail_block = row.select_one("div.event-details")
            location_anchors = detail_block.select("a.font-red") if detail_block else []
            location_text = location_anchors[-1].get_text(" ", strip=True) if location_anchors else ""
            city, state_full = _extract_city_state(location_text)
            if not state_full and selected_state_abbr:
                state_full = _state_full_name(selected_state_abbr)

            listing_text = " ".join(row.get_text(" ", strip=True).split())
            self.listing_meta[href] = {
                "host": host,
                "listing_name": link.get_text(" ", strip=True),
                "start_date": _mmddyyyy(start_dt),
                "end_date": _mmddyyyy(end_dt or start_dt),
                "listing_location": location_text,
                "listing_city": city,
                "listing_state": state_full,
                "selected_state": _state_full_name(selected_state_abbr),
                "listing_text": listing_text,
            }
            urls.append(href)

        return urls

    def _apply_common_requireds(self, row: Dict[str, str]) -> Dict[str, str]:
        tournament_key = _normalize_text_key(row.get("Tournament-name", ""))
        contact_key = _normalize_text_key(row.get("Contact-name", ""))
        if contact_key and tournament_key:
            if contact_key == tournament_key or tournament_key in contact_key or contact_key in tournament_key:
                row["Contact-name"] = ""
            elif "home" in contact_key and tournament_key in contact_key.replace("home", ""):
                row["Contact-name"] = ""

        has_age = any(row.get(f"Age-groups.{idx}", "").strip() for idx in range(1, 14))
        if not has_age:
            raise ValueError("Missing required age group data")
        if not row.get("Email", "").strip():
            raise ValueError("Missing required email")
        if not row.get("State-FullName", "").strip():
            raise ValueError("Missing required state")
        return row

    def _extract_first_contact(self, soup: BeautifulSoup) -> Tuple[str, str, str]:
        emails = _extract_emails(soup)
        text = soup.get_text("\n", strip=True)
        phone = _extract_phone(text)
        contact_name = ""

        contact_blocks: List[Tuple[str, int]] = []

        for email in emails:
            lowered = email.lower()
            email_node = soup.find(string=lambda s: s and lowered in s.lower())
            if email_node and email_node.parent:
                block = email_node.parent.get_text("\n", strip=True)
                score = 0
                lowered_block = block.lower()
                if "call or text" in lowered_block:
                    score += 5
                if "contact us" in lowered_block:
                    score += 3
                if "lodging" in lowered_block:
                    score -= 3
                if "eventconnect" in lowered_block:
                    score -= 4
                contact_blocks.append((block, score))

        for node in soup.select("div.event-details-item, div.waddons-tabs__contents-block, div.paragraph"):
            block_text = node.get_text("\n", strip=True)
            if any(email in block_text.lower() for email in emails) or _extract_phone(block_text):
                lowered_block = block_text.lower()
                score = 0
                if "call or text" in lowered_block:
                    score += 5
                if "contact us" in lowered_block:
                    score += 3
                if "lodging" in lowered_block:
                    score -= 3
                if "eventconnect" in lowered_block:
                    score -= 4
                contact_blocks.append((block_text, score))

        seen_blocks = set()
        selected_email = emails[0] if emails else ""
        selected_phone = phone
        for block, _ in sorted(contact_blocks, key=lambda item: item[1], reverse=True):
            normalized_block = block.strip()
            if not normalized_block or normalized_block in seen_blocks:
                continue
            seen_blocks.add(normalized_block)
            block_emails = [e for e in emails if e in normalized_block.lower()]
            block_phone = _extract_phone(normalized_block)
            block_lines = [part.strip() for part in normalized_block.splitlines() if part.strip()]
            for part in block_lines:
                if re.fullmatch(r"[A-Za-z][A-Za-z'.\-]+(?:\s+[A-Za-z][A-Za-z'.\-]+){1,3}", part):
                    lowered = part.lower()
                    if not any(bad in lowered for bad in ["triple crown", "event info", "useful links", "register", "schedule", "results", "home"]):
                        contact_name = part
                        if block_phone:
                            selected_phone = block_phone
                        if block_emails:
                            selected_email = block_emails[0]
                        break
            if contact_name:
                break

        return _normalize_person_name(contact_name), selected_phone, selected_email

    def _fill_comp_levels(self, row: Dict[str, str], text: str) -> None:
        levels = _extract_comp_levels(text)
        for idx, level in enumerate(levels[:9], start=1):
            row[f"Competition-level.{idx}"] = level

    def _find_exposure_url(self, soup: BeautifulSoup) -> str:
        exposure_links: List[str] = []
        for link in soup.select("a[href*='exposureevents.com']"):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if "/schedule" in href:
                href = href.rsplit("/schedule", 1)[0]
            exposure_links.append(href)
        return exposure_links[0] if exposure_links else ""

    def _extract_exposure_fallback(self, exposure_url: str) -> Dict[str, str]:
        if not exposure_url:
            return {}

        soup = _soup(self.session, exposure_url)
        full_text = soup.get_text("\n", strip=True)
        data: Dict[str, str] = {}

        desc_lines = [
            " ".join(node.get_text(" ", strip=True).split())
            for node in soup.select("ul[itemprop='description'] li")
        ]
        age_source = "\n".join(desc_lines)
        if not age_source.strip():
            targeted_blocks = []
            for node in soup.select(".row, .col-12, .col-12.col-md-6, .mb-5"):
                txt = " ".join(node.get_text(" ", strip=True).split())
                lowered = txt.lower()
                if any(marker in lowered for marker in ["boys:", "girls:", "divisions:", "general information", "description"]):
                    targeted_blocks.append(txt)
            age_source = "\n".join(targeted_blocks)

        age_groups = _extract_age_groups(age_source)
        if age_groups:
            data["age_groups"] = "|".join(age_groups)

        contact = soup.select_one("div[itemprop='performer']")
        if contact:
            name_node = contact.select_one("[itemprop='name']")
            phone_node = contact.select_one("[itemprop='telephone']")
            email_node = contact.select_one("[itemprop='email'], a.email[href^='mailto:']")
            if name_node:
                data["contact_name"] = _normalize_person_name(name_node.get_text(" ", strip=True))
            if phone_node:
                data["phone"] = _extract_phone(phone_node.get_text(" ", strip=True))
            if email_node:
                data["email"] = (
                    (email_node.get("content") or "").strip().lower()
                    or (email_node.get("href") or "").replace("mailto:", "").strip().lower()
                    or email_node.get_text(" ", strip=True).strip().lower()
                )

        address = soup.select_one("address[itemprop='location']")
        if address:
            venue_node = address.select_one("[itemprop='name']")
            locality = address.select_one("[itemprop='addressLocality']")
            region = address.select_one("[itemprop='addressRegion']")
            postal = address.select_one("[itemprop='postalCode']")
            street = address.select_one("[itemprop='streetAddress']")
            if venue_node:
                data["venue"] = _normalize_display_text(venue_node.get_text(" ", strip=True))
            if street:
                data["address"] = " ".join(street.get_text(" ", strip=True).split()).strip()
            if locality:
                data["city"] = _normalize_city_value(locality.get_text(" ", strip=True))
            if region:
                data["state"] = _state_full_name(region.get_text(" ", strip=True))
            if postal:
                data["zip"] = postal.get_text(" ", strip=True).strip()

        gg = _extract_guaranteed_games(full_text)
        if gg:
            data["guaranteed_games"] = gg

        return data

    def _merge_exposure_fallback(self, row: Dict[str, str], soup: BeautifulSoup) -> Dict[str, str]:
        needs_fallback = (
            _age_count_from_row(row) == 0
            or not (row.get("Email", "") or "").strip()
            or not (row.get("City", "") or "").strip()
            or not (row.get("State-FullName", "") or "").strip()
        )
        if not needs_fallback:
            return row

        exposure_url = self._find_exposure_url(soup)
        if not exposure_url:
            return row

        fallback = self._extract_exposure_fallback(exposure_url)
        if not fallback:
            return row

        if _age_count_from_row(row) == 0 and fallback.get("age_groups"):
            for idx, age in enumerate(fallback["age_groups"].split("|")[:13], start=1):
                row[f"Age-groups.{idx}"] = age

        tc_has_contact = any((row.get(field, "") or "").strip() for field in ["Contact-name", "Phone-number", "Email"])
        if not tc_has_contact:
            if fallback.get("contact_name"):
                row["Contact-name"] = fallback["contact_name"]
            if fallback.get("phone"):
                row["Phone-number"] = fallback["phone"]
            if fallback.get("email"):
                row["Email"] = fallback["email"]

        if not (row.get("Venue", "") or "").strip() and fallback.get("venue"):
            row["Venue"] = fallback["venue"]
        if not (row.get("Address", "") or "").strip() and fallback.get("address"):
            row["Address"] = fallback["address"]
        if not (row.get("City", "") or "").strip() and fallback.get("city"):
            row["City"] = fallback["city"]
        if not (row.get("State-FullName", "") or "").strip() and fallback.get("state"):
            row["State-FullName"] = fallback["state"]
        if not (row.get("Zip", "") or "").strip() and fallback.get("zip"):
            row["Zip"] = fallback["zip"]
        if not (row.get("Guaranteed-games", "") or "").strip() and fallback.get("guaranteed_games"):
            row["Guaranteed-games"] = fallback["guaranteed_games"]

        return row

    def _extract_core_facilities(self, soup: BeautifulSoup) -> Tuple[Optional[Dict[str, str]], List[str]]:
        facilities: List[Dict[str, str]] = []
        for item in soup.select("nav.event-info-details .facilities-item"):
            venue = item.select_one("h6")
            venue_name = _normalize_display_text(venue.get_text(" ", strip=True) if venue else "")
            p = item.select_one("p")
            lines = [line.strip() for line in (p.get_text("\n", strip=True).splitlines() if p else []) if line.strip()]
            parsed = _parse_facility_block(lines)
            address = parsed.get("Address", "")
            city = parsed.get("City", "")
            state_full = parsed.get("State-FullName", "")
            zip_code = parsed.get("Zip", "")
            joined_lines = " ".join(lines).lower()
            if not city and not state_full and not address:
                continue
            if "gate fee" in joined_lines or "portable mounds" in joined_lines or "food cannot be brought" in joined_lines:
                continue
            if venue_name or address or city:
                facilities.append(
                    {
                        "Venue": venue_name,
                        "Address": address,
                        "City": city,
                        "State-FullName": state_full,
                        "Zip": zip_code,
                    }
                )

        if not facilities:
            return None, []

        primary = facilities[0]
        if not primary.get("Address") and len(facilities) > 1:
            primary = {
                "Venue": "",
                "Address": "",
                "City": "",
                "State-FullName": "",
                "Zip": "",
            }

        extras = []
        for location in facilities[1:]:
            parts = [
                location.get("Venue", "").strip(),
                location.get("Address", "").strip(),
                f"{location.get('City', '').strip()}, {location.get('State-FullName', '').strip()} {location.get('Zip', '').strip()}".strip(),
            ]
            extras.append(" - ".join(part for part in parts if part))
        if _looks_like_venue_blob(primary.get("Venue", "")):
            extras.insert(0, primary.get("Venue", ""))
            primary = {
                "Venue": "",
                "Address": "",
                "City": "",
                "State-FullName": "",
                "Zip": "",
            }
        elif primary.get("Venue") and not primary.get("Address") and not primary.get("City"):
            extras.insert(0, primary.get("Venue", ""))
        return primary, extras

    def _handle_webflow_family(self, url: str, soup: BeautifulSoup, meta: Dict[str, Any]) -> Dict[str, str]:
        row = _make_blank_row()
        title = ""
        details_map: Dict[str, str] = {}

        if soup.select_one("div.banner-title-wrapper"):
            banner = soup.select_one("div.banner-title-wrapper")
            title_node = banner.select_one("h1.banner-title, h1.banner-title-2, h1")
            title = title_node.get_text(" ", strip=True) if title_node else ""
        else:
            title_node = soup.select_one("h1")
            title = title_node.get_text(" ", strip=True) if title_node else meta.get("listing_name", "")

        for text_row in soup.select("div.event-details-info div.text-row"):
            key_node = text_row.select_one("div.item-title")
            value_node = text_row.select_one("div.info-item-title")
            if key_node and value_node:
                details_map[key_node.get_text(" ", strip=True).rstrip(":")] = value_node.get_text(" ", strip=True)
                continue
            info_nodes = text_row.select("div.info-item-title")
            if len(info_nodes) >= 2:
                strong = info_nodes[0].find("strong")
                key = strong.get_text(" ", strip=True).rstrip(":") if strong else info_nodes[0].get_text(" ", strip=True).rstrip(":")
                value = info_nodes[1].get_text(" ", strip=True)
                if key:
                    details_map[key] = value

        full_text = soup.get_text("\n", strip=True)
        primary_location, extras = self._extract_core_facilities(soup)
        contact_name, phone, email = self._extract_first_contact(soup)
        page_city, page_state = _extract_city_state(details_map.get("Location", "") or _extract_location_line(full_text))

        page_start, page_end = _parse_month_day_range(details_map.get("Dates", ""), None)
        row["Tournament-name"] = title or meta.get("listing_name", "")
        row["Start-date"] = meta.get("start_date", "") or page_start
        row["End-date"] = meta.get("end_date", "") or page_end
        row["Are-there-multiple_locations"] = "Yes" if extras else "No"
        row["Venue"] = _normalize_display_text(primary_location.get("Venue", "") if primary_location else "")
        row["Address"] = primary_location.get("Address", "") if primary_location else ""
        row["Address-2"] = ""
        row["City"] = _normalize_city_value((primary_location.get("City", "") if primary_location else "") or page_city or meta.get("listing_city", ""))
        row["State-FullName"] = (primary_location.get("State-FullName", "") if primary_location else "") or page_state or meta.get("listing_state", "") or meta.get("selected_state", "")
        row["Zip"] = primary_location.get("Zip", "") if primary_location else ""
        row["Guaranteed-games"] = _extract_guaranteed_games(details_map.get("Format", "") or full_text)
        row["Gate-fees"] = "No" if re.search(r"\bno\s+gate\s+fees?\b", full_text, flags=re.I) else ("Yes" if re.search(r"\bgate\s+fees?\b", full_text, flags=re.I) else "NA")
        row["Hotel-required"] = "Yes" if "stay to play" in full_text.lower() else "NA"
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "Artificial" if "turf" in full_text.lower() else "NA"
        row["Contact-name"] = contact_name
        row["Phone-number"] = phone
        row["Email"] = email
        row["Website-url"] = url
        row["More-information-and-additional-tournament-locations"] = " | ".join(extras)

        age_source = details_map.get("Division", "") or details_map.get("Age Groups", "") or full_text
        age_groups = _extract_age_groups(age_source)
        price_text = details_map.get("Price", "")
        entry_fee = ""
        fee_match = re.search(r"\$([0-9][0-9,]*)", price_text)
        if fee_match:
            entry_fee = fee_match.group(1).replace(",", "")
        for idx, age in enumerate(age_groups[:13], start=1):
            row[f"Age-groups.{idx}"] = age
            row[f"Entry-fee.{idx}"] = entry_fee

        self._fill_comp_levels(row, full_text)
        row = self._merge_exposure_fallback(row, soup)
        return self._apply_common_requireds(row)

    def _handle_pathway(self, url: str, soup: BeautifulSoup, meta: Dict[str, Any]) -> Dict[str, str]:
        row = _make_blank_row()
        full_text = soup.get_text("\n", strip=True)
        title_node = soup.select_one("h1")
        contact_name, phone, email = self._extract_first_contact(soup)

        date_match = re.search(r"Dates:\s*([A-Za-z]+\s+\d{1,2}-\d{1,2},\s*\d{4})", full_text, flags=re.I)
        page_start, page_end = _parse_month_day_range(date_match.group(1), None) if date_match else ("", "")
        row["Tournament-name"] = title_node.get_text(" ", strip=True) if title_node else meta.get("listing_name", "")
        row["Start-date"] = meta.get("start_date", "") or page_start
        row["End-date"] = meta.get("end_date", "") or page_end
        row["Are-there-multiple_locations"] = "No"
        row["Address"] = ""
        row["Address-2"] = ""
        row["Venue"] = ""
        location_line = _extract_location_line(full_text)
        page_city, page_state = _extract_city_state(location_line)
        if location_line and not page_city:
            row["City"] = ""
        else:
            row["City"] = _normalize_city_value(page_city or meta.get("listing_city", ""))
        row["State-FullName"] = page_state or meta.get("listing_state", "") or meta.get("selected_state", "")
        row["Zip"] = ""
        row["Guaranteed-games"] = _extract_guaranteed_games(full_text)
        row["Gate-fees"] = "No" if re.search(r"\bno\s+gate\s+fees?\b", full_text, flags=re.I) else "NA"
        row["Hotel-required"] = "NA"
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "Artificial" if "turf" in full_text.lower() else "NA"
        row["Contact-name"] = contact_name
        row["Phone-number"] = phone
        row["Email"] = email
        row["Website-url"] = url
        row["More-information-and-additional-tournament-locations"] = ""

        age_groups = _extract_age_groups(full_text + "\n" + meta.get("listing_text", ""))
        for idx, age in enumerate(age_groups[:13], start=1):
            row[f"Age-groups.{idx}"] = age

        self._fill_comp_levels(row, full_text)
        row = self._merge_exposure_fallback(row, soup)
        return self._apply_common_requireds(row)

    def _extract_weebly_facilities(self, event_url: str, soup: BeautifulSoup) -> Tuple[Optional[Dict[str, str]], List[str]]:
        facilities_link = None
        for link in soup.select("a[href]"):
            if "facilities" in (link.get_text(" ", strip=True).lower()):
                href = (link.get("href") or "").strip()
                if href:
                    facilities_link = urljoin(event_url, href)
                    break

        if not facilities_link:
            return None, []

        facilities_soup = _soup(self.session, facilities_link)
        items: List[Dict[str, str]] = []

        for acc in facilities_soup.select("div.accordion__item"):
            text = acc.get_text("\n", strip=True)
            if "{title_" in text or "{content_" in text:
                continue
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            venue = _normalize_display_text(lines[0])
            parsed = _parse_facility_block(lines[1:])
            items.append(
                {
                    "Venue": venue,
                    "Address": parsed.get("Address", ""),
                    "City": parsed.get("City", ""),
                    "State-FullName": parsed.get("State-FullName", ""),
                    "Zip": parsed.get("Zip", ""),
                }
            )

        if not items:
            return None, []

        extras = []
        for location in items[1:]:
            parts = [
                location.get("Venue", "").strip(),
                location.get("Address", "").strip(),
                f"{location.get('City', '').strip()}, {location.get('State-FullName', '').strip()} {location.get('Zip', '').strip()}".strip(),
            ]
            extras.append(" - ".join(part for part in parts if part))
        return items[0], extras

    def _extract_weebly_prices(self, text: str) -> Dict[str, str]:
        prices: Dict[str, str] = {}
        normalized = text.replace("\u2013", "-").replace("\u2014", "-").replace("=", " = ")
        for match in re.finditer(r"(\d{1,2})\s*(?:U|u|'s|s)\b[^\n$]{0,40}\$([0-9][0-9,]*)", normalized):
            age = int(match.group(1))
            if 6 <= age <= 18:
                prices[f"{age}u"] = match.group(2).replace(",", "")
        return prices

    def _handle_weebly(self, url: str, soup: BeautifulSoup, meta: Dict[str, Any]) -> Dict[str, str]:
        row = _make_blank_row()
        full_text = soup.get_text("\n", strip=True)
        title = soup.title.get_text(" ", strip=True) if soup.title else meta.get("listing_name", "")
        title = re.sub(r"\s*-\s*TC .*?$", "", title).strip()

        details_text = ""
        first_col = soup.select_one("td.wsite-multicol-col")
        if first_col:
            details_text = first_col.get_text("\n", strip=True)

        details_source = details_text or full_text
        date_match = re.search(r"Date[s]?:\s*([A-Za-z]+\s+\d{1,2}(?:-\d{1,2}|th-\d{1,2}th)?(?:,\s*\d{4}|\s*\d{4}|-\d{1,2}th,\s*\d{4})?)", details_source, flags=re.I)
        if date_match:
            start_date, end_date = _parse_month_day_range(date_match.group(1), None)
        else:
            start_date, end_date = meta.get("start_date", ""), meta.get("end_date", "")
        if not start_date:
            start_date = meta.get("start_date", "")
        if not end_date:
            end_date = meta.get("end_date", "")

        location_match = re.search(r"Location[s]?:\s*([^\n]+)", details_source, flags=re.I)
        city = ""
        venue_hint = ""
        state_full = meta.get("listing_state", "") or meta.get("selected_state", "")
        if location_match:
            parsed_hint = _parse_location_hint(location_match.group(1))
            city = parsed_hint.get("City", "")
            venue_hint = parsed_hint.get("Venue", "")
            state_full = parsed_hint.get("State-FullName", "") or state_full

        age_source = details_source + "\n" + full_text
        age_groups = _extract_age_groups(age_source)
        prices = self._extract_weebly_prices(full_text)
        primary_location, extras = self._extract_weebly_facilities(url, soup)
        if primary_location and city and _city_key(primary_location.get("City", "")) != _city_key(city):
            primary_location = None
            extras = []
        contact_name, phone, email = self._extract_first_contact(soup)

        row["Tournament-name"] = title or meta.get("listing_name", "")
        row["Start-date"] = start_date
        row["End-date"] = end_date
        row["Are-there-multiple_locations"] = "Yes" if extras else "No"
        row["Venue"] = _normalize_display_text(primary_location.get("Venue", "") if primary_location else "") or venue_hint
        row["Address"] = primary_location.get("Address", "") if primary_location else ""
        row["Address-2"] = ""
        row["City"] = _normalize_city_value((primary_location.get("City", "") if primary_location else "") or city or meta.get("listing_city", ""))
        row["State-FullName"] = (primary_location.get("State-FullName", "") if primary_location else "") or state_full
        row["Zip"] = primary_location.get("Zip", "") if primary_location else ""
        row["Guaranteed-games"] = _extract_guaranteed_games(full_text)
        row["Gate-fees"] = "No" if re.search(r"\bno\s+gate\s+fees?\b", full_text, flags=re.I) else ("Yes" if re.search(r"\bGATE:\b|\bgate\s+fee", full_text, flags=re.I) else "NA")
        row["Hotel-required"] = "Yes" if "stay to play" in full_text.lower() else "NA"
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "Artificial" if "turf" in full_text.lower() else "NA"
        row["Contact-name"] = contact_name
        row["Phone-number"] = phone
        row["Email"] = email
        row["Website-url"] = url
        row["More-information-and-additional-tournament-locations"] = " | ".join(extras)

        for idx, age in enumerate(age_groups[:13], start=1):
            row[f"Age-groups.{idx}"] = age
            row[f"Entry-fee.{idx}"] = prices.get(age, "")

        self._fill_comp_levels(row, full_text)
        return self._apply_common_requireds(row)

    def _handle_allstar(self, url: str, soup: BeautifulSoup, meta: Dict[str, Any]) -> Dict[str, str]:
        row = _make_blank_row()
        container = soup.select_one("div.et_pb_text_inner")
        if not container:
            raise ValueError("AllStar detail content block not found")

        title = meta.get("listing_name", "")
        for h3 in container.find_all(["h2", "h3"], recursive=False):
            parts = [part.strip() for part in h3.stripped_strings if part.strip()]
            if len(parts) >= 2 and re.search(r"[A-Za-z]+\s+\d", parts[1]):
                title = parts[0]
                break

        full_text = container.get_text("\n", strip=True)
        city_match = re.search(r"\b([A-Za-z][A-Za-z .'-]+),\s*([A-Z]{2})\b", full_text)
        fallback_city = city_match.group(1).strip() if city_match else meta.get("listing_city", "")
        fallback_state = _state_full_name(city_match.group(2).strip()) if city_match else (meta.get("listing_state", "") or meta.get("selected_state", ""))

        locations: List[Dict[str, str]] = []
        for link in container.select("a[href*='google.com/maps'], a[href*='maps.google.com']"):
            href = (link.get("href") or "").strip()
            details = _extract_map_components(href)
            venue = details.get("Venue", "")
            parent_text = " ".join(link.parent.get_text("\n", strip=True).split())
            anchor_text = link.get_text(" ", strip=True)
            venue_match = re.search(rf"{re.escape(anchor_text)}\s*=\s*([^\n|]+)", parent_text)
            if venue_match:
                venue = venue_match.group(1).strip()
            locations.append(
                {
                    "Venue": venue,
                    "Address": details.get("Address", ""),
                    "City": details.get("City", "") or fallback_city,
                    "State-FullName": details.get("State-FullName", "") or fallback_state,
                    "Zip": details.get("Zip", ""),
                }
            )

        if not locations:
            raise ValueError("Missing required Google Maps location link")

        emails = _extract_emails(container)
        phone = _extract_phone(full_text)
        age_groups = _extract_age_groups(full_text)

        row["Tournament-name"] = title
        row["Start-date"] = meta.get("start_date", "")
        row["End-date"] = meta.get("end_date", "")
        row["Are-there-multiple_locations"] = "Yes" if len(locations) > 1 else "No"
        row["Venue"] = locations[0].get("Venue", "")
        row["Address"] = locations[0].get("Address", "")
        row["Address-2"] = ""
        row["City"] = locations[0].get("City", "")
        row["State-FullName"] = locations[0].get("State-FullName", "")
        row["Zip"] = locations[0].get("Zip", "")
        row["Guaranteed-games"] = _extract_guaranteed_games(full_text)
        row["Gate-fees"] = "NA"
        row["Hotel-required"] = "NA"
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "Artificial" if "turf" in full_text.lower() else "NA"
        row["Contact-name"] = ""
        row["Phone-number"] = phone
        row["Email"] = emails[0] if emails else ""
        row["Website-url"] = url

        fee_match = re.search(r"Cost:\s*\$([0-9][0-9,]*)", full_text, flags=re.I)
        fee = fee_match.group(1).replace(",", "") if fee_match else ""
        for idx, age in enumerate(age_groups[:13], start=1):
            row[f"Age-groups.{idx}"] = age
            row[f"Entry-fee.{idx}"] = fee

        return self._apply_common_requireds(row)

    def extract_event_details(self, url: str, driver, **kwargs) -> Dict:
        soup = _soup(self.session, url)
        meta = self.listing_meta.get(url, {})
        host = urlparse(url).netloc.lower()

        if host in TC_WEBFLOW_HOSTS:
            return self._handle_webflow_family(url, soup, meta)
        if host == "www.pathwaybaseball.com":
            return self._handle_pathway(url, soup, meta)
        if host in TC_WEEBLY_HOSTS:
            return self._handle_weebly(url, soup, meta)
        if host == "allstartournaments.com":
            return self._handle_allstar(url, soup, meta)

        raise ValueError(f"Unsupported TripleCrown domain: {host}")

    def get_event_key(self, url: str) -> str:
        return url
