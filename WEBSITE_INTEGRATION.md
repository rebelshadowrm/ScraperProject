# Independent Connected delivery contract

The canonical acquisition ledger stays in events.json; the legacy Excel writer
keeps its existing flags, payload staging, original destination and identity index.
New Excel commits also record excel_delivered_revision. An old processed flag is
not upgraded into a website receipt. The 56 OUTPUT_COLUMNS and template are unchanged.

Website canonical revisions and outboxes live in ignored Delivery/state.sqlite3
(SQLite WAL, synchronous FULL). Treat the DB, WAL and SHM files as one live store;
use SQLite backup or stop all writers before copying it. Do not delete this store
to retry deliveries: its per-event sequence protects receiver ordering.

## Canonical and destination state

Each event has an opaque event_id, provider, source_id, public source_url, sport,
operation, fields and source_details. fields carries the original 56-column row.
source_details preserves richer source divisions, eligibility, paired fees and
all venues. Sport is metadata, never a 57th Excel column. Existing baseball keys
remain unchanged. Softball uses a sport-scoped identity; it cannot adopt an old
baseball key or export checkpoint. Different providers with the same numeric ID
remain distinct. Do not fuzzy-merge tournaments by name/date/location.

Legacy rows from the known configured baseball provider classes are backfilled
under their original project baseball contract, provided their source URL does
not use a fastpitch route. Missing/unknown provenance is quarantined and reported.
Operators must resolve provenance rather than assigning sport from a title.
Website ingestion does not use processed or export_status as delivery filters.

The two website destinations are baseball_connected and softball_connected.
They own desired_revision/desired_sequence, delivered_revision/delivered_sequence,
acknowledged_at, status, attempts, retry_at and last_error independently. The
legacy Excel destination owns its existing processed_at/export_status/export_file,
export_error, attempt history, staged payload and excel_delivered_revision in
events.json. No website code writes those fields.

Website revision is SHA-256 over sorted compact UTF-8 JSON of the canonical body
before adding destination/revision/sequence/idempotency_key. The monotonic sequence
advances when content changes, including when content reverts to an earlier digest.
Do not hash titles or dates to create event identity. The idempotency key hashes
destination, event_id, revision and sequence. A repeated request retains that key.
The local revision digest is independent of a website's normalized checksum.

Latest-state synchronization coalesces unsent intermediate revisions into the
latest desired revision. Previous acknowledged revision and attempt history remain
available. This is an event-state synchronizer, not a full historical event journal.

## Receiver request and acknowledgement

Configure a dedicated HTTPS receiver with a distinct environment-held bearer token
for each destination. Endpoint URLs cannot contain embedded credentials, query
parameters or fragments. Redirects are disabled; secrets are never persisted in
the outbox, reports or task arguments. A POST is one event envelope:

```json
{
  "schema_version": 1,
  "destination": "softball_connected",
  "event_id": "NCSFastpitchScraper|softball|13689",
  "provider": "NCSFastpitchScraper",
  "source_id": "13689",
  "source_url": "https://www.playncs.com/fastpitch/Events/Details/13689/spooky-bats-4gg-game-balls-provided",
  "sport": "softball",
  "operation": "upsert",
  "fields": {"Tournament-name": "Example", "Start-date": "10/17/2026"},
  "source_details": {"divisions": [], "locations": []},
  "revision": "<sha256>",
  "sequence": 1,
  "idempotency_key": "<sha256>"
}
```

The fields above are abbreviated; actual envelopes contain all 56 fields. Send
Authorization: Bearer <token> and Idempotency-Key matching the body. Only HTTP 200
with the following exact correlated receipt acknowledges this revision:

```json
{
  "status": "committed",
  "destination": "softball_connected",
  "event_id": "NCSFastpitchScraper|softball|13689",
  "revision": "<the submitted sha256>",
  "sequence": 1,
  "idempotency_key": "<the submitted sha256>"
}
```

The receiver must commit event staging and the idempotency receipt in the same
database transaction before responding. committed means durable staging, not
public publication. Preserve review-required and validation-failed outcomes;
never use this receipt to bypass the website's curator or quality controls.
Do not return committed when the item failed validation or was discarded.

Receiver requirements:

1. Authenticate and bind the credential to one destination/sport. Reject mismatches.
2. Validate schema, identity, digest, source URL and allowed operation. Bound payload
   size and preserve explicit source eligibility and organizer contacts.
3. Lock the destination/event row. Enforce unique destination/event identity and
   destination/idempotency receipt; reject conflicting content for one key/sequence.
4. Repeated committed keys return the original correlated receipt without applying
   the event again. Higher sequences update staging; older sequences never overwrite
   a newer revision. Preserve/reply from their durable receipts when available.
5. Record upsert/withdraw revisions and moderation outcomes transactionally. Explicit
   source deletion or a previously staged event's expired start creates withdraw.
   Missing records or incomplete discovery do not withdraw anything.
6. After commit, return the receipt. A request timeout after commit is resolved by
   replaying the identical key. Public listing state remains governed by website
   review and attribution rules, including withdrawal review where required.

For an empty DB restore or lost receiver receipts, pause senders, restore both event
and receipt state consistently, then replay that destination. Requeue does not reset
local sequences. Replacing the sender DB requires sequence reconciliation with the
receiver; never start sequence 1 against an already populated receiver silently.

## Sender recovery and commands

Each attempt is durably staged before POST. Timeout, malformed/truncated receipt,
202/204, redirect or mismatched response stays unknown. HTTP 4xx/5xx is failed.
A killed in_flight attempt is reported unknown and becomes due after its recorded
retry time. Backoff starts at one minute and is bounded; each destination gets its
own attempt limit. A valid response followed by a local checkpoint failure remains
replayable. No file creation or request attempt acknowledges a website.

```powershell
# No network, locks, database creation or output mutation.
.\.venv\Scripts\python.exe scripts/sync_destinations.py --dry-run

# Copy the example, then set each receiver and environment variable name.
# Keep actual credentials in the scheduled account's environment/secret store.
.\.venv\Scripts\python.exe scripts/sync_destinations.py --config delivery.local.json --report Delivery/last-sync.json

# Materialize separate JSON envelopes; this does not acknowledge delivery.
.\.venv\Scripts\python.exe scripts/sync_destinations.py --export-dir Delivery/Exports

# Reconcile only one destination, retaining event IDs, revisions and sequences.
.\.venv\Scripts\python.exe scripts/sync_destinations.py --config delivery.local.json --destination softball_connected --replay softball_connected
```

Disabled/unconfigured destinations retain pending work and make the status partial.
Exit 0 means complete (or dry_run), 1 means unresolved/quarantined delivery, and 2
means command/store failure. Retry is possible without source acquisition or Excel.
Local materialization with unresolved deliveries intentionally returns 1.

## diamond-connected integration handoff

Read-only review of rebelshadowrm/diamond-connected master at
74ffed04a9bb18831e3553450660e25dbf2a05e3 on 2026-10-14 found
Nuxt/Nitro, PostgreSQL/Drizzle, shared/scraper/contracts.ts,
server/api/ingestion/batches.post.ts, server/utils/ingestion-batch.ts,
server/utils/scraper-normalize.ts and scripts/push-scraper-output.py.
Its existing machine boundary is POST /api/ingestion/batches using X-Ingestion-Key,
returning HTTP 202 with aggregate runId/received/accepted/duplicates/invalid metrics
after a database transaction. It already normalizes the wide rows and stages
review/quality outcomes. Its bridge owns separate atomic receipts.

**That current endpoint is not this correlated receipt protocol. Do not point the
new sender at it and interpret 202 as delivered.** No website files were changed.
Required website work should reuse that ingestion service behind a new receiver
wrapper (for example server/api/acquisition/events.post.ts), rather than creating
a second normalization/moderation pipeline:

- Extend shared contracts with explicit sport/destination, source event identity,
  source divisions/locations, operation, sequence and correlated receipt fields.
- Add a Drizzle migration for per-destination event version and idempotency receipts;
  keep event staging and receipt inside the same transaction as ingestion.
- Map a provider/sport to a stable ingestion source key; preserve the established
  externalKey/source attribution and prevent baseball/softball source collisions.
  Use the original event_id as externalKey where its length fits the current limit
  (500), or maintain an explicit stable identity mapping. Never truncate identities.
- Authenticate the wrapper per destination, convert fields to existing row payloads,
  map additional venues to the existing locations contract, and preserve raw B/C,
  graduation-year and invitation/waitlist labels. Do not relabel softball classes.
- Record validation/moderation outcomes, implement version ordering and explicit
  withdrawal handling, and return the exact receipt only for durable valid staging.
  Existing aggregate 202 responses can remain compatible for older clients.
- Keep separate website instances/databases or explicit destination/sport constraints
  across staging, source mappings, listing queries and receipts. Do not assume the
  present baseball-only public directory already supports softball.
- Test duplicate key, changed revision, stale replay, invalid item, timeout after
  commit, withdrawal and sport mismatch in a disposable local website database.

Receiver code, migration, endpoint configuration, credentials and end-to-end
acceptance remain website-side work. This task neither modified that repository
nor contacted/deployed production website services.
