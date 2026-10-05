# Scraper optimization review

Reviewed the current working tree on October 4, 2026, including the earlier full-pull improvements. Scope: all 17 concrete providers, the shared Play365 implementation, and the discovery/extraction contracts consumed by the runner. This is a source review and proposed implementation sequence; scraper behavior was not changed and no production pull was run.

The project has useful patterns that can be transferred between providers, especially persistent HTTP sessions, validated source caches, listing metadata reuse, and explicit collection outcomes. Share those mechanisms while retaining each provider's parsing and source semantics. File age alone does not establish which implementation is better.

The seasonal default remains refresh plus a fresh snapshot, with event start date strictly later than today and any supplied start/end bounds. Optimizations must retain provider identities, location membership, the 56 import columns, and durable export replay.

## Recommended sequence

| Order | Change | Why first | Risk / verification gate |
| --- | --- | --- | --- |
| 1 | Close remaining discovery and fallback completeness gaps | Fast output is useful only if missing coverage is visible | Provider-specific valid-empty fixtures, malformed responses, repeated pages, and interrupted exports |
| 2 | Reuse pages already loaded in USSSA and PerfectGame | Clear redundant loads; no new endpoint or parser required | Same links/rows, redirect handling, and browser recovery under mocked navigation |
| 3 | Persistent HTTP sessions and bounded GET retries for Game7, GrandSlam, TopGun; consistent lifecycle for existing sessions | Transfers an existing PerfectGame pattern to repeated HTTP work | Preserve headers/cookies; bound total retry time; test 429/5xx/timeouts and session cleanup |
| 4 | PST coordinate cache; Play365 and optionally TopGun source indexes | Avoid repeated work within one run without changing data acquisition | Same field precedence and ordering; cache successful empty results separately from failures |
| 5 | Skip expensive detail enrichment for definitively out-of-scope events | Can save several requests per event; benefit depends on actual listing age mix | Trustworthy explicit dates only; unknown/ambiguous dates still take the existing path |
| 6 | Trial HTTP-first NCS details and selected PerfectGame pages; tune browser waits | Potentially larger gains, but depends on real rendering and response parity | Saved fixtures plus bounded live smoke comparison before making a default change |

Implement one provider or narrowly shared mechanism at a time. Compare identities, link coverage, and all output fields before comparing speed. Add useful request counters and phase timings to each trial; do not infer elapsed full-season savings from source inspection.

## Remaining correctness foundations

### Explicitly distinguish empty, incomplete, and failed discovery

The runner accepts ordinary lists as well as `CollectedLinks`; an ordinary empty list carries no failure explanation. The following paths still deserve individual checks:

- **TripleCrown:** `collect_event_links` returns a plain list and has no recognized-page/valid-empty check before selecting `div.event-row`. A 200 response with changed markup or a challenge can look like no events. Missing/unparseable listing dates are silently excluded, unlike unknown dates that proceed to later validation in some other providers. Report unknown-date candidates or a partial outcome instead of silently losing them. See [collector](scrapers/triplecrown_scraper.py#L532).
- **GrandSlam:** discovery stops successfully when a page adds zero new links. That can mean the end, repeated content with an active Next control, or missing event selectors. Verify the end state separately; an active Next plus repeated event identities should mark partial. See [collector](scrapers/grandslam_scraper.py#L378).
- **Game7:** the explicit `no events found` check is useful, but zero cards without that message also ends successfully. Distinguish valid empty markup from blocked/changed HTML. Tracking page numbers prevents a URL loop, but does not detect a server returning the same events under increasing page numbers. See [collector](scrapers/game7baseball_scraper.py#L254).
- **PST:** `data.get("Results", [])` treats a missing field as a valid last page. Require a dictionary with a list-valued Results field. Missing organization ID currently returns a plain empty list; raise a configuration error. Preserve the browser fallback and its existing partial status. See [collector](scrapers/pst_scraper.py#L147).
- **Ripken:** missing site credentials return an empty list; `response.json() or []` accepts null as empty and does not explicitly validate a list of programs. Require a documented list shape and explicit credential errors. The widget response validation already provides a pattern to reuse. See [widget validation](scrapers/ripkenbaseball_scraper.py#L202) and [program collector](scrapers/ripkenbaseball_scraper.py#L334).
- **TopGun:** feed shape validation is present, but per-record exceptions are caught and silently skipped. Record skipped malformed records and decide whether the collection is partial rather than treating every skip as a normal provider filter. Preserve intentional sport/hidden/type/date exclusions. See [collector](scrapers/topgun_scraper.py#L259).
- **PerfectGame:** a recognized table without group headers ends successfully. Validate actual empty versus a different listing layout. Repeated-page detection currently compares entire HTML, which can change while event identities repeat; compare stable listing identities or paging state instead. See [collector](scrapers/pg_scraper.py#L205).
- **USSSA/NCS:** URL/page bounds are present, but compare stable event identities across pages as well. USSSA's discovered page count can overestimate an empty later page; establish whether that represents legitimate exhaustion or incomplete coverage before assigning partial status. Preserve state query parameters when building subsequent USSSA URLs rather than unconditionally appending `?paged=`. See [USSSA](scrapers/usssa_scraper.py#L138) and [NCS](scrapers/ncs_scraper.py#L50).

A shared outcome type is appropriate; one shared HTML-empty selector is not. Keep the provider-specific evidence that distinguishes the end of listings from failure. The current 600-second budgets are checked between operations, not hard wall-clock cancellation. Any retries must consume that budget rather than multiplying it unnoticed.

### Play365 browser dumps need parity with direct API safeguards

The direct Python API path validates event-list shape, caps pagination, checks a source time budget, and rejects repeated nonempty payloads. Its two generated JavaScript exporters still have unbounded `while (true)` paging and fetch calls without request cancellation. The manual exporter can stop on exhausted retries or non-JSON and subsequently download the accumulated pages without a completion marker. Unrecognized response shapes can also count as zero events. Freshness checks establish age, not completeness or the intended season/filter scope.

Proposed change: use a shared specification for recognized payload forms, paging/time limits, and completion metadata across Python and both exporters. Give fetch calls explicit cancellation; write per-state completed/failed outcomes and source/filter provenance into dumps; reject or explicitly mark partial dumps on import. Older dumps without those fields need a deliberate compatibility policy, rather than being silently accepted as verified full-season coverage. A newly generated timestamp must not substitute for successful collection.

Preserve browser-observed season parameters and POST tokens. Do not simply replace browser seed parameters with Python defaults: the browser path intentionally captures filters that the direct bootstrap may not discover. Compare supported payload forms too: JavaScript currently recognizes wrappers/HTML forms beyond the direct Python path, while row extraction needs actual event objects.

Evidence: [manual exporter](scrapers/play365_base.py#L366), [Selenium exporter](scrapers/play365_base.py#L655), [direct API](scrapers/play365_base.py#L957), [dump loading](scrapers/play365_base.py#L877), [freshness](scrapers/play365_base.py#L1134).

### Let recoverable driver failures reach the runner

The runner can restart a disconnected browser when extraction raises the connection error. NCS and USSSA extraction catch broad WebDriver exceptions, consume their local attempt, and return None. PST catches all bootstrap failures and continues to API work, potentially hiding a dead browser. Classify disconnections before ordinary parse/timeouts and re-raise them; keep normal fallback decisions provider-specific. Audit nested exception handlers too. PerfectGame's dedicated disconnect propagation is a useful starting pattern.

Evidence: [runner recovery](common/batch_runner.py#L130), [NCS extraction](scrapers/ncs_scraper.py#L174), [USSSA extraction](scrapers/usssa_scraper.py#L311), [PST bootstrap](scrapers/pst_scraper.py#L147), [PerfectGame transport](scrapers/pg_scraper.py#L335).

### Preserve visibility of failed enrichment

GrandSlam catches failed locations/contacts/divisions requests and emits defaults; Game7 suppresses contacts failures; PST suppresses schedule failures; Ripken standings failures return an empty age list. A valid required-field row can still lack optional information because an endpoint failed. Track enrichment warnings or status separately from row validity, and retry only the failed enrichment within a bounded policy. Do not require every provider to have every optional field, or mark a legitimately empty contacts/schedule page as a network failure.

## Provider-by-provider opportunities

### USSSA

**Low-risk first change:** reuse the listing HTML loaded by `_get_total_pages` when collecting page 1. Currently page discovery navigates to the base URL, then the main loop navigates there again. This removes one redundant browser navigation per nonempty location. Preserve the detected empty state and per-page wait behavior.

Use an ordered output list plus a set for duplicate detection, replacing linear membership tests as the list grows. Replace unconditional three-second page sleeps with provider-tested readiness/change checks only after timing them. Detail extraction still needs its tab activation, division table/fallback layout, and host-derived state fallback; do not substitute a generic HTTP detail parser without evidence those data are rendered in the response.

**Gate:** one-page, multi-page, empty, timeout, duplicate-page, and disconnected-browser fixtures; identical event URLs and division/guarantee output. Evidence: [page-count load](scrapers/usssa_scraper.py#L106), [collection load](scrapers/usssa_scraper.py#L138), [division parsing](scrapers/usssa_scraper.py#L257).

### NCS

**Low-risk first changes:** set-based ordered link deduplication and driver-error propagation. Keep canonical `/Details/` casing and state/base-query preservation.

**Conditional larger change:** trial persistent-session HTTP retrieval for Details, Divisions, Locations, and Contacts, with page-specific content checks and browser fallback. Game7 demonstrates HTTP retrieval of similarly named routes, but that does not prove NCS renders equivalent data. Keep all four source pages: listing metadata does not replace venue addresses or contacts. Empty optional sections should avoid a full selector timeout only when a recognized empty-state marker is present; a generic body tag is insufficient.

**Gate:** parity for dates, divisions, all venues, and contacts, plus blocked HTML and session-cookie behavior. Evidence: [four-page extraction](scrapers/ncs_scraper.py#L174), [soft waits](scrapers/ncs_scraper.py#L380), [query preservation](scrapers/ncs_scraper.py#L306).

### PerfectGame

**Low-risk first change:** pass the already fetched soup and resolved URL into the single-event parser, then parse contact information from that same detail soup. Currently the outer extraction loads the page, the single fallback loads it again, and the contact helper fetches the detail page again. On a successful single-event path, this can reduce three detail-page loads to one; the separate locations page remains. This is a structural request-count opportunity, not a measured elapsed-time claim.

The grouped path already uses HTTP while other pages normally use Selenium; it is not universally HTTP-first. Preserve that intentional grouped-page behavior, all supported grouped layouts, age/level aggregation, representative child selection, and location scoring. HTTP-first single/contact/locations pages and reduced paging pauses are subsequent experiments, with page-content validation. Do not replace grouped identity with a child event ID merely to reuse parsing.

**Gate:** grouped layouts and genuine single events, redirects, unparseable grouped pages, contact/guarantee parity, and disconnected drivers. Evidence: [HTTP session/retries](scrapers/pg_scraper.py#L188), [transport split](scrapers/pg_scraper.py#L335), [outer fallback](scrapers/pg_scraper.py#L821), [single parser](scrapers/pg_scraper.py#L955), [contact reload](scrapers/pg_scraper.py#L765).

### PST

**Low-risk first change:** memoize successful reverse-geocoding results by exact coordinate pair for the scraper run. If several events share incomplete venue data and coordinates, one lookup can serve them all. Preserve the current one-second limiter on real lookups and the precedence of supplied venue/address fields. Do not cache transient failures as successful empty results; do not merge nearby coordinates through arbitrary rounding.

Review the inherited 30-second batch delay using measured API and geocoder timings. Change it only after replacing it with appropriate request-level pacing; HTTP use alone does not justify zero delay. The schedule request is already supplemental and the API listing already supplies core event data. A schedule cache helps retries of the same event, rather than ordinary one-pass events; key by organization and event ID. Keep browser cookie bootstrap and DOM fallback. Broaden date parsing only through explicit date-format fixtures: the existing parser accepts one fractional-second format and returns N/A otherwise.

**Gate:** repeated coordinates, supplied addresses, geocoder failures, valid-empty versus malformed Results, schedule failures, ISO date variants, and tomorrow boundary. Evidence: [geocode](scrapers/pst_scraper.py#L68), [address precedence](scrapers/pst_scraper.py#L105), [schedule/date parsing](scrapers/pst_scraper.py#L283), [default delay](scrapers/base_scraper.py#L12).

### TopGun

**Low-risk first change:** use a persistent session for the existing filter/global-feed GET requests with bounded GET retries. Feed caching and removal of browser startup are already implemented; do not propose them again as new gains.

Optionally build a LocationID/SportID index once after feed validation to avoid filtering the full cache for every state. At the previously observed feed size this may be minor; measure before adding complexity. Preserve hidden/baseball/umpire exclusions, location-to-state mapping, TournamentID identity, and fragment-free public URLs. The existing collection filters on end date; the final runner filters strictly on start date. Early start-date exclusion can avoid processing ongoing events only when the parsed start date is trustworthy.

**Gate:** duplicate public filter URLs with different TournamentIDs, missing IDs, malformed records, empty feeds, and multi-location states. Evidence: [transport](scrapers/topgun_scraper.py#L159), [feed filtering](scrapers/topgun_scraper.py#L259), [identity](scrapers/topgun_scraper.py#L486).

### GrandSlam

**Low-risk first change:** replace standalone requests.get calls with an instance session, preserving headers/timeouts and adding bounded GET retries. A normal event currently uses Details, Locations, Contacts, and Divisions requests. Pooling improves repeated transport work without deleting any of those sources.

Keep badge-derived age groups, division labels, and guarantees as listing metadata; they are not equivalent to Game7's tags. Improve completeness checks before considering early date filtering. To avoid three enrichment requests for expired events, an extraction phase could parse the authoritative Details date first, then ask the shared date-scope predicate whether enrichment is needed. Unknown dates still proceed; preserve expiry/scope counters and pending-export recovery.

**Gate:** listing badges, related-route derivation, cross-year dates, multiple venues, per-age fees, and enrichment failures. Evidence: [transport](scrapers/grandslam_scraper.py#L40), [listing metadata](scrapers/grandslam_scraper.py#L378), [four-page details](scrapers/grandslam_scraper.py#L459).

### Game7Baseball

**Low-risk first change:** persistent session and bounded GET retries, like GrandSlam. Keep Details/Contacts/Divisions/Locations parsing separate; matching route names do not make the parsers interchangeable.

Listing metadata already provides raw dates, age hints, location/venue hints, and tags. Parse explicit-year listing dates conservatively to skip definitively out-of-scope detail requests; otherwise use the existing Details dates. Alternatively skip enrichment only after parsing Details. Preserve per-age division fees, gate fees, stay-to-play/turf interpretation, venue selection, and base state query parameters.

**Gate:** unknown and cross-year listing dates, contacts missing versus failed, fee mappings, pagination with retained filters, and changed-card HTML. Evidence: [collector](scrapers/game7baseball_scraper.py#L261), [four-page extraction](scrapers/game7baseball_scraper.py#L605), [transport](scrapers/game7baseball_scraper.py#L35).

### TripleCrown

**First change:** validated collection outcomes and reporting of unparseable listing dates. It already has a persistent session and an early strictly-future listing filter.

Add bounded GET retries and consider a run-local URL cache for shared facility pages or exposure fallback pages. Key on the complete URL and only cache successfully validated responses; measure actual repeated URLs first. The configured listing URLs are state-filtered server endpoints, so do not replace 50 state requests with a single master request unless full coverage and state membership are proven. Keep Webflow-family, Pathway, Weebly, AllStar, and Exposure fallback parsers and their field precedence separate.

**Gate:** fixtures for every host family, supported/unsupported hosts, recognized-empty listings, relative/absolute links, dates, shared facilities, and fallback precedence. Evidence: [listing](scrapers/triplecrown_scraper.py#L532), [facility follow-up](scrapers/triplecrown_scraper.py#L966), [host dispatch](scrapers/triplecrown_scraper.py#L1164), [state endpoints](locations.json#L115).

### RipkenBaseball

**First change:** explicit credentials and program-list validation; then bounded GET retries using the existing session. The cached widget site discovery and API-record detail extraction are already good reusable patterns.

Precompute whether each program group contains age-named children. `_is_leaf_program` currently scans its group for each candidate, and `_group_names` is also rebuilt for each selected program. Reuse per-group information while preserving exclusions and output order; benefit depends on group size. Cache validated standings ages by canonical standings URL for repeated extraction; standings are already fetched only when program/group text has no ages, so do not introduce an unnecessary request on every event. Preserve parent/child display names, waitlist exclusion, site-specific locations, and same-name sites distinguished by site credentials.

**Gate:** parent-only, children, mixed waitlists, deleted/non-baseball records, dates, missing ages, transient standings errors, and fee propagation. Evidence: [group selection](scrapers/ripkenbaseball_scraper.py#L267), [program feed](scrapers/ripkenbaseball_scraper.py#L334), [conditional standings](scrapers/ripkenbaseball_scraper.py#L392).

## Play365: eight providers reviewed individually

The subclasses are mostly provider configuration; most implementation work belongs in `play365_base.py`. This is the best existing example of shared plumbing with provider-specific switches. Apply fixes once in the base, then verify all eight configurations.

**Shared optimization:** build a state-to-event index when a validated API/dump source loads. Collection currently walks the full event cache for every selected state. An index changes repeated filtering from roughly states times records to a source pass plus retrieval of selected buckets. Preserve event-state precedence over first-venue fallback, FORCE_SPORT_ID behavior, sorted link output, duplicate-url overwrite behavior, and rebuilding on dump/source changes. Retain the loaded flag so empty successful sources remain cached. This saves CPU work; it does not remove API pagination requests.

**Shared correctness work:** completion-aware dump imports, bounded JavaScript pagination, and request budgets described above. Add bounded transport retries only after explicitly classifying the POST endpoints as read operations. Python already reuses one session for the source fetch and closes it. A single-location request currently initializes all configured export states for FiveTool; selected-state loading could help interactive use, but needs per-state cache completeness and must not make later full collection reuse a partial source as global success.

| Concrete provider | Current acquisition foundation | Proposed update / special verification |
| --- | --- | --- |
| [FiveTool](scrapers/play365/fivetool_scraper.py) | GET; state-filtered export; FORCE_SPORT_ID=None | Shared state index and exporter safeguards. Consider selected-state acquisition for single-location runs only. Do not remove state-filtered requests without proving an all-state endpoint includes the same records. |
| [FiveToolYouth](scrapers/play365/fivetoolyouth_scraper.py) | GET; all-state export through GetAll base; separate youth domain | Shared index/safeguards. Preserve this domain's youth divisions and first-venue fallback; do not adopt FiveTool's state-filter paging just because the name matches. |
| [Play24Sports](scrapers/play365/play24sports_scraper.py) | POST; token; full layout; extra organization/month/name/event-label/division/level/venue parameters | Shared index/safeguards. Preserve the complete parameter set and token/cookie bootstrap; prove retry safety for this read POST. Compare full-layout nested fees/levels. |
| [PlayPPS](scrapers/play365/playpps_scraper.py) | POST; token; medium layout; organization parameter | Shared index/safeguards. Keep medium layout until a fixture/live comparison proves another layout preserves all divisions and fees. |
| [PlayRBI](scrapers/play365/playrbi_scraper.py) | POST; token; full layout; organization parameter | Shared index/safeguards. Check nested team-level pricing and captured season seeds independently of PlayPPS. |
| [ProspectWire](scrapers/play365/prospectwire_scraper.py) | POST; token; full layout; organization parameter; separate domain | Shared index/safeguards. Preserve domain-specific cookies/token and division labels; same parameter shape does not establish identical responses. |
| [2DSportsHighSchool](scrapers/play365/twodsportshighschool_scraper.py) | GET; all-state export; direct API enabled; no prepare-dump prompt override to true | Shared index/safeguards. Verify high-school age/grade normalization and browser-captured season filters; retain unattended fallback queue behavior. |
| [2DSportsYouth](scrapers/play365/twodsportsyouth_scraper.py) | GET; all-state export; separate youth domain | Shared index/safeguards. Verify youth ages/fees and domain provenance separately from the high-school source. |

Evidence: [source loading](scrapers/play365_base.py#L877), [API parameters](scrapers/play365_base.py#L940), [per-state cache scan](scrapers/play365_base.py#L1159), [nested division extraction](scrapers/play365_base.py#L1222), [GET/POST inheritance](scrapers/play365_base.py#L1396).

## Safe scope for homogenization

Share transport construction, explicit connect/read timeouts, bounded retry accounting, session cleanup, collection outcome metadata, request/phase measurement, and cache lifecycle rules. Keep request policy configurable: plain GET HTML, authenticated JSON, browser paging, and read-only POST each need their own rules. A generic cache must include provider/domain and relevant query/organization/filter context, distinguish loaded-empty from failed, and expire with the run or source revision.

Keep provider parsers, route construction, identity decisions, multi-child aggregation, primary venue selection, fee pairing, and season/token parameters local. Do not blanket-set every batch delay to zero, skip enrichment because only required fields are populated, or treat every absence as failure. There is no evidence yet to justify replacing every browser with requests.

Defer concurrent event extraction until session/driver ownership and per-location mutable caches are addressed. `events_by_url`, `listing_meta`, and TopGun's current-location tournament map change during collection. Shared instances across parallel locations can race and assign the wrong metadata. Keep durable staging, idempotent workbook updates, and the single-writer ledger foundation intact.

## Verification before adopting each change

1. Capture or use offline provider fixtures for normal, empty, malformed, partial, and alternate layouts. Assert link identities and complete rows, not just request success.
2. Count source/listing/detail/enrichment requests and browser navigations; time those phases independently from persistence and export. Include retry counts and failed optional enrichment.
3. Check strict future dates, supplied date bounds, unknown dates, duplicates across states, and pending export recovery. Early filtering must still preserve accurate manifest/scheduler outcomes.
4. Run the relevant existing offline regression tests, adding targeted provider fixtures only for changed behavior. The previous 40-test baseline covers the full-pull infrastructure; it does not prove all provider parsing or live endpoint equivalence.
5. For transport/rendering changes, compare bounded representative live samples with the old path using isolated output and no production ledger writes. Make a default change only after row/coverage parity, then review timing gains.

This review adds no measured seasonal speedup claims. The immediate request reductions identified above follow from current control flow; their wall-clock benefit and provider compatibility remain to be verified during implementation.
