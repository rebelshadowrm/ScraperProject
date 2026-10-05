# Full pull audit

Audit date: 2026-10-04. Scope: CLI orchestration, discovery, extraction, validation, persistence, export, recovery, and performance. Reviewed all 34 Python source files at least for syntax and relevant call paths; examined local state, scheduler history, logs, and output workbooks. No live provider requests or production pulls were made. No production code or run data was changed.

## Current behavior and evidence

`main.py` enters `ui/cli.py`. Selecting “Pull all scrapers” discovers 10 top-level scraper classes. One is the Play365 dispatcher, which expands to eight configured providers: 17 concrete providers overall. Providers and locations run sequentially. Configuration supplies 701 location passes, including 400 Play365 state passes, plus the dynamically discovered TopGun and Ripken locations.

Each location collects links, updates a shared event ledger, extracts only rediscovered unprocessed entries, validates them, and appends future events to a dated workbook in batches of 10. Successful exports set a permanent `processed` flag. The event ledger stores lifecycle metadata, not the extracted tournament payload.

Local evidence:

- 10,324 ledger entries; 10,257 processed; 67 unprocessed; no soft-deleted entries.
- `events.json` is 8,264,407 bytes. 4,935 entries have no scraper provenance.
- 69 failure records. Several describe older implementations, so they are not all current defects. In particular, PST's current extraction signature accepts the runner's keyword arguments.
- Scheduler history covers TripleCrown, Ripken, and Game7 only. It is insufficient to estimate a complete seasonal pull or assert health for every provider.
- `Logs/2026-04-07/160530_RipkenBaseballScraper.log` records a checkpoint failure after an Excel append. The corresponding Aberdeen workbook contains 112 rows and 10 duplicate event URLs. This is consistent with the reproduced recovery defect below; the audit does not establish the history of each duplicate independently.

## Fix before the seasonal full pull

### 1. Make exports and recovery idempotent — critical

Evidence: `common/runner.py:254–281`, `common/json_utils.py:180–190`, and `common/excel_utils.py:305`.

The batch handler appends to Excel, increments its row count, and then checkpoints individual events. If a checkpoint fails, the broad exception handler logs an Excel error and retains `pending_rows`. The next batch appends those already-written rows again. An isolated two-event reproduction wrote three rows, repeating the first event. A process crash between the workbook write and checkpoint can produce the same ambiguity on restart.

If workbook writes fail entirely, the run can finish with no durable export failure records: the isolated reproduction reported zero extraction/validation failures, zero rows, and an empty failure list. Scheduler history computes `last_failed` from extraction and validation failures only.

All three JSON state files are written directly to their destination. An interrupted or failed write can truncate the previous checkpoint. Excel saves also overwrite their destination directly.

Recommended change:

- Persist normalized event payloads and extraction status in a canonical store before export. A transactional SQLite store is a suitable longer-term replacement for the growing JSON ledger.
- Generate or upsert exports using stable event identities, so regenerating or resuming produces exactly one row per intended event. Treat extraction success and export success as separate states.
- As an immediate improvement, use temporary files in the destination directory followed by atomic replacement, maintain backups, and prevent overlapping writers. Atomic replacement protects each file; it does not create a transaction across JSON and Excel.
- Separate workbook failures from checkpoint failures. Stop the affected location on an ambiguous commit and record its status explicitly rather than automatically re-appending the pending batch.
- Reconcile or regenerate existing output through stable IDs; Aberdeen already needs duplicate cleanup.

Acceptance: inject workbook failure, checkpoint failure after append, and interruption; resume each case and verify one output row per event, recoverable state, and explicit partial/failed status.

### 2. Define what “full” means and allow seasonal refresh — high

Evidence: `common/runner.py:375–383`, `common/json_utils.py:253–283`, `common/validation.py:228–234`.

The current full pull sweeps all sources but extracts only previously unprocessed URLs. Existing events never refresh their dates, fees, contacts, or venues. Sources that reuse a URL for a new season remain skipped. A new dated output folder contains only newly processed events, not a complete snapshot. Removing an output workbook does not reset the ledger or allow reconstruction from saved payloads.

Recommended change: expose explicit incremental, refresh, and snapshot behavior; scope runs to an explicit season/date interval; persist the latest normalized payload and source identity; refresh eligible existing events according to age or season; generate the seasonal snapshot from that dataset. Do not blindly clear processed flags against existing workbooks, which would introduce duplicates.

Also distinguish exported, expired, filtered, and deleted events. Currently a non-future event becomes `processed=True` despite never being exported. The strict `start_date > today` rule excludes events starting today and events already underway; confirm that this matches the desired product behavior.

Acceptance: rediscover a processed URL with changed fields and a reused seasonal URL; verify the chosen refresh policy and a complete snapshot with no duplicated rows.

### 3. Fix TopGun identity collisions and overwritten filters — high

Evidence: `scrapers/topgun_scraper.py:173–222`, `293–356`, and `common/runner.py:42–43`.

TopGun builds an event identity from sport, location, type, and dates, with no tournament identifier. Two distinct events sharing those fields overwrite the same cache entry and are deduplicated by the runner. The isolated reproduction returned two links but retained only one tournament, losing the first.

The type lookup is also constructed twice. The second construction drops the supported `TournamentTypeName` field and replaces the first lookup and banned-type set. A mocked type named “Umpire” solely through that field produced an empty name and no banned ID.

Recommended change: use the provider's stable tournament ID when available, verified against an actual response, and keep the public website URL separate from internal identity. Consolidate the type parsing into one pass. Centralize identity rules: the runner currently ignores the scraper `get_event_key()` contract and lowercases entire URLs, including potentially case-sensitive paths and query values. Namespace IDs by provider and model location membership separately from identity.

Acceptance: two same-date/same-location tournaments survive; umpire types using either supported name field are filtered; case-sensitive URLs are not merged accidentally.

### 4. Isolate failures and distinguish empty from incomplete discovery — high

Evidence: `ui/cli.py:364–415`, `442–455`; `scrapers/usssa_scraper.py:107–189`; `scrapers/ncs_scraper.py:68–131`.

One exception in a location exits the remaining locations for its provider. Dynamic location discovery happens outside that provider's exception handler, so a TopGun or Ripken discovery failure can terminate the remaining all-provider sweep. A non-manual-fallback exception in the Play365 dispatcher also abandons the remaining Play365 providers.

USSSA converts initial discovery errors into zero pages and absorbs page failures. NCS returns the links collected before a pagination timeout or driver failure. The runner sees these as ordinary empty or successful collections and cannot assess missing coverage; swallowed disconnects bypass its restart path.

Recommended change: catch and report failures at provider discovery and individual location boundaries, continue independent work, and preserve partial outcomes. Return collection metadata including completion, page count, and error details alongside links. Propagate driver disconnects to the recovery layer. Retry transient failures within bounded budgets and flag unexpected drops against historical coverage. An empty result should be accepted only after a valid, complete response.

Acceptance: fail discovery, a middle location, and a middle listing page; other providers/locations continue and the manifest accurately records missing coverage.

### 5. Bound network calls, pagination, and manual fallbacks — high

Evidence: `scrapers/topgun_scraper.py:159`; `scrapers/pst_scraper.py:199,344`; `common/driver_manager.py:77–135`; `scrapers/play365_base.py:974–1007`; `ui/cli.py:215–225`.

TopGun requests and PST event/schedule requests have no explicit timeout. Driver initialization supports a page-load timeout but CLI startup/restart paths do not supply it. Play365 pagination increments until an empty response, without a page cap or repeated-page guard. The full sweep can pause at a Play365 manual prompt before the remaining top-level providers run.

Recommended change: configure HTTP and browser timeouts, bounded transient retries with backoff, provider/location runtime budgets, pagination caps and repeated-page detection. Use PerfectGame's existing HTTP retry adapter as an internal starting point. Add an unattended fallback policy that queues manual work and finishes the automatic sweep; report queued providers in the manifest. Make interruption flush completed staged work safely; the current signal handler raises `KeyboardInterrupt` immediately, which can bypass the batch flush.

Acceptance: stalled requests, a repeated nonempty API page, and blocked Play365 automation terminate within their budgets and produce reviewable recovery work.

## Highest-value performance improvements

1. **Stop rescanning and rewriting the entire ledger per event.** `ensure_events_schema()` walks all events and constructs defaults; discovery, attempt recording, marking processed, and saving call it repeatedly. On this machine at the current ledger size, 10 mocked attempt updates averaged 0.028 seconds each and three temporary-file saves averaged 0.157 seconds each. Two saves alone cost about 0.31 seconds per successful exported event. At a fixed ledger size, 10,000 such events would imply roughly 52 minutes of save time and 165 GB of serialized output, excluding network, Excel, other updates, and ledger growth. These are extrapolations, not measured full-pull duration. Validate/migrate once on load, update one entry at a time, and checkpoint per committed batch using the recovery design above. Move to transactional row updates as scale grows.

2. **Fetch and partition shared feeds once.** TopGun currently requests its complete tournament feed for each state; cache it once per run and index by location/state. Play365 already caches a nonempty feed, but uses list truthiness as its loaded flag. A successfully empty feed is fetched again for every state. The mock reproduced two loads for two states; each provider has 50 state passes. Use an explicit loaded flag and build a state index once. Validate response shape and pagination completion before recording a successful empty load. Require timestamps/freshness checks for manual dumps rather than accepting any existing file.

3. **Remove unnecessary browser startup.** TopGun and GrandSlam inherit `REQUIRES_DRIVER=True` although their current collection/extraction paths use HTTP/cached data. Set their capability correctly. PST still has browser fallback and cookie initialization, so it needs a separate lazy-start design. Make the hardcoded Firefox location configurable.

4. **Reduce repeated workbook work.** Every 10 rows reloads the complete workbook, rebuilds column mappings, scans for its first empty row, and saves the file. Deriving exports once from durable normalized data avoids this repeated work. If batch export remains, cache immutable header mappings and measure larger provider-specific batches after making writes recoverable. Avoid simply enlarging batches before addressing the lost/duplicate-work window.

5. **Add concurrency only after persistence is safe.** Initially parallelize independent HTTP providers with a small configurable worker count, separate sessions and per-host limits, and a single canonical writer. Keep browser providers sequential until separate driver/state ownership is explicit. Current shared JSON writes and mutable scraper caches are not ready for concurrent mutation. Measure network, parsing, checkpoint, and export time separately before selecting worker counts.

## Readiness and validation plan

The existing `Tests/` directory contains isolated scrape artifacts, not an automated test suite. All 34 Python sources parsed successfully. The top-level scraper extraction signatures accepted the runner's current keyword arguments. A real temporary copy of the current template successfully appended two rows and then a third. The current template has 56 columns, 1,003 formatted rows, and no Excel table; the table-indexing branch was therefore not exercised. Export header coverage is only logged, not enforced, and reversed date ranges pass current validation.

Before the full pull:

1. Implement the critical recovery/identity fixes and explicit refresh behavior. Add meaningful automated regression checks for failure recovery, collisions, empty-source caching, partial discovery, and date ordering.
2. Add a run manifest with stable run ID and output root, season/date scope, expected providers/locations, completed/partial/failed/queued status, discovered/attempted/exported counts, persistence failures, duplicates, and timing. Current `candidates_processed` is the planned candidate count even if interrupted, so track actual attempts separately. Keep the output date fixed for a run spanning midnight.
3. Preflight configuration, template/header compatibility, output write access, state readability/backups, dependencies, and browser availability. Pin a known-working dependency set after smoke verification; `requirements.txt` currently has no version constraints. Fail export before scraping if required headers cannot be resolved.
4. Run a small isolated live smoke pull for every concrete provider, including each Play365 variant and providers not covered by the current three-provider validation subset. Check future dates and next-season boundaries, required fields, dropdown normalization, and date ordering. These live checks remain outstanding after this audit.
5. Run a representative staged pull, interrupt/resume it, and reconcile unique source IDs against canonical records and output. Require no unexplained duplicate rows, explicit accounting for every provider/location, and no unreconciled persistence failures before starting the seasonal sweep.

Suggested implementation order: recovery and event identity first; refresh semantics and coverage reporting next; bounded execution and source-cache fixes next; ledger/workbook optimization next; concurrency last.
