# ScraperProject
Baseball Aggregator Scraper

Run from the project root with Python and the packages in `requirements.txt`.

```powershell
.\.venv\Scripts\python.exe main.py
```

The “Pull all scrapers” menu now defaults to refreshing existing events and building a new snapshot. Only events starting after today are exported. Optional date bounds apply inclusively to event start dates.

```powershell
# Isolated full snapshot; this contacts all configured providers.
.\.venv\Scripts\python.exe main.py --all --test

# Full snapshot of every valid future event, without an end-date cutoff.
.\.venv\Scripts\python.exe main.py --all --mode snapshot

# Retain incremental behavior, exporting newly discovered events.
.\.venv\Scripts\python.exe main.py --all --mode incremental
```

Full sweeps defer Play365 manual fallback and continue other providers. Use `--interactive-fallback` to enable those prompts. A partial or interrupted noninteractive sweep returns a nonzero exit status. `--mode refresh` updates rediscovered events in the usual dated sheets; `--mode snapshot` creates a separate run directory.

Snapshot outputs are under `Sheets/Snapshots/<run-id>/`; coverage manifests are under `Sheets/PullRuns/`. `--test` keeps state and output under `Tests/`. Set `SCRAPER_FIREFOX_BINARY` if Firefox is installed outside the default Windows path.

Offline regression checks:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s test_suite -v
```

See [PULL_IMPROVEMENTS.md](PULL_IMPROVEMENTS.md) for project skills, completed fixes, recovery behavior, and remaining readiness checks. The original findings are in [AUDIT_FULL_PULL.md](AUDIT_FULL_PULL.md).

For monitored Windows execution with native file logging, use `& scripts/run_full_pull.ps1`.

Retry an unfinished snapshot from today with `& scripts/run_full_pull.ps1 -RepairSnapshot Sheets/PullRuns/<run-id>.json`. This preserves successful locations, uses the original date settings and destination, and archives the previous manifest before retrying incomplete work. Wait for the original process to finish first. A new calendar day requires a new snapshot.

Verify a completed snapshot with:

```powershell
.\.venv\Scripts\python.exe scripts/audit_snapshot.py Sheets/PullRuns/<run-id>.json --report Tests/full_pull_monitor/<run-id>_audit.json
```

See [PROVIDER_UPGRADE_VALIDATION.md](PROVIDER_UPGRADE_VALIDATION.md) for frozen parity evidence and [LIVE_FULL_PULL_RUN_REPORT.md](LIVE_FULL_PULL_RUN_REPORT.md) for live findings and release status.

After a repair finishes, compare its 56 output fields with both the initial and most recent stable same-date backups:

```powershell
.\.venv\Scripts\python.exe scripts/compare_snapshot_fields.py <backup-root> <snapshot-root> Tests/full_pull_monitor/field_parity.json
```

Removed records and changed fields return a nonzero status for source-backed review. Additions are reported separately. A source may legitimately update a field during a run; verify that change against the published page. Keep all captures and generated reports local.

Multi-sport acquisition and independent delivery:

- [Architecture audit](MULTISPORT_AUDIT.md) and [softball provider assessment](SOFTBALL_PROVIDERS.md).
- [Website contract and diamond-connected handoff](WEBSITE_INTEGRATION.md).
- [Windows daily execution and recovery](WINDOWS_DAILY.md).

The legacy baseball Excel contract remains 56 columns. Website synchronization owns separate revision queues and acknowledgements in `Delivery/`; Excel processed status cannot suppress it. NCS and TopGun fastpitch adapters are implemented behind separate recorded written-permission gates. NCS rejects yearless dates. Other softball providers remain research candidates, not verified nationwide coverage.

```powershell
# No network or runtime-state writes; inspect the unattended daily plan.
.\.venv\Scripts\python.exe scripts/daily_acquisition.py --dry-run

# Retry independently configured website receivers without scraping or Excel writes.
.\.venv\Scripts\python.exe scripts/sync_destinations.py --config delivery.local.json
```

Copy `delivery.example.json` to ignored `delivery.local.json` for endpoint configuration; store credentials in the scheduled account's environment. Both example destinations are disabled. The existing diamond-connected ingestion endpoint needs the correlated receipt/version wrapper described in the handoff. No Windows task is installed or activated by these commands.
