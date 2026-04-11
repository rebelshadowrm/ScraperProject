import logging
import re
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
from us import states

from common.config import OUTPUT_COLUMNS
from scrapers.base_scraper import BaseScraper

logger = logging.getLogger(__name__)


def _fmt_epoch_ms(epoch_ms) -> str:
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000)
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return ""


def _state_full_name(state_value: str) -> str:
    raw = (state_value or "").strip()
    if not raw:
        return ""
    state_obj = states.lookup(raw)
    return state_obj.name if state_obj else raw


def _extract_age_groups(*values: str) -> List[str]:
    age_groups: List[str] = []
    seen = set()
    for value in values:
        for match in re.finditer(r"\b(\d{1,2})\s*[Uu]\b", value or ""):
            age_num = int(match.group(1))
            if 6 <= age_num <= 18:
                token = f"{age_num}u"
                if token not in seen:
                    age_groups.append(token)
                    seen.add(token)
    return age_groups


def _extract_age_groups_from_standings_html(html: str) -> List[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    age_groups: List[str] = []
    seen = set()

    for header in soup.select(".master-standings_division h2.context > span"):
        text = header.get_text(" ", strip=True)
        if not text or "waitlist" in text.lower():
            continue
        for age in _extract_age_groups(text):
            if age not in seen:
                seen.add(age)
                age_groups.append(age)

    return age_groups


def _extract_guaranteed_games(value: str) -> str:
    match = re.search(r"(\d+)\s*-\s*Game|(\d+)\s*Game", value or "", re.IGNORECASE)
    if not match:
        return ""
    return next((grp for grp in match.groups() if grp), "")


def _is_waitlist_program(program: Dict) -> bool:
    name = (program.get("name") or "").strip().lower()
    return "waitlist" in name


def _program_group_id(program: Dict) -> int:
    try:
        master_id = int(program.get("masterProgramId") or 0)
    except Exception:
        master_id = 0
    try:
        program_id = int(program.get("programId") or 0)
    except Exception:
        program_id = 0
    return master_id or program_id


def _clean_html_text(value: str) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    return soup.get_text("\n", strip=True)


def _parse_description_location(description_html: str) -> Dict[str, str]:
    text = _clean_html_text(description_html)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    venue = ""
    address = ""
    city = ""
    state_full = ""
    zip_code = ""

    city_state_zip_idx = None
    city_state_zip_match = None
    for idx, line in enumerate(lines):
        match = re.search(r"(.+?),\s*([A-Z]{2})\s+(\d{5})(?:-\d{4})?$", line)
        if match:
            city_state_zip_idx = idx
            city_state_zip_match = match
            break

    if city_state_zip_match is not None and city_state_zip_idx is not None:
        city = city_state_zip_match.group(1).strip()
        state_full = _state_full_name(city_state_zip_match.group(2))
        zip_code = city_state_zip_match.group(3)
        if city_state_zip_idx > 0:
            address = lines[city_state_zip_idx - 1]
        if city_state_zip_idx > 1:
            venue = lines[city_state_zip_idx - 2]

    return {
        "Venue": venue,
        "Address": address,
        "City": city,
        "State-FullName": state_full,
        "Zip": zip_code,
        "DescriptionText": text,
    }


class RipkenBaseballScraper(BaseScraper):
    SCRAPER_NAME = "RipkenBaseball"
    REQUIRES_DRIVER = False
    BATCH_DELAY_SECONDS = 0
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "dynamic"
    DYNAMIC_LOCATIONS = True

    WIDGET_CONFIG_URL = "https://us-central1-leagueappwidget.cloudfunctions.net/getWidgetConfig"
    WIDGET_ID = "4d75f548-3c95-4945-8a81-88b47c1671e1"
    PROGRAMS_BASE_URL = "https://public.leagueapps.io/v1/sites/{site_id}/programs/current"
    SITE_LOCATION_FALLBACKS = {
        "Pigeon Forge": {
            "Venue": "The Ripken Experience Pigeon Forge",
            "Address": "405 Jake Thomas Road",
            "City": "Pigeon Forge",
            "State-FullName": "Tennessee",
            "Zip": "37863",
            "Phone-number": "865-366-3101",
        },
        "Myrtle Beach": {
            "Venue": "The Ripken Experience Myrtle Beach",
            "Address": "3051 Ripken Way",
            "City": "Myrtle Beach",
            "State-FullName": "South Carolina",
            "Zip": "29577",
            "Phone-number": "843-913-5278",
        },
        "Aberdeen": {
            "Venue": "The Ripken Experience Aberdeen",
            "Address": "873 Long Dr",
            "City": "Aberdeen",
            "State-FullName": "Maryland",
            "Zip": "21001",
            "Phone-number": "410-306-7599",
        },
        "Elizabethtown": {
            "Venue": "Elizabethtown Sports Park",
            "Address": "1401 West Park Rd",
            "City": "Elizabethtown",
            "State-FullName": "Kentucky",
            "Zip": "42701",
        },
        "Sandusky": {
            "Venue": "Sports Force Parks at Cedar Point Sports Center",
            "Address": "3115 Cleveland Rd W",
            "City": "Sandusky",
            "State-FullName": "Ohio",
            "Zip": "44870",
            "Phone-number": "844-737-2757",
        },
    }

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0.0.0 Safari/537.36"
                ),
            }
        )
        self.events_by_url: Dict[str, Dict] = {}
        self._sites_cache: Optional[List[Dict]] = None

    def _load_widget_sites(self) -> List[Dict]:
        if self._sites_cache is not None:
            return self._sites_cache

        response = self.session.get(self.WIDGET_CONFIG_URL, params={"id": self.WIDGET_ID}, timeout=30)
        response.raise_for_status()
        data = response.json()
        sites_info = data.get("sitesInfo", [])
        self._sites_cache = [site for site in sites_info if site.get("siteId") and site.get("apiKey")]
        return self._sites_cache

    def get_dynamic_locations(self) -> Dict[str, str]:
        dynamic_locations: Dict[str, str] = {}
        for site in self._load_widget_sites():
            site_name = site.get("siteName", "").strip()
            label = f"{site_name} ({self.SCRAPER_NAME})"
            query = urlencode(
                {
                    "siteId": site.get("siteId", ""),
                    "apiKey": site.get("apiKey", ""),
                    "siteName": site_name,
                }
            )
            dynamic_locations[label] = f"https://ripkenbaseball.com/?{query}"
        return dynamic_locations

    def _site_params_from_base_url(self, base_url: str) -> Dict[str, str]:
        parsed = urlparse(base_url)
        params = parse_qs(parsed.query)
        return {
            "siteId": params.get("siteId", [""])[0],
            "apiKey": params.get("apiKey", [""])[0],
            "siteName": params.get("siteName", [""])[0],
        }

    def _program_url(self, program: Dict) -> str:
        html_url = (program.get("programUrlHtml") or "").strip()
        if not html_url:
            return ""
        if html_url.startswith("//"):
            return f"https:{html_url}"
        if html_url.startswith("/"):
            return f"https://ripkenbaseball.com{html_url}"
        return html_url

    def _standings_url(self, event_url: str) -> str:
        match = re.search(r"/tournaments/(\d+)", event_url)
        if not match:
            return ""
        parsed = urlparse(event_url)
        return f"{parsed.scheme}://{parsed.netloc}/tournaments/{match.group(1)}/standings"

    def _extract_standings_age_groups(self, event_url: str) -> List[str]:
        standings_url = self._standings_url(event_url)
        if not standings_url:
            return []
        try:
            response = self.session.get(standings_url, timeout=30)
            response.raise_for_status()
        except Exception:
            return []
        return _extract_age_groups_from_standings_html(response.text)

    def _build_parent_title_map(self, grouped_programs: Dict[int, List[Dict]]) -> Dict[int, str]:
        parent_titles: Dict[int, str] = {}
        for group_id, programs in grouped_programs.items():
            named_parents = [
                (program.get("name") or "").strip()
                for program in programs
                if not _extract_age_groups(program.get("name", ""))
                and not _is_waitlist_program(program)
            ]
            named_parents = [name for name in named_parents if name]
            if named_parents:
                parent_titles[group_id] = named_parents[0]
        return parent_titles

    def _is_leaf_program(self, program: Dict, grouped_programs: Dict[int, List[Dict]]) -> bool:
        group_id = _program_group_id(program)
        group = grouped_programs.get(group_id, [])
        has_age_named_child = any(
            _extract_age_groups(candidate.get("name", ""))
            for candidate in group
            if int(candidate.get("programId") or 0) != int(program.get("programId") or 0)
        )
        if has_age_named_child and not (
            _extract_age_groups(program.get("name", ""))
        ):
            return False
        return True

    def _build_display_name(self, program: Dict, parent_title: str) -> str:
        name = (program.get("name") or "").strip()
        if not name:
            return parent_title
        if not parent_title or parent_title == name:
            return name
        if name in parent_title:
            return parent_title
        return f"{parent_title} - {name}"

    def _display_programs(self, grouped_programs: Dict[int, List[Dict]]) -> List[Dict]:
        parent_titles = self._build_parent_title_map(grouped_programs)
        selected_programs: List[Dict] = []

        for group_id, programs in grouped_programs.items():
            parent_title = parent_titles.get(group_id, "")
            non_waitlist = [program for program in programs if not _is_waitlist_program(program)]
            if not non_waitlist:
                continue

            leaf_programs = [
                program for program in non_waitlist
                if self._is_leaf_program(program, grouped_programs)
            ]
            chosen_programs = leaf_programs or non_waitlist

            for program in chosen_programs:
                program_copy = dict(program)
                program_copy["_parent_tournament_name"] = parent_title
                program_copy["_display_name"] = self._build_display_name(program_copy, parent_title)
                program_copy["_group_names"] = [
                    (candidate.get("name") or "").strip()
                    for candidate in programs
                    if (candidate.get("name") or "").strip()
                ]
                selected_programs.append(program_copy)

        return selected_programs

    def collect_event_links(self, driver, base_url: str, **kwargs) -> List[str]:
        site_info = self._site_params_from_base_url(base_url)
        site_id = site_info["siteId"]
        api_key = site_info["apiKey"]
        site_name = site_info["siteName"]

        if not site_id or not api_key:
            logger.error("RipkenBaseball missing siteId/apiKey in base URL: %s", base_url)
            return []

        url = self.PROGRAMS_BASE_URL.format(site_id=site_id)
        response = self.session.get(url, params={"x-api-key": api_key}, timeout=30)
        response.raise_for_status()
        programs = response.json() or []

        grouped_programs: Dict[int, List[Dict]] = {}
        for program in programs:
            if program.get("deleted"):
                continue
            if str(program.get("sportId", "")) not in {"1", ""} and (program.get("sport") or "").strip().lower() != "baseball":
                continue

            program_copy = dict(program)
            program_copy["_site_name"] = site_name
            program_copy["_source_base_url"] = base_url
            grouped_programs.setdefault(_program_group_id(program_copy), []).append(program_copy)

        today = datetime.now().date()
        collected_urls: List[str] = []
        self.events_by_url = {
            url: payload for url, payload in self.events_by_url.items()
            if payload.get("_site_name") != site_name
        }

        for program in self._display_programs(grouped_programs):
            event_url = self._program_url(program)
            if not event_url:
                continue

            start_date_str = _fmt_epoch_ms(program.get("startTime"))
            try:
                start_dt = datetime.strptime(start_date_str, "%m/%d/%Y").date() if start_date_str else None
            except Exception:
                start_dt = None

            if start_dt and start_dt <= today:
                continue

            self.events_by_url[event_url] = program
            collected_urls.append(event_url)

        logger.info(
            "[RipkenBaseball] Site '%s' returned %d future baseball program(s).",
            site_name,
            len(collected_urls),
        )
        return collected_urls

    def _build_row(self, event_url: str, program: Dict) -> Dict:
        row = {col: "" for col in OUTPUT_COLUMNS}

        site_name = program.get("_site_name", "")
        name = (program.get("_display_name") or program.get("name") or "").strip()
        season = (program.get("season") or "").strip()
        experience_level = (program.get("experienceLevel") or "").strip()

        tournament_name = " - ".join(part for part in [site_name, name] if part)
        row["Tournament-name"] = tournament_name or name or site_name
        row["Start-date"] = _fmt_epoch_ms(program.get("startTime"))
        row["End-date"] = _fmt_epoch_ms(program.get("endTime"))
        row["Are-there-multiple_locations"] = "No"
        row["Website-url"] = event_url
        row["Tournament-type"] = "Team"
        row["Tournament-surface"] = "NA"
        row["Hotel-required"] = "NA"
        row["Gate-fees"] = "NA"
        row["More-information-and-additional-tournament-locations"] = _clean_html_text(program.get("description") or "")

        parsed_location = _parse_description_location(program.get("description") or "")
        fallback_location = self.SITE_LOCATION_FALLBACKS.get(site_name, {})

        row["Venue"] = (
            parsed_location.get("Venue")
            or fallback_location.get("Venue", "")
            or site_name
        )
        row["Address"] = (
            (program.get("locationAddress1") or "").strip()
            or parsed_location.get("Address", "")
            or fallback_location.get("Address", "")
        )
        row["Address-2"] = (program.get("locationAddress2") or "").strip()
        row["City"] = (
            (program.get("locationCity") or "").strip()
            or parsed_location.get("City", "")
            or fallback_location.get("City", "")
        )
        state_value = (
            _state_full_name((program.get("locationState") or "").strip())
            or parsed_location.get("State-FullName", "")
            or fallback_location.get("State-FullName", "")
        )
        row["State-FullName"] = state_value
        row["Zip"] = (
            (program.get("locationZip") or "").strip()
            or parsed_location.get("Zip", "")
            or fallback_location.get("Zip", "")
        )

        team_fee = program.get("teamFee")
        if team_fee not in (None, ""):
            fee_str = str(int(team_fee) if isinstance(team_fee, (int, float)) and int(team_fee) == team_fee else team_fee)
        else:
            fee_str = ""

        age_fee_pairs = []
        fallback_age_groups = _extract_age_groups(
            name,
            str(program.get("subProgram") or ""),
            parsed_location.get("DescriptionText", ""),
            *program.get("_group_names", []),
        )
        if not fallback_age_groups:
            fallback_age_groups = self._extract_standings_age_groups(event_url)
        age_fee_pairs = [(age_group, fee_str) for age_group in fallback_age_groups]

        for idx, (age_group, entry_fee) in enumerate(age_fee_pairs[:13], start=1):
            row[f"Age-groups.{idx}"] = age_group
            row[f"Entry-fee.{idx}"] = entry_fee

        guaranteed_games = _extract_guaranteed_games(experience_level)
        row["Guaranteed-games"] = guaranteed_games

        row["Contact-name"] = ""
        row["Phone-number"] = fallback_location.get("Phone-number", "")
        row["Email"] = ""

        # Preserve some useful context in otherwise underused fields.
        if season:
            row["Sponsor"] = season
        if experience_level:
            row["Location"] = program.get("location", "") or experience_level

        has_age_groups = any((row.get(f"Age-groups.{idx}") or "").strip() for idx in range(1, 14))
        if not has_age_groups:
            raise ValueError("Missing required age group data")

        return row

    def extract_event_details(self, url: str, driver, **kwargs) -> Dict:
        program = self.events_by_url.get(url)
        if not program:
            logger.error("RipkenBaseball event not cached for URL: %s", url)
            return {}
        return self._build_row(url, program)

    def get_event_key(self, url: str) -> str:
        program = self.events_by_url.get(url)
        if program and program.get("programId"):
            return str(program["programId"])

        match = re.search(r"/tournaments/(\d+)", url)
        if match:
            return match.group(1)
        return url
