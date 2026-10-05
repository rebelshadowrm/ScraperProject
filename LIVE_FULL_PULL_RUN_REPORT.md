# Live full-pull verification

The October 4, 2026 production snapshot **passed release verification**. It refreshes existing events and includes valid starts strictly after the run date, with no end-date cutoff. Execution and GitHub publication after successful verification were authorized in the conversation.

The final repair began October 4 and finished shortly after midnight on October 5, retaining the original snapshot date and destination. The complete field comparison confirms no unexpected omissions.

## Verified output

- Run: `20261004_174122_96948744`; final manifest status `complete`.
- Coverage: **17 data providers, plus their Play365 dispatcher; all 722 locations complete**.
- Output: **3,610 location rows, 3,593 unique indexed events, 144 workbooks**. Cross-location event membership may repeat a unique event.
- 578 verified empty locations legitimately have no workbook.
- 1 explicitly unpublished-date source exclusion; no dates were invented.
- Snapshot audit: zero problems in coverage, failure counters, the 56-column schema, required fields, future dates, workbook counts, indexed identities, ledger membership, or export acknowledgements.
- Actual workbook comparison: 3,102 unchanged original rows, zero removed rows, 502 additions, and 6 reviewed changed records: two published source updates and four additional grouped-field recoveries.

Two records changed with their published source. Perfect Game group 25379 now starts October 10 rather than October 9; frozen and current parsers agree on the captured page. Group 26588 now redirects to its published 14U event 173260, with the updated title and URL; its old 13U detail explicitly says it does not exist or is not ready for publication. Fresh source captures confirm this division change. The venue, Open level, Team classification, and existing unknown-surface value remain consistent with the original grouped output. Group 26154's three published contact fields also match the original output. Baselines remain preserved.

Snapshot: `Sheets/Snapshots/20261004_174122_96948744`. Manifest and archived attempts: `Sheets/PullRuns/`. Detailed audit and field-comparison reports: ignored `Tests/full_pull_monitor/final_snapshot_audit.json` and `final_snapshot_field_parity.json`. Generated data and source captures are kept local.

## Preserved attempts and recovery

| Attempt | Result |
| --- | --- |
| Diagnostic `20261004_164913_0dc86e84` | Interrupted, not accepted: 177 location results, 473 rows, 12 validation failures, 89 extraction failures, one export acknowledgement failure. |
| Initial `20261004_174122_96948744` pass | Partial, exit 1: 322 location results, 3,108 rows. |
| First same-snapshot repair | Partial, exit 1: 722 location results, 3,608 rows; all providers except Perfect Game complete. |
| Second same-snapshot repair | Retained completed locations; final outstanding discovery/browser failures and contact quality were reviewed before another repair. |
| Third same-snapshot repair | Georgia completed, native exit 0; subsequent full-field review identified one omitted Texas event despite reported API completion. |
| Fourth repair | Native exit 0; recovered the omitted event. Subsequent field review found lost grouped fields after a source redirect. |
| Fifth repair | Restored grouped fields; partial, exit 1, after one unverified contact page. A fresh browser verified the published contact. |
| Sixth repair | Recovered group 26104; partial, exit 1, after two other intermittent detail failures. Fresh source verification led to a bounded browser retry. |
| Seventh repair | Complete, native exit 0; all release checks passed. Existing fields are preserved and the two failed groups are restored. |

The diagnostic PowerShell launcher failed while formatting redirected errors, leaving its scraper child running. Only that attempt's owned process tree was stopped; its manifest records interruption. Native logging now writes stdout/stderr directly and records the scraper's actual exit code. Five older durable pending exports were subsequently recovered and acknowledged at their original destinations. Ledgers were not reset.

Each repair preserves the original date and destination, archives its previous manifest, and holds the production ledger lock. Stable workbook/ledger copies before repairs are retained under ignored `Tests/full_pull_monitor/`. No overlapping production writers were started. Contact field differences were reviewed against source captures and a fresh browser; published data was recovered without overwriting production records with stale baseline values.

## Source-grounded fixes

- Game7/GrandSlam accept published trailing date years and verified empty contracts; NCS preserves explicit winter years. Valid four-digit years beyond 2099 remain supported.
- Perfect Game supplements legacy discovery with the paginated public API using original state/categories and no end date. Migration redirects during navigation, table waits, or pagination retain legacy links and require complete API results. Unknown filters, truncated totals, repeats, and malformed data remain failures. Supported categories are PGBA, WWBA, BCS, Super25, and PG Youth; other discovered legacy categories are preserved.
- Perfect Game's API overlaps some page boundaries. Captured 50-record pages contain 249 raw records but only 245 unique groups. The API supports at most 100 records per page. The fix uses that supported size and up to three bounded passes, requiring distinct groups to equal a stable reported total; gaps and changed identities remain partial. All 50 states passed fresh distinct-group verification. Group 26486 is recovered under the existing filters; the broader category experiment was not adopted. Known valid future Perfect Game URLs absent from the catalogue are rechecked against their fresh source in snapshot mode; cached payloads cannot certify an unavailable source.
- Perfect Game uses indexed hidden endpoint dates for winter rollover. Early past skips require explicit valid starts for every child. Group 23994 redirects to event 150423, explicitly marked `DATE TBD`; it is a durable, counted, retryable source exclusion, exportable when required dates become published.
- Perfect Game recognizes detail pages before accepting contact data. Incomplete bodies or missing contacts can recover from a verified public HTTP page; unverifiable bodies fail visibly. A fresh browser independently verified event 170402's published contact fields. HTTP availability is intermittent and cannot establish empty success.
- If both the initial browser body and HTTP fallback cannot be verified, Perfect Game retries that browser detail navigation once under the existing timeouts. Fresh source checks confirmed intermittent failures for events 169447 and 170426. Valid contact inputs incur no extra navigation; persistent unverified bodies still fail. Two regression tests verify exact recovery and the finite failure budget.
- A grouped Perfect Game URL that redirects to a single published division retains its verified venue and parenthesized competition level, plus the grouped Team/unknown-surface contract when eligibility confirms a team event. Direct single-event inputs retain exact 56-field baseline parity. Unpublished divisions are not copied from stale records.
- Inactive sessions and WebDriver out-of-memory errors reach the existing single fresh-driver retry. Ordinary source text does not trigger restart. Successful extraction recovery is counted separately from unresolved failures.
- Play365 uses the frontend's fresh CSRF refresh and event POST, retaining filters and identities. Generated exporters require complete bounded pagination. Verified fresh dumps can supply fallback before manual work is queued.
- Triple Crown recognizes its exact empty marker within the filtered schedule component. Explicit graduation-year/division labels, prices, invitations, and waitlists remain in additional information without invented numeric ages/fees. Individual classification requires explicit player eligibility or a camp/clinic title; unknown required metadata still fails.
- USSSA uses matching Event JSON-LD only for missing, invalid, or reversed visible dates. New Year's Bash spans December 28, 2026 to January 3, 2027. Montana's recognized redirect verifies the official national region filter and all published seasons before accepting explicit empty results. Unsupported nonempty responses fail.
- PST's browser request works where a plain HTTP probe did not; its working acquisition path was retained.

## Parity evidence and reusable checks

**126 offline tests passed in 185.593 seconds**, including frozen provider outputs/discovery, exporter execution, source contracts, identity, durable recovery, browser failure accounting, snapshot repair, and auditing. Earlier test counts are superseded, not additive.

- Play365: all 56 fields match for 35 live events across seven configurations and all 208 unique FiveTool Youth events in a complete 42-page capture; exporter execution covers 16 generated paths.
- Triple Crown: three live state feeds retain ordered URLs and metadata. Three corrected eligibility records validate; all 56 fields remain unchanged after the classification guard.
- USSSA: three valid captured Indiana pages retain all 56 fields; the corrected New Year row changes only its two dates. A populated Indiana control and the production Firefox path verify Montana's empty result across all 12 published baseball seasons.
- Perfect Game: frozen and candidate live discovery return the same 65 Alabama groups. Live ordering drifts; fixed-capture replay retains all 65 ordered links. Same-source grouped data and contact parsing match before/after the contact repair.
- The latest bounded-contact-retry fix also replays captured groups 26104 and 26155 through the frozen and current full parsers: all 56 fields match in both cases, with no network during replay. Before-source SHA-256 and results are retained in the ignored `pg_bounded_contact_retry/live_source_parity.json` report.
- The grouped-redirect upgrade preserves all 3,610 preceding rows: zero removals/additions and only source-verified recovered fields on five groups. Group 26588 regains venue, competition, type, and unknown surface; groups 25932, 26379, 26859, and 26937 regain venue and grouped Team/unknown-surface fields, with the published Open level also restored on 25932. Fresh browser captures verify all five. The contact retry preserves all 56 fields of its 3,608 existing rows and restores exactly the two failed groups 26154 and 26155. Three redirect tests cover direct-input parity, grouped fields, and redirect context.
- Date generalization: 36 frozen comparisons for 2026, 2027, and 2099 remain exact; year 2100 and winter rollover have explicit regressions.

`scripts/audit_snapshot.py` checks the final snapshot and ledger. `scripts/compare_snapshot_fields.py` checks every indexed import field against a stable same-date backup after writers stop. Removals and field differences return nonzero for source-backed review; published source updates and source-verified field recoveries are documented separately from exact parity.

All seven project skills validate and match installed copies. The release skill requires actual optional-field comparison as well as schema checks. Provider consolidation and an external importer were not invoked; template/import constraints were checked through tests and the snapshot audit.

## Remaining operational limitation

FiveTool Youth currently blocks unattended HTTP and the project's headless Firefox requests. The October 4 normal-browser export completed 42 nonempty pages and verified the terminal empty response. Its validated local dump supplies this snapshot and is ignored by Git. While blocking persists, subsequent pulls need a fresh normal-browser export; the 24-hour freshness guard prevents indefinite reuse. Browser defaults and access controls were preserved.
