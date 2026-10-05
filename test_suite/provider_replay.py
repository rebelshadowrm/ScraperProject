"""Deterministic provider replay. Capture is explicit and must precede edits."""
import argparse
import hashlib
import importlib.util
import json
import sys
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common.config import OUTPUT_COLUMNS
from common.identity import event_identity
from common.validation import validate_event_data
from common.normalize import normalize_row


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2099, 1, 1)


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2099, 1, 1, 12)
        return value.replace(tzinfo=tz) if tz else value


DETAIL = '''<hgroup><h1 class="mv-sm"><strong>Summer Classic</strong></h1>
<h3>Austin, TX</h3><h3>Central Park</h3><h3 class="inline-block m0">Jul 20-22</h3></hgroup>
<div id="event-details-body"><p>Tournament Profile</p><ul><li>AA</li></ul>
<p>Tournament Format</p><ul><li>3 game guarantee</li></ul></div>'''
CONTACT = '''<ul class="list-contacts"><li><h5 class="title">Alex Coach</h5>
<a href="mailto:alex@example.com">alex@example.com</a><a href="tel:5125550100">512-555-0100</a></li></ul>'''
LOCATIONS = '''<ul class="list-sites"><li data-title="Central Park" data-line1="100 Main St"
data-line2="Austin, TX 78701" data-state="TX"><h5 class="title">Central Park</h5></li>
<li data-title="North Park" data-line1="200 Main St" data-line2="Austin, TX 78702" data-state="TX">
<h5 class="title">North Park</h5></li></ul>'''
DIVISIONS = '''<div id="divisionsContainer"><div class="panel"><div class="panel-heading">
<div class="division">10U AA</div><div class="amount">$1,295</div></div>
<div class="locations"><div class="small text-muted">Gate fee $5</div></div></div>
<div class="panel"><div class="panel-heading"><div class="division">12U AAA</div>
<div class="amount">$650</div></div></div></div>'''
PG_DETAIL = '''<a id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventNameNew">Summer Classic 10U (AA)</a>
<span id="ContentTopLevel_ContentPlaceHolder1_lblEventDateNew">Jul 20-22</span>
<div id="pnlContactPG"><span id="lblDirectorName">Alex Coach</span>
<a href="mailto:alex@example.com">alex@example.com</a><span id="lblDirectorPhone">512-555-0100</span></div>
<span id="ContentTopLevel_ContentPlaceHolder1_lblGamesGuaranteed">3 Games</span>'''
PG_LOCATION = '''<span id="rptLocations_lblBallParkNames_0">Central Park</span>
<span id="rptLocations_lblBallParkAddresses_0">100 Main St, Austin, TX 78701</span>'''
TC_LISTING = '''<div class="event-row"><a class="event-name" href="https://www.triplecrownbaseball.com/classic">Summer Classic</a>
<div class="event-date-container"><div>20</div><div>Jul</div><div>2099</div></div>
<div class="event-details"><a class="font-red">July 20-22, 2099</a><a class="font-red">Austin, TX</a></div></div>'''
TC_DETAIL = '''<h1>Summer Classic</h1><div class="event-details-info">
<div class="text-row"><div class="item-title">Division</div><div class="info-item-title">10U AA, 12U AAA</div></div>
<div class="text-row"><div class="item-title">Price</div><div class="info-item-title">$1,295</div></div>
<div class="text-row"><div class="item-title">Format</div><div class="info-item-title">3 game guarantee</div></div></div>
<div class="event-details-item"><p>Alex Coach</p><p>alex@example.com</p><p>512-555-0100</p></div>'''


def module(name, source_root=None):
    path = (Path(source_root) if source_root else ROOT / "scrapers") / f"{name}_scraper.py"
    if name == "play365_base":
        path = (Path(source_root) if source_root else ROOT / "scrapers") / "play365_base.py"
    spec = importlib.util.spec_from_file_location(f"scrapers.replay_{name}", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def pack(scraper, links, rows):
    if any(row is None for row in rows):
        raise AssertionError(f"{scraper.SCRAPER_NAME} fixture failed to extract")
    export_rows = []
    for row in rows:
        validated = dict(row)
        valid, reason = validate_event_data(validated)
        if not valid:
            raise AssertionError(f"{scraper.SCRAPER_NAME} fixture lacks required output: {reason}")
        export_rows.append(normalize_row(validated))
    return {"links": list(links), "identities": [event_identity(scraper, u) for u in links],
            "rows": [{c: row.get(c, "") for c in OUTPUT_COLUMNS} for row in rows],
            "export_rows": [{c: row.get(c, "") for c in OUTPUT_COLUMNS} for row in export_rows]}


def replay(source_root=None):
    data, metrics = {}, {}
    with ExitStack() as global_stack:
        # Every HTTP operation not explicitly replayed is an error.
        global_stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Unreplayed HTTP")))
        for name, clsname in (("game7baseball", "Game7BaseballScraper"), ("grandslam", "GrandSlamScraper"),
                              ("ncs", "NCSScraper"), ("pg", "PGScraper"), ("triplecrown", "TripleCrownScraper")):
            mod = module(name, source_root)
            scraper = getattr(mod, clsname)()
            calls = []
            url = "https://example.com/baseball/Events/Details/123/classic"
            if name == "ncs":
                url = "https://playncs.com/baseball/Events/Details/123/classic"
            if name == "pg":
                url = "https://www.perfectgame.org/Events/Default.aspx?event=123"
            if name == "triplecrown":
                url = "https://www.triplecrownbaseball.com/classic"
            base = "https://example.com/baseball/Events?state=TX"
            gs_listing = '''<div><a href="/baseball/Events/Details/123/classic">Event Info</a><div class="badges">
<div class="ages"><button class="badge">10U</button><button class="badge">12U</button></div>
<div class="divisions"><button class="badge">AA</button><button class="badge">AAA</button></div>3 game guarantee</div></div>'''
            g7_listing = '''<div class="media-list media-list-events"><div class="media"><div class="media-body">
<div class="h4"><a href="/baseball/Events/Details/123/classic">Summer Classic</a></div><div class="h4">Jul 20-22 2099</div>
<div class="h6">Austin, TX | Central Park</div><div class="h6">10U 12U</div><ul class="tags"><span class="label">Turf</span>
<span class="label">Stay to Play</span></ul></div></div></div>'''
            def html(target):
                low = target.lower()
                if "getfilteredeventschedule" in low:
                    return TC_LISTING
                if low.endswith("/classic") and name == "triplecrown":
                    return TC_DETAIL
                if "/locations" in low:
                    if name == "pg":
                        return PG_LOCATION
                    if name == "grandslam":
                        return "<p>Central Park</p><p>100 Main St</p><p>Austin, TX 78701</p><p>North Park</p><p>200 Main St</p><p>Austin, TX 78702</p>"
                    return LOCATIONS
                if "/contacts/" in low:
                    return CONTACT
                if "/divisions/" in low:
                    return DIVISIONS
                if "/details/" in low or "default.aspx" in low:
                    return PG_DETAIL if name == "pg" else DETAIL
                return gs_listing if name == "grandslam" else g7_listing
            def get(target, *args, **kwargs):
                calls.append(target)
                response = MagicMock(text=html(target), status_code=200, ok=True, url=target)
                return response
            driver = MagicMock()
            def navigate(target):
                calls.append(target)
                driver.current_url = target
                driver.page_source = html(target)
            driver.get.side_effect = navigate
            with ExitStack() as stack:
                stack.enter_context(patch.object(mod.requests, "get", side_effect=get)) if hasattr(mod, "requests") else None
                stack.enter_context(patch("requests.Session.get", side_effect=get))
                if hasattr(mod, "date"):
                    stack.enter_context(patch.object(mod, "date", FixedDate))
                if hasattr(mod, "datetime"):
                    stack.enter_context(patch.object(mod, "datetime", FixedDateTime))
                if hasattr(mod, "WebDriverWait"):
                    stack.enter_context(patch.object(mod, "WebDriverWait", return_value=MagicMock()))
                if name in {"game7baseball", "grandslam"}:
                    links = scraper.collect_event_links(None, base)
                    rows = [scraper.extract_event_details(u, None) for u in links]
                elif name == "triplecrown":
                    links = scraper.collect_event_links(None, "https://components.triplecrownsports.com/Components/GetFilteredEventSchedule/?sportID=1&state=TX")
                    if not links:
                        raise AssertionError("TripleCrown listing fixture must contain an event")
                    rows = [scraper.extract_event_details(u, None) for u in links]
                elif name == "ncs":
                    links = scraper.collect_event_links(driver, base, pause=0)
                    rows = [scraper.extract_event_details(u, driver, selected_location="Texas") for u in links]
                else:
                    links = [url]
                    rows = [scraper.extract_event_details(url, driver, selected_location="Texas")]
            data[scraper.SCRAPER_NAME] = pack(scraper, links, rows)
            metrics[scraper.SCRAPER_NAME] = {"loads": len(calls)}
            if name == "pg":
                listing_driver = MagicMock(page_source='<table class="rgMasterTable"><tr class="rgGroupHeader"><a href="GroupedEvents.aspx?gid=123">Classic</a></tr></table>')
                listing_driver.find_elements.return_value = []
                with patch.object(mod, "WebDriverWait", return_value=MagicMock()):
                    listing_links = scraper.collect_event_links(listing_driver, "https://www.perfectgame.org/Schedule/", pause=0)
                if not listing_links or not getattr(listing_links, "complete", True):
                    raise AssertionError("PerfectGame listing fixture must collect a complete group")
                data[scraper.SCRAPER_NAME]["listing_links"] = list(listing_links)

        mod = module("usssa", source_root)
        scraper = mod.USSSAScraper()
        driver = MagicMock(page_source='<a href="https://txbaseball.usssa.com/event/classic/">Classic</a>')
        with patch.object(scraper, "_wait_for_listing_state"), patch.object(mod.time, "sleep"):
            links = scraper.collect_event_links(driver, "https://txbaseball.usssa.com/events/", pause=0)
        data["USSSA"] = pack(scraper, links, [])
        metrics["USSSA"] = {"loads": driver.get.call_count}

        mod = module("pst", source_root)
        scraper = mod.PSTScraper()
        event = {"Id": 123, "Name": "Summer Classic", "StartDate": "2099-07-20T00:00:00.000", "EndDate": "2099-07-22T00:00:00.000",
                 "Location": "Central Park", "StreetAddress": "100 Main St", "City": "Austin", "StateRegion": "TX", "PostalCode": "78701",
                 "YouthAgeGradesMale": "10,12", "GamesGuaranteed": 3, "ContactName": "Alex Coach", "ContactEmail": "alex@example.com", "ContactPhone": "5125550100"}
        url = f"{scraper.EVENT_BASE_URL}?eventid=123"
        scraper.store_org_id = "456"
        scraper.events_by_url[url] = event
        with patch.object(scraper.session, "get", return_value=MagicMock(json=lambda: {"Teams": [{"Division": "10U AA"}]})):
            row = scraper.extract_event_details(url, None)
        data["PST"] = pack(scraper, [url], [row])
        incomplete_address = {**event, "StreetAddress": "", "City": "", "StateRegion": "", "PostalCode": "",
                              "Latitude": 30.1, "Longitude": -97.1}
        geo_location = MagicMock(raw={"address": {"house_number": "100", "road": "Main St", "city": "Austin",
                                                   "state": "Texas", "postcode": "78701", "amenity": "Geocoded Park"}})
        with patch.object(scraper, "reverse_func", return_value=geo_location) as reverse:
            data["PST"]["geocode_rows"] = [scraper.build_address_and_venue(incomplete_address) for _ in range(3)]
            metrics["PST"] = {"geocode_lookups": reverse.call_count}

        mod = module("topgun", source_root)
        scraper = mod.TopGunScraper()
        tournament = {"TournamentID": 123, "TournamentName": "Summer Classic", "StartDate": "2099-07-20", "EndDate": "2099-07-22",
                      "LocationID": 1, "SportID": 1, "TournamentTypeID": 1, "GameGuarantee": 3, "FullName": "Alex Coach",
                      "Email": "alex@example.com", "CellPhone": "5125550100", "TFeeObj": json.dumps([{"Fee": "1295", "AgeGroups": ["10U", "12U"]}]),
                      "TComplexArr": json.dumps([{"name": "Central Park", "location": "100 Main St, Austin, TX 78701"}])}
        filters = {"locations": [{"LocationID": 1, "CityState": "Austin, TX"}], "types": [{"TournamentTypeID": 1, "TournamentTypeName": "Team"}]}
        with patch.object(scraper, "_get", side_effect=lambda u: filters if u == scraper.FILTER_DATA_URL else [tournament]), patch.object(mod, "date", FixedDate):
            links = scraper.collect_event_links(None, "https://topgunstats.com/tournaments?state=TX&sport=1")
            rows = [scraper.extract_event_details(u, None) for u in links]
        data["TopGun"] = pack(scraper, links, rows)

        mod = module("ripkenbaseball", source_root)
        scraper = mod.RipkenBaseballScraper()
        programs = [{"programId": i, "masterProgramId": 10 if i != 10 else 0, "name": label,
                     "programUrlHtml": f"https://example.com/tournaments/{i}", "startTime": 4088232000000, "endTime": 4088404800000,
                     "sportId": 1, "teamFee": 1295, "experienceLevel": "3-Game", "season": "2099"}
                    for i, label in ((10, "Summer Classic"), (11, "10U"), (12, "12U"), (13, "10U Waitlist"))]
        with patch.object(scraper.session, "get", return_value=MagicMock(json=lambda: programs)), patch.object(mod, "datetime", FixedDateTime):
            links = scraper.collect_event_links(None, "https://ripkenbaseball.com/?siteId=1&apiKey=fixture&siteName=Aberdeen")
            rows = [scraper.extract_event_details(u, None) for u in links]
        data["RipkenBaseball"] = pack(scraper, links, rows)
        selection_cases = [
            [{"programId": 1, "name": "Parent"}],
            [{"programId": 1, "name": "Parent"}, {"programId": 2, "name": "10U"}],
            [{"programId": 1, "name": "Parent"}, {"programId": 2, "name": "10U Waitlist"}],
            [{"programId": 1, "name": "Parent"}, {"programId": 1, "name": "10U"}],
            [{"programId": 1, "name": "10U"}, {"programId": 2, "name": "12U"}],
            [{"programId": 1, "name": "Waitlist"}],
        ]
        data["RipkenBaseball"]["selection_cases"] = [scraper._display_programs({1: case}) for case in selection_cases]

        mod = module("play365_base", source_root)
        # Use each actual subclass configuration with the frozen/current shared base.
        configs = [("fivetool", "FiveToolPlay365Scraper"), ("fivetoolyouth", "FiveToolYouthScraper"),
                   ("play24sports", "Play24SportsScraper"), ("playpps", "PlayPPSScraper"), ("playrbi", "PlayRBIScraper"),
                   ("prospectwire", "ProspectWireScraper"), ("twodsportshighschool", "TwoDSportsHighSchoolScraper"),
                   ("twodsportsyouth", "TwoDSportsYouthScraper")]
        event = {"url_key": "classic", "name": "Summer Classic", "start_date": "07/20/2099", "end_date": "07/22/2099", "state": "TX",
                 "game_guarantee": 3, "stay_to_play": 1, "sport_id": 1,
                 "venues": [{"name": "Central Park", "address1": "100 Main St", "city": "Austin", "state": "TX", "postal_code": "78701"},
                            {"name": "North Park", "address1": "200 Main St", "city": "Austin", "state": "TX", "postal_code": "78702"}],
                 "contacts": [{"first_name": "Alex", "last_name": "Coach", "mobile": "5125550100", "email": "alex@example.com"}],
                 "divisions": [{"label": "10U", "price": "1295", "team_levels": [{"label": "AA", "price": "1200"}, {"label": "AAA", "price": "1295"}]},
                               {"label": "12U", "price": "650", "level": "Open"}]}
        for filename, clsname in configs:
            path = (Path(source_root) if source_root else ROOT / "scrapers") / "play365" / f"{filename}_scraper.py"
            scope = {"__name__": f"replay_{filename}"}
            # Imports resolve against this exact version of the shared base.
            with patch.dict(sys.modules, {"scrapers.play365_base": mod}):
                exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), scope)
            scraper = scope[clsname]()
            source_events = [event, {**event, "url_key": "other", "state": "CA"},
                             {**event, "url_key": "venue-fallback", "state": ""},
                             {**event, "url_key": "state-precedence", "venues": [{**event["venues"][0], "state": "CA"}]},
                             {**event, "url_key": "classic", "name": "Summer Classic Updated"},
                             {**event, "url_key": "", "name": "Missing URL"},
                             {**event, "url_key": "unknown-state", "state": "", "venues": []}]
            scraper._load_events_from_blob({"events": source_events}, "fixture")
            links = scraper.collect_event_links(None, "fixture.json?state=TX")
            data[scraper.SCRAPER_NAME] = pack(scraper, links, [scraper.extract_event_details(u) for u in links])
    return {"data": data, "metrics": metrics}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.source_root)
    source = args.source_root or ROOT / "scrapers"
    result["provenance"] = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source.rglob("*.py"))}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Replayed {len(result['data'])} providers to {args.output}")
