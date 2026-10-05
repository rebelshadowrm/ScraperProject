# Full-pull improvement sequence

Current production verification, source repairs, and the release gate are recorded in [LIVE_FULL_PULL_RUN_REPORT.md](LIVE_FULL_PULL_RUN_REPORT.md). The implementation stages below retain their original local-validation scope.

Project skills live in `.agents/skills/` and are installed into the user's Codex skill directory for discovery. Each skill supports this repository's actual data and export contracts.

The subsequent provider upgrades and frozen before/after testing are documented in [PROVIDER_UPGRADE_VALIDATION.md](PROVIDER_UPGRADE_VALIDATION.md). The provider-parity and live-parity skills extend the four infrastructure skills below.

1. **Recovery and durable exports** — implemented and locally verified. State and workbook saves use same-directory atomic replacement with rolling `.bak` copies. Cooperating live writers use an OS-released ledger lock. Validated payloads and original destinations are staged before export; workbook identities make retries update the same row. Workbook/checkpoint failures are reported separately, and staged exports survive missing discovery and a later output date.
2. **Identity and seasonal behavior** — implemented and locally verified. TopGun uses `TournamentID`, verified in one bounded live schema request, and its umpire filter is parsed once. Provider identities preserve URL path/query case. Unambiguous legacy entries retain lifecycle state and export aliases; ambiguous TopGun filter URLs are not assigned to an arbitrary new tournament. Full sweeps default to refreshed snapshots, with optional event start-date bounds and the existing strictly-future rule. Recovered old exports are acknowledged at their original destination before a fresh snapshot extraction.
3. **Coverage and bounded execution** — implemented and locally verified. Independent provider/location failures continue the sweep. USSSA, NCS, PerfectGame, PST, Game7, and GrandSlam expose listing failures/budgets; Play365 validates response shape and detects repeated pages. HTTP/browser calls are bounded, and listing loops have a 200-page/600-second budget checked between requests. Unattended full sweeps queue Play365 manual fallback. A run manifest records expected work and complete/partial/failed/queued/interrupted outcomes. Template header compatibility is checked before browser startup. Firefox uses the actual `-headless` argument.
4. **Performance** — baseline optimizations implemented and locally verified. Legacy event schema migration occurs on load, and event updates no longer rescan the whole ledger. Checkpoints are batched. Snapshot payloads are staged per batch and exported together per destination, then the location snapshot is finalized from canonical records. TopGun and Play365 cache successfully empty feeds, and TopGun fetches its global feed once per scraper run. TopGun and GrandSlam no longer start Firefox. Template styles and validations extend beyond its initial 1,003 rows.

Production full pulls, legacy workbook cleanup, and live provider smoke verification are separate operational actions. Local changes are validated against temporary files and fixtures.

## Skills

- `$scraper-pull-recovery`: durable staging, replay, and failure injection.
- `$scraper-event-identity`: provider identities, conservative migration, and seasonal output.
- `$scraper-pull-resilience`: coverage, bounded discovery, and deferred fallback.
- `$scraper-pull-performance`: ledger, workbook, and source-cache measurement.

Repository copies are in `.agents/skills/`; installed copies are in `C:/Users/rebel/.codex/skills/`. All four passed the skill-creator validator. The validator's PyYAML dependency was installed into a temporary tools directory, without changing project requirements.

Later provider parity/live parity skills and the full-pull release skill extend these workflows. Read [PROVIDER_UPGRADE_VALIDATION.md](PROVIDER_UPGRADE_VALIDATION.md) for frozen output evidence and [LIVE_FULL_PULL_RUN_REPORT.md](LIVE_FULL_PULL_RUN_REPORT.md) for authorized production attempts and current acceptance status.

## Output and recovery contracts

- Full sweeps write fresh output under `Sheets/Snapshots/<run-id>/<run-date>/<organization>/`. Manifests are under `Sheets/PullRuns/<run-id>.json`; test snapshots/manifests use `Tests/` instead.
- Incremental and refresh modes retain the usual dated output folders. A refresh updates rediscovered events; a snapshot also removes stale rows from its output by rebuilding from the current successful collection. An incomplete collection always leaves the manifest partial.
- The 56 import columns remain unchanged. A hidden `_ScraperExportIndex` sheet holds identities and row references. A conflicting manual edit/sort is reported rather than silently overwriting an indexed row. Downstream live importer compatibility still needs smoke verification.
- The first Ctrl+C requests shutdown after active work returns and flushes completed extraction batches; a second interrupts immediately. OS locks release on process exit. Rolling backups preserve the previous successful file, and are not a transaction across files.
- A new full run can replay pending exports to their previous destination and refresh their rediscovered source data into its own snapshot. It does not continue the previous manifest as the same run.
- Manual Play365 dumps must be fresh within 24 hours by file time and, when supplied, embedded generation time. Download recovery copies the original file through an atomic replacement.

## Validation and measurement

Run the offline regression suite from the project root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test_suite -v
```

The suite injects storage and workbook failures, exercises interrupt/replay, checks provider/location isolation and response validation, tests the real fixture-driven CLI pipeline, and verifies refresh/snapshot behavior and template overflow. Fixture failures deliberately produce error log messages.

Validation result: 40 offline regression tests passed, including staged recovery after dynamic location discovery fails. Python syntax checks and `git diff --check` also passed.

At 10,324 loaded ledger entries, 1,000 attempt updates averaged approximately 0.0000017 seconds each, compared with the audit's approximately 0.028 seconds with whole-ledger scans. Three atomic saves with backups averaged 0.230 seconds each for the migrated 10.1 MB metadata fixture. Individual saves are costlier than the original unsafe writes; successful incremental batches now use two event-ledger commits per 10 events instead of two per event. Snapshots use one staging commit per batch and an acknowledgement per destination. These are local measurements, not measured full-season runtime.

## Remaining readiness work

Run isolated live smoke checks for every concrete provider and check the downstream importer, then exercise a representative staged seasonal run and interruption/resume. The implementation test pass did not launch a full pull or change production ledgers or workbooks; the only live request inspected TopGun's response schema.

SQLite row persistence, provider-specific concurrency, and dependency pinning after live verification remain follow-up improvements. Existing duplicate historical workbooks need reconciliation separately; they were not rewritten as part of these code changes. Live full sweeps include staged locations that have disappeared from a provider's dynamic location list, using saved provenance for recovery.
