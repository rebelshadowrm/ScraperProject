# Acquisition and delivery audit

Audit: 2026-10-14, before implementation. Existing local changes to README.md,
main.py, ui/cli.py and test_cli_launch.py are preserved separately from this work.
Provider/common sources and the pre-existing diff were frozen under ignored
Tests/parity_baseline/multisport with SHA-256 hashes. No production pull was run.

## Operational architecture

main.py anchors the working directory and enters ui/cli.py. Dynamic class discovery
finds nine concrete top-level adapters and a Play365 dispatcher for eight more.
Locations are configured in locations.json or discovered dynamically. Provider and
location processing is sequential. HTTP retries, page/time budgets, one browser
restart, deferred manual fallback and partial coverage reporting already exist.

common/runner.py discovers provider identities, conservatively migrates legacy URL
keys, and invokes common/batch_runner.py. Validation normalizes optional contact
fields and requires title, dates, state and usable public URL. normalize_row maps
the importer dropdowns, paired ages/fees and dates. The persisted event_data is a
56-field importer row, not a sport-aware event model. No sport column exists.

Each entry in events.json contains provenance, locations, lifecycle, last normalized
payload, attempt/error metadata, and Excel staging fields. processed and
export_status are Excel lifecycle flags. Discovery, extraction, file creation and
delivery are distinct operations. These flags cannot acknowledge a website.

common/storage.py implements same-directory atomic replacement, previous-file
backups and nonblocking OS locks released on process death. Batch staging persists
payload and original workbook path before writing. Workbook identity upserts and
hidden _ScraperExportIndex permit replay after workbook success/checkpoint failure.
Preserve these paths and identities. Existing baseball IDs are provider class plus
case-preserving normalized URL, except TopGun's verified TournamentID fragment
(fallback hashes include name, sport, location, type and dates).

## Existing provider inventory

Configuration and the original workflow target baseball, but organization-wide
feeds are not sport proof. A later read-only backfill check found an actual
fastpitch record in the legacy PlayPPS output. These shared parsers can extract
softball-shaped rows without identifying their sport; that is not verified typed
softball support. All original adapters emit the same importer row for Excel.

| Adapter | Coverage configured | Acquisition and identity | Softball today |
| --- | --- | --- | --- |
| USSSA | State baseball sites, national region fallback | Browser HTML, JSON-LD date repair; URL identity | No; national fallback explicitly uses baseball sport 11 |
| NCS | State baseball listings | Browser pagination/details; URL identity | No verified fastpitch route/layout |
| Perfect Game | 50 states and baseball categories | Browser legacy plus bounded public group API; URL identity | No; sportType=Baseball |
| Triple Crown | State baseball component feed and allowed detail hosts | HTTP schedule HTML and several detail layouts; URL identity | No; sportID=1, baseball host allowlist |
| TopGun | Dynamically listed states | Two shared JSON feeds; TournamentID | Parser accepts sport query but dynamic locations force baseball 1 |
| PST | Configured Premier Sports organizations | Registration API with browser/session fallback; URL identity | Unverified; no explicit sport identity in rows |
| Game7 | Configured state baseball listings | HTTP HTML plus detail/contact extraction; URL identity | No |
| GrandSlam | Configured state baseball listings | HTTP paginated HTML/details; URL identity | No |
| Ripken | Dynamic baseball locations | Browser listing/details; URL identity | No |
| FiveTool, FiveToolYouth | Configured Play365 organizations across states | Shared Play365 GET/GET-all feeds, browser/manual fallback; URL identity | Unverified; FiveTool has no forced sport filter |
| Play24Sports, PlayPPS, PlayRBI, ProspectWire | Configured Play365 organizations across states | Shared POST/CSRF feed, browser/manual fallback; URL identity | PlayPPS has an observed legacy fastpitch row; sport typing is unverified. Organization coverage is not sport proof |
| 2DSportsYouth, 2DSportsHighSchool | Configured Play365 organizations across states | Shared GET-all feed, browser/manual fallback; URL identity | Unverified |

The October 4 release report records 17 concrete providers, 722 completed location
passes, 3,610 location rows/3,593 unique events and 144 workbooks. This is historical
baseball evidence, not fresh October 14 verification or nationwide completeness.
FiveToolYouth requires a fresh authorized normal-browser dump when automation is
blocked; the 24-hour guard must remain. Frozen fixture parity does not certify live
provider availability. See LIVE_FULL_PULL_RUN_REPORT.md and
PROVIDER_UPGRADE_VALIDATION.md for exact samples and limitations.

## CLI, manifests and recovery

Interactive menus coexist with --all, --test, --mode incremental|refresh|snapshot,
inclusive start-date bounds and --repair-snapshot. Only strictly future starts are
eligible. Snapshot writes a run-specific tree; refresh updates dated sheets;
incremental skips Excel-processed events. Pending Excel payloads replay to their
original paths even if no longer discovered. Recovery then refreshes rediscovered
sources for a new snapshot. Same-day repair retains successful locations and
archives the earlier manifest. New-day repair requires a new run.

RunManifest persists planned providers, expected locations, completion/error
metrics and output root. Noninteractive partial/interrupted runs return nonzero.
The existing PowerShell monitor waits for a child with durable stdout/stderr and
native exit status, but has no whole-process deadline or daily destination retry.
The ledger lock covers each provider run; a daily coordinator needs a separate
whole-job lock. No scheduled task will be installed as part of this work.

## Compatibility and gaps

The canonical OUTPUT_COLUMNS and template contain 56 import columns, including
repeated headers, formatting and dropdown validations. Keep them unchanged and
retain existing Excel acknowledgement semantics. Local schema/parity/recovery
tests can verify workbook compatibility; an external importer end-to-end smoke
test requires the importer and remains a separate acceptance gate.

Missing: explicit sport provenance; sport-isolated acquisition and output routing;
website revision/acknowledgement queues; receiver idempotency/reconciliation;
bounded daily orchestration with independent destination retries. Never infer
website delivery from processed, workbook creation or HTTP success alone. Never
infer event deletion from absence in an incomplete provider listing.

## Current ledger inspection (read-only handoff supplement)

The current local events.json contains 14,344 records, 4,130 durable payloads,
14,194 processed flags, 3,593 exported statuses, 537 expired statuses and one
source-incomplete status. There are 4,934 records without scraper provenance;
all 14,344 predate explicit sport metadata. No records are soft-deleted.
failed_events.json contains 149 failure records. Those counts describe local
state, not verified active events or unresolved failures in a fresh provider run.
No ledger was rewritten or migrated on disk to obtain these statistics.

An initial temporary website backfill staged 3,308 future rows and caught one
fastpitch URL under PlayPPSScraper. The source is
baseball.playpps.com/events/fastpitch-armed-forces-brawl-mansfield-11-27-2026.
This exposed an unsafe historical baseball assumption, so the final implementation
also quarantines untyped legacy Play365/PST website payloads until sport provenance
is verified. Fresh Play365 acquisition records the observed sport_id; only the
existing known baseball ID 1 verifies that route. Other/missing IDs are not guessed
into softball. Dedicated softball adapters own explicit sport identities. The
established Excel path is preserved; no historical mixed-sport row was removed.
