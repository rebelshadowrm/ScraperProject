"""Uncapped event discovery within verified RegisterPlay state searches.

Every state has an independent outcome. Unsupported provider searches remain
explicit gaps; the small development sample limit is never a production limit.
"""
import json
from datetime import date, datetime
from common.collection import CollectedLinks, validated_records
from common.softball_api import fetch_listing
from common.softball_rows import city_state, source_date, source_state
from common.source_exclusions import SourceEventIncomplete


REGISTERPLAY = ("usa_fastpitch", "usa_softball", "nsa_fastpitch")
NATIONAL_STRATEGIES = {**{name: "registerplay_us_states" for name in REGISTERPLAY}, "pgf": "tournamentconnect_pages"}
NATIONAL_STRATEGIES.update(fasa='all_dates_all_states_table',topgun_events='tagup_published_pages',
    triplecrown_fastpitch='published_fastpitch_schedule',usssa_fastpitch='published_state_directory_pages',
    usfa='published_state_directory',alliance_fastpitch='source_season_all_league_pages')
NATIONAL_STRATEGIES['pg_softball']='published_form_and_server_paging'


def discover_pgf(scraper, *, max_pages=200):
    from common.softball_tournamentconnect import tc_read, TC_LISTING
    from common.sport import payload_revision
    scraper._tc_payloads = {}
    scraper.discovery_revisions = {}
    links, seen, pages = [], set(), 0
    error = "PGF page budget exhausted before the source ended"
    for page in range(max_pages):
        try:
            data = tc_read(dict(providerid=76, eventtypeid=1, eventids=None, sportid=4,
                sort="startdate", limit=5, offset=page * 5, entityid=None, entity="team",
                historylabel=False, futurelabel=True, fltr=False, filters={"visible": 1}), encoded=True)
            rows = validated_records(list(data.values()) if isinstance(data, dict) else data, source="PGF national page")
            pages += 1
            if len(rows) > 5:
                raise ValueError("PGF page exceeded the verified source limit")
            for payload in rows:
                event = payload.get("event", {})
                identity = event.get("eventid")
                if type(identity) is not int or identity <= 0 or identity in seen:
                    raise ValueError("PGF page repeated or omitted a stable identity")
                seen.add(identity)
                url = TC_LISTING + f"#tc-event={identity}"
                links.append(url)
                scraper._tc_payloads[url] = payload
                scraper.discovery_revisions[url] = payload_revision({key: event.get(key) for key in
                    ("eventid", "modified", "startdate", "enddate", "name", "pricinglabel", "divisionlabel", "visible")})
            if len(rows) < 5:
                error = ""
                break
        except Exception as exc:
            error = type(exc).__name__ + ": PGF pagination incomplete"
            break
    result = CollectedLinks(links, complete=not error, error=error, pages=pages)
    result.held = []
    result.source_total = len(links) if not error else None
    return result


def discover_state(scraper, state, *, today=None, capture=None):
    today = today or date.today()
    scraper.configure_search(state=state, period=f"d|{today.month}|{today.year}")
    raw = fetch_listing(f"https://{scraper.EVENT_HOST}/api/v1/Event/search", content_type="json",
                        method="POST", json_body=scraper.discovery_filters)
    if capture:
        capture.write_text(raw, encoding="utf-8")
    records = validated_records(json.loads(raw), source="National RegisterPlay state search")
    accepted, held = [], []
    counts = {'source_records':len(records), 'other_sport':0, 'not_future':0,
              'unresolved_date':0, 'unresolved_location':0}
    for record in records:
        sport = record.get("sport")
        if sport in scraper.OTHER_SPORTS:
            counts['other_sport'] += 1
            continue
        if sport != "FP":
            raise ValueError("State search returned an unverified sport")
        # The source search begins at a calendar month. Source dates provide the
        # stricter future boundary, independently of delivery acknowledgements.
        try:
            if datetime.strptime(source_date(record.get("starts")), "%m/%d/%Y").date() <= today:
                counts['not_future'] += 1
                continue
        except (ValueError, TypeError, SourceEventIncomplete):
            counts['unresolved_date'] += 1
            held.append({"source_id": record.get("_key"), "reason": "Source start date is missing or ambiguous"})
            continue
        try:
            city, location_state = scraper.location_parts(record.get("location"))
        except SourceEventIncomplete:
            city, location_state = "", ""
        if not city or location_state not in {source_state(code) for code in scraper.scope_states(state)}:
            counts['unresolved_location'] += 1
            held.append({"source_id": record.get("_key"), "reason": "Source city/state does not match the requested state"})
            continue
        accepted.append(record)
    links, revisions = [], {}
    # Reuse the existing validated provider parser for every record, in chunks
    # matching its fixture-tested input bound. No records are silently clipped.
    for offset in range(0, len(accepted), 25):
        chunk = scraper.parse_listing(json.dumps(accepted[offset:offset + 25]), scraper.DISCOVERY_URL, max_events=25)
        links.extend(chunk)
        revisions.update(scraper.discovery_revisions)
    scraper.discovery_revisions = revisions
    result = CollectedLinks(list(dict.fromkeys(links)), complete=not held, pages=1,
                            error="Source locations require review" if held else "")
    result.held = held
    result.source_total = len(records)
    counts.update(selected_records=len(accepted), discovered_identities=len(result),
                  duplicate_records=len(links)-len(result))
    result.filter_counts = counts
    return result
