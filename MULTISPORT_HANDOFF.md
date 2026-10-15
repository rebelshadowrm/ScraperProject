# Multi-sport implementation handoff

Branch: dev/multisport-independent-delivery. Audit milestone: 7950bad.
Implementation milestone: dcfe9f1. No shared history was rewritten.

## Delivered

- Architecture/provider/identity/ledger/manifest/recovery audit in
  MULTISPORT_AUDIT.md, with a separate assessment of every requested softball
  organization in SOFTBALL_PROVIDERS.md.
- Sport-scoped softball identities and durable source metadata through the shared
  runner, normalization and staging paths. Existing baseball identities remain
  unchanged. Dedicated typed softball adapters bypass legacy baseball Excel.
  Existing untyped mixed-sport Excel records are retained for compatibility;
  their website routing requires independently verified sport provenance.
- Temporary backfill caught a real fastpitch row in the legacy PlayPPS baseball
  workflow. Untyped Play365/PST payloads are now quarantined for websites until
  sport evidence is verified. Fresh Play365 extraction records observed sport_id;
  unknown/other IDs are never guessed into softball or admitted as baseball.
- NCS and TopGun fastpitch subclasses reuse existing adapters. Each is disabled by
  default with an independent recorded provider-permission gate. Fees, eligibility,
  additional venues and published organizer contacts are retained. NCS will not
  substitute a current/season year for a source date that omits its year.
- Transactional SQLite website revision/outbox state, separate from legacy Excel
  acknowledgements. Exact correlated receipts, stable replay keys, changed content,
  stale acknowledgements, explicit withdrawals, unknown responses, interrupted
  requests and per-destination retries are covered.
- Daily Windows coordinator with an OS lock, serial provider processing, independent
  destination process budgets, owned Windows Job Object cleanup, durable logs,
  Chicago-date metadata, missed-day accounting and existing same-day snapshot repair.
- Nonmutating dry-run commands, disabled endpoint examples, receiver contract and
  a source-pinned read-only diamond-connected integration handoff.

## Verification

The baseline working tree passed 128 tests. That includes two pre-existing local
launch tests, which are preserved but not committed in this branch. Frozen source
hashes are tracked in test_suite/fixtures/multisport_baseline_provenance.json;
before-change sources and original edits remain under ignored
Tests/parity_baseline/multisport. Existing golden provider outputs were not regenerated.

The final isolated staged tree passed **156 tests in 305.411 seconds**, with an
explicit native exit-code receipt of **0**. This includes all 126 originally tracked
tests and 30 new tests for delivery, sport separation and unattended execution.
The published source was tested independently of the pre-existing local launch
changes. Logs and the machine-readable receipt are in
Tests/multisport_staged_tree/sport-provenance-verification.log and
sport-provenance-verification-receipt.json. Expected fixture-injected exceptions
appear in the log; the suite result is OK. The new receiver-selection, launcher,
mixed-feed quarantine and acquisition checks also passed their focused suites.

The 56-column OUTPUT_COLUMNS, common/excel_utils.py and the workbook template have
no changes against the original commit 160be44. Template preflight passes. Template
SHA-256: 1b2d54e69225833056d8fb4654e4f46861be10e0a992306d9f7e05ee557143ef.
Regression coverage includes frozen provider field/discovery parity, atomic writes,
checkpoint failure after workbook save, original-destination recovery, snapshot
repair and duplicate prevention. Compilation and git diff --check pass.

Both daily and destination dry-run commands were exercised without provider/website
requests or runtime-state writes. Daily tests exercise actual Windows subprocess
exit status and deadline termination in temporary directories. No task was installed.

Final read-only production-ledger backfill into a temporary DB stages 2,838 future
baseball events and quarantines 517 untyped payloads (including some already expired
records). It stages no softball events from unverified legacy sport provenance.
Both website JSON materializations were created locally; zero deliveries were
acknowledged, zero network requests were made and production state was unchanged.
The report is Tests/multisport_research/final_backfill_validation.json. Quarantine
is visible partial work requiring source evidence, not a website acknowledgement.

## Bounded source evidence and limits

- TopGun: two structured responses plus robots.txt; feed contained 128 SportID=2
  records and 84 SportID=1 records. Stable TournamentID and explicit ISO start/end
  dates are present. Counts are raw feed records, not validated nationwide coverage.
  Its robots policy disallows this crawler; no further probes or softball rollout.
- NCS: robots, initial listing, terms, corrected countryState=US-TX listing and four
  subpages of one event were captured (eight requests total). The initial state=TX
  parameter was not the supported filter and was not adopted. Local replay recovered
  four age/fee pairs, C eligibility, contacts and two venues from the same captured
  pages; the only intentional mapped field differences were four fees and additional
  information. The captured date label lacks a year. An initial inherited-parser
  replay is superseded by the explicit-year guard; it is not accepted live date
  evidence. Written redistribution permission and a reliable dated feed are needed.
- Triple Crown: homepage and robots were inspected; official fastpitch event pages
  and indexed schedules identify a viable future extension. No sport ID was guessed
  and no new adapter was enabled without feed/layout/date evidence.
- PGF: direct robots request returned 403; no further automated probing. Other
  requested organizations were assessed using linked official sources. Search-index
  visibility does not establish live extraction, coverage, freshness or permission.

Live captures/replay reports remain local under Tests/multisport_research. No full
production pull, production ledger reset, historical workbook cleanup, website
deployment or website-repository modification was performed.

## Remaining external acceptance gates

1. Obtain and record source permission for TopGun/NCS acquisition and intended
   Connected redistribution. Supply permitted explicit-year NCS data. Then perform
   bounded acquisition-path verification and representative coverage checks. Existing
   fixtures prove parser behavior, not nationwide live coverage.
   Verify sport provenance for the quarantined legacy Play365/PST payloads; do not
   set sport_verified by title/organization guesses or force-reset the website queue.
2. Implement the documented correlated receipt/version wrapper in diamond-connected
   around its current ingestion service. Its existing /api/ingestion/batches returns
   aggregate HTTP 202 receipts and is not the new sender protocol. Configure separate
   destination credentials/endpoints and validate duplicate/change/timeout/recovery
   cases against disposable website databases before any production delivery.
3. Run the actual legacy Baseball Connected importer smoke test with a generated
   representative workbook. Schema, golden output and recovery parity passed locally;
   access to that external importer was not available for end-to-end acceptance.
4. After operator approval, install a daily 03:00 America/Chicago task using
   WINDOWS_DAILY.md. Check the host timezone, account environment and provider browser
   prerequisites; do not treat task activation as an implementation test.

Remaining softball provider adapters are candidates, not silently enabled or claimed
complete. Measure coverage by state, date, division and unique provider identity before
selecting further providers. Preserve unknown eligibility and all actual fees/venues.

The existing local README/main.py/ui/cli.py edits and untracked launch test remain
outside these commits. Their added/removed tracked lines match the original saved
patch. All newly committed runtime state paths are ignored; no credentials or source
captures were published.
