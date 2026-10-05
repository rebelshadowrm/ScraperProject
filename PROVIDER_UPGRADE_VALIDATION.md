# Provider upgrade validation

The authorized live follow-up also updates Play365's public CSRF refresh/POST protocol. Frozen exporter payloads and source filters still match; URL comparisons omit changing CSRF credentials, and separate assertions require POST plus a fresh token. Live captures show zero 56-field differences for 35 events across seven configurations and all 208 events in FiveTool Youth's verified 42-page capture. See LIVE_FULL_PULL_RUN_REPORT.md for current run status and release checks; older method/default descriptions below describe the earlier optimization stage.

Subsequent authorized production verification is recorded in [LIVE_FULL_PULL_RUN_REPORT.md](LIVE_FULL_PULL_RUN_REPORT.md). The expanded suite after live date, discovery, source-contract, contact-recovery, browser-recovery, pagination reconciliation, and audit fixes passed **126 tests**. Live full-pull acceptance remains a separate gate; the historical offline-only pass below did not itself mutate production data.

This pass implements the low-risk upgrades from SCRAPER_OPTIMIZATION_REVIEW.md and establishes before/after parity tests. The baseline is the working tree at the start of this pass, including the previous full-pull recovery fixes. It is not the older Git HEAD.

## Testing skills

- [.agents/skills/scraper-provider-parity/SKILL.md](.agents/skills/scraper-provider-parity/SKILL.md): frozen source, deterministic response replay, exact field/identity comparisons, request counts, and targeted failure tests. Includes a reusable JSON comparison script.
- [.agents/skills/scraper-live-parity/SKILL.md](.agents/skills/scraper-live-parity/SKILL.md): bounded live comparison for proposed HTTP/browser substitutions, server filtering, and season/layout changes.

Both are installed under `C:/Users/rebel/.codex/skills/` for future discovery. The existing performance and resilience skills remain applicable.

## Implemented sequence

1. **Coverage and recovery:** require explicit valid-empty evidence for Game7, GrandSlam, TripleCrown, and PerfectGame; report unparseable TripleCrown listing dates; validate PST Results and Ripken programs; reject missing source credentials; retain collected Game7/GrandSlam links when subsequent requests fail. Detect repeated identities despite changing HTML. Preserve USSSA query filters. Propagate browser disconnections from extraction and nested handlers to the runner's restart mechanism.
2. **Redundant loads:** reuse USSSA's first listing page. Reuse PerfectGame's already-loaded single-event detail soup for parsing and contacts, while retaining the separate locations request and grouped extraction path.
3. **Transport:** instance-owned sessions for Game7, GrandSlam, and TopGun. A shared GET-only policy also equips existing TripleCrown/Ripken sessions with up to two retries for connection/status failures. Read retries are disabled; backoff is bounded. Retry-After waits above 30 seconds fail visibly instead of waiting indefinitely or retrying prematurely. Existing headers, endpoint parameters, and request timeouts are retained. The CLI closes instance-owned sessions even on failure. PST and Play365 POST transport defaults were not replaced by this GET-only policy.
4. **Caches and group work:** cache successful PST coordinate results, including verified no-result lookups; retry transient lookup failures and preserve supplied address precedence. Index Play365 source records by state and rebuild on source replacement, retaining venue fallback, sorted URLs, and duplicate overwrite order. Precompute Ripken group information while preserving parent/child, waitlist, and duplicate-program-ID behavior.
5. **Play365 fallback safety:** both generated JavaScript exporters share list validation, stable page fingerprints, 200-page/source-time budgets, and abortable fetch/body reads. No successful dump is produced after incomplete collection. Verified dumps record completed export states, provider domain, and captured seed parameters. Import checks completion, domain, states, payload shape, and contiguous page numbering before loading the cache. Selenium dump replacement is atomic.

Strictly future event starts, supplied date bounds, provider identities, output columns, and pending export recovery remain intact.

## Before/after evidence

Final validation: **60 offline tests passed** in the full suite, including executed Node exporter tests and workbook recovery/identity/manifest checks. The expanded latest provider fixture replay was also rerun independently and passed. The standalone comparator reports zero data differences. Syntax checks passed for 51 Python files, `git diff --check` passed, and both new skills passed the skill-creator validator; installed copies match their repository sources.

`test_suite/fixtures/provider_baseline.json` contains frozen discovery identities, raw rows, and validated/normalized export rows. All row fixtures must pass the existing required-field validator before capture or replay can succeed. The before-change source SHA-256 values are recorded with the baseline. Raw source snapshots are local and ignored in `Tests/parity_baseline/scrapers/`; routine tests use the tracked goldens and do not need those snapshots.

- All 17 provider configurations are replayed. Sixteen providers produce complete 56-column extraction/export row comparisons; USSSA has discovery parity because its optimization changes listing navigation, not detail parsing. USSSA's existing detail parser is not claimed to be comprehensively covered.
- There are 33 normal event rows across these fixtures. Play365 cases include multiple venues, nested team-level prices, duplicate URLs, state versus venue precedence, missing state, and a changed source. Ripken also has six group-selection cases, including a duplicate ID and waitlist-only child.
- `test_suite/fixtures/exporter_baseline.json` captures both generated exporter paths for each of the eight Play365 configurations. Node executes the JavaScript with deterministic browser API substitutes and no network. Candidate event pages and captured season seed parameters must exactly match the frozen exporter output. New completion metadata is checked separately as an intentional addition.
- A local HTTP server verifies actual connection reuse, retry counts, and failure on a long Retry-After. No external provider requests are needed for that transport check.
- Failure tests cover malformed/null/missing lists, blocked HTML, unknown dates, repeated event content, retained links after a later failure, driver disconnections, cache replacement, transient geocode errors, incomplete dump provenance, and hung exporter requests.
- The comparison utility was also checked with an intentionally changed entry fee; it correctly exits unsuccessfully and identifies the differing field.

| Fixed replay case | Before | After | Field/identity result |
| --- | --- | --- | --- |
| USSSA one-page nonempty discovery | 2 browser navigations | 1 | Exact links and identities |
| PerfectGame single event including locations/contacts | 4 page loads | 2 | Exact raw and export rows |
| PST three events sharing incomplete address/coordinates | 3 reverse lookups | 1 | Exact address results and supplied venue precedence |
| Game7/GrandSlam normal listing plus details | 5 requests each | 5 requests each | Exact rows; connection reuse, rather than fewer requests, is the improvement |

These are observed fixture request counts, not seasonal elapsed-time benchmarks. State indexing and Ripken grouping reduce repeated CPU work; no full-season speedup is claimed.

## Intentional behavior changes

Valid fixture outputs remain identical. Invalid or incomplete input is deliberately no longer preserved as a healthy empty result. Manifests can now report partial/failed work where older code reported success. The existing runner consumes these completion flags and continues independent locations.

**Older manual Play365 dumps without completion metadata must be regenerated with the current exporter.** Fresh file modification time alone cannot prove successful collection. Dumps for another domain, missing export states, malformed payloads, and discontinuous pages are rejected before source loading. No existing dump or production data was migrated or deleted by this pass.

The stricter empty-page recognition is conservative. Live markup not represented by the current explicit-empty markers may be reported partial; add a provider-grounded fixture before extending accepted empty states. Optional enrichment failure reporting remains a separate improvement: successful required-field rows can still omit supplemental contacts/fees/levels after an endpoint failure despite bounded GET retries.

## Reproduction

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test_suite -v
.\.venv\Scripts\python.exe test_suite/provider_replay.py --output Tests/parity_after.json
.\.venv\Scripts\python.exe .agents/skills/scraper-provider-parity/scripts/compare_outputs.py test_suite/fixtures/provider_baseline.json Tests/parity_after.json
```

Node is required to execute the generated-exporter tests; those tests explicitly skip if Node is absent. Node was available and those tests executed in this validation pass. Do not silently count skipped exporter tests as browser-export parity.

Golden recapture is an explicit maintenance action, using frozen before-change source. Never overwrite goldens with changed-code output to erase a discrepancy. When adding supported fixtures, capture their expected results from the existing frozen source and document intentional corrections separately.

## Changes held behind evidence gates

- HTTP-first NCS details and additional PerfectGame pages require actual rendered-content parity; existing browser acquisition remains in place.
- Early date filtering requires trustworthy provider listing dates and preserved expiry/date-scope accounting. The fixture work exposed existing limitations for some explicit-year date displays; broadening provider date parsers is a separate correction with supported-format and cross-year fixtures, rather than an assumption folded into transport optimization.
- Reducing PST's inherited batch delay, other fixed page pauses, or request pacing needs provider timing/error evidence. Those delays remain unchanged.
- TopGun source indexing, shared facility-page caching, selected-state-only FiveTool fetching, concurrency, and database replacement need measured benefit and their own cache/ownership contracts.
- The fixtures do not cover every live host/layout, HTTP blocking pattern, midnight/timezone boundary, or browser widget state. No live endpoint equivalence, full-season coverage, or downstream importer acceptance is claimed from offline parity.

Production events.json, failed_events.json, scheduler_history.json, and seasonal workbooks were not used as mutation targets. Existing recovery, identity, manifest, and workbook checks are run alongside these provider tests.
