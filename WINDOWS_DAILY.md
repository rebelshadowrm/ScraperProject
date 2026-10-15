# Daily Windows acquisition

scripts/daily_acquisition.py is a noninteractive coordinator for the existing CLI
and independent website retry command. It does not register or enable a task.
scripts/acquire_unattended.py anchors ui.cli to the project root and propagates
its return status, independently of existing uncommitted main.py launch changes.
Use a dedicated operator account, a local project checkout and the tested virtual
environment. Install requirements.txt, including tzdata for America/Chicago on
Windows. Provider browser prerequisites remain those of the existing project.

```powershell
# Read-only plan: no source requests, locks, state or workbook writes.
.\.venv\Scripts\python.exe scripts/daily_acquisition.py --dry-run

# Authorized unattended acquisition, followed by independent website delivery.
.\.venv\Scripts\python.exe scripts/daily_acquisition.py --sport all --mode snapshot --config delivery.local.json

# Existing baseball-only operation while softball access is unresolved.
.\.venv\Scripts\python.exe scripts/daily_acquisition.py --sport baseball --config delivery.local.json
```

The all-sports coordinator default deliberately reports queued softball acquisition
for each provider whose written permission is missing. It never treats that blocker as complete
softball coverage. Legacy main.py --all still defaults to baseball. Softball may be
requested explicitly with main.py --all --sport softball or --sport all.
Set SCRAPER_NCS_FASTPITCH_PERMISSION_FILE to the local nonempty written permission
record only after provider permission for the intended acquisition/redistribution
has actually been obtained. Do not create a placeholder to defeat the gate.
TopGun fastpitch has its own SCRAPER_TOPGUN_FASTPITCH_PERMISSION_FILE gate because
its robots policy disallows this crawler. Provider gates are independent: one
blocked provider does not suppress another authorized provider. NCS also requires
an explicit year in the published date; the current captured yearless labels
remain failures until a permitted dated source is supplied.

## Task Scheduler settings for later operator approval

Prepare these settings in Task Scheduler; install/enable only with explicit approval:

- Trigger: daily at 03:00, on a Windows host configured for **Central Standard Time**
  (the Windows zone observing America/Chicago daylight saving changes). Use local
  wall time, not a fixed UTC offset. The scraper's existing today/run_date uses host
  local time, so that host zone is required for consistent future-date boundaries.
- Action program: absolute .venv\Scripts\python.exe path. Arguments: absolute
  scripts\daily_acquisition.py path, --sport all --mode snapshot --config followed
  by the quoted absolute local configuration path. Start in the repository root.
- Run whether the operator is logged on or not. Supply secrets securely to that
  account's process environment. No credentials in the task arguments, repository,
  config JSON, diagnostic reports or endpoint URLs.
- If task already running: **Do not start a new instance**. Enable **Run task as soon
  as possible after a scheduled start is missed** (StartWhenAvailable). A missed day
  needs one fresh current snapshot, not a backlog of old snapshots.
- Allow at least seven hours plus process cleanup. Python defaults bound acquisition
  at six hours and each website destination at thirty minutes; --acquisition-timeout/--delivery-timeout
  specify positive seconds. A scheduler outer deadline should exceed both combined.
  Use restart-on-failure conservatively; daily and ledger locks prevent overlaps.
- Retain task history and inspect the durable daily manifest even if Task Scheduler
  reports the process completed. Exit 1 means partial, not success.

## Guarantees and recovery

One OS-released Delivery/daily.lock spans both phases. Existing events.json locks
still serialize CLI and data-control writes. Destination commands also serialize
their own DB operations. No additional provider concurrency is introduced; current
provider/location isolation, pagination budgets, network/browser timeouts and
deferred manual fallback are reused.

Each acquisition/destination child has its own whole-process deadline. A timed-out
baseball receiver cannot consume the softball receiver's retry budget. A Windows Job Object with kill-on-close
contains its owned descendants (including browsers). Deadline or parent process
termination closes that job and terminates owned processes; unrelated sessions are
never enumerated or killed. Job setup errors fail the phase visibly. Windows handle
termination can take up to ten seconds of bounded cleanup after the deadline.

Delivery/runs/<run-id>/ contains stdout/stderr for acquisition and each destination,
plus a durable manifest.
Delivery/daily.json points to the latest run, Chicago local date, previous run ID,
missed day count, phase exit statuses and acquisition manifests. Provider coverage
and event failures remain in the existing Sheets/PullRuns and Logs records.

After a failed/interrupted acquisition, delivery still runs from durable acquired
payloads. A killed CLI manifest tagged with that coordinator ID becomes interrupted,
never complete. On the next invocation, abandoned manifests from the prior owned
run are reconciled under the original ledger lock. Same-day partial/interrupted
snapshot runs use --repair-snapshot and retain completed locations, date settings,
original workbook destination and manifest archives. New-day runs build a new
snapshot and replay old staged Excel work through the existing recovery path.
Ordinary successful daily reruns build another fresh snapshot. Provider permission
blockers do not stop unrelated provider repairs.

The daily process returns 0 only when both phases return 0; otherwise 1 (partial).
Setup, lock, invalid configuration or coordinator persistence failure returns 2.
Child timeout status is 124. An interrupted external termination may not have a
final daily receipt; running manifests remain evidence of unfinished work.

Back up the SQLite outbox through SQLite's backup API and protect local ledgers/logs
with the operator account's filesystem permissions. Retry unresolved websites with
scripts/sync_destinations.py without reacquiring sources or updating Excel. Consult
WEBSITE_INTEGRATION.md for endpoint setup, acknowledgement rules and replay.

## Isolated validation

--dry-run on both commands has no provider or website calls. main.py --all --test
is isolated state/output but **does contact providers**; it is not a network dry run.
The unittest suite uses temporary state, source fixtures and failure injection,
including a real owned Windows subprocess deadline/exit-status check. No production
pull, scheduled task installation or receiver deployment is part of this validation.
