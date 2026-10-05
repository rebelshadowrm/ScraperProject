---
name: scraper-provider-parity
description: Verify ScraperProject provider upgrades against frozen before-change outputs and request counts. Use when changing scraper transport, caching, pagination, or extraction while preserving event coverage and import data.
---

Freeze the current working-tree scraper files before editing; HEAD may precede other authorized full-pull fixes. Keep source copies in ignored Tests/parity_baseline and record SHA-256 provenance with tracked golden outputs. Never regenerate expected outputs from changed code merely to make tests pass.

Run identical deterministic HTML/JSON/browser fixtures through the baseline and candidate. Compare ordered discovery links, provider identities, and every output column, including optional fields, paired age/fees, all locations, and contacts. Fix the clock for date-dependent paths. Use real provider parsing rather than mocking the parser to return the expected row. Block unintended network access during replay. Fixture parity proves those cases, not live provider equivalence.

Record request/navigation counts separately from rows. Require a reduction for optimizations targeting redundant loads; do not claim elapsed full-season savings from request counts. Cache keys must include relevant provider, state, organization, filters, and source revision. Test empty-success caching, failed-fetch retry, repeated extraction, and cache replacement.

For correctness fixes, preserve valid-input parity and add explicit new expectations for malformed, blocked, incomplete, or disconnected inputs. A baseline's silent failure is not behavior to preserve. Document each intentional status change. Keep strict future-only dates, date-bound counters, identity, output schema, and durable pending-export recovery intact.

Use `test_suite/provider_replay.py` to replay fixtures and `test_suite/test_provider_parity.py` to compare frozen outputs. Read the fixture coverage and limitations in PROVIDER_UPGRADE_VALIDATION.md before relying on a pass. Extend scenarios for each changed source layout. The existing recovery suite does not prove provider field parity.

Apply one narrow upgrade at a time, run its parity and failure tests, then run the infrastructure suite when provider-runner contracts change. For HTTP/browser substitutions, changed server filters, or season/layout defaults, require bounded live comparison with isolated output before adoption. If evidence is insufficient, retain the existing acquisition path and record the unresolved experiment. Do not run production full pulls or rewrite production ledgers to obtain benchmarks.

When a source publishes nonnumeric divisions or graduation-year eligibility, preserve the explicit labels, fees, and registration restrictions in the existing additional-information field. Do not translate graduation years into U ages or assign fees to invented age groups. Require labelled source evidence before relaxing a provider-specific age guard; missing email/state and unknown eligibility remain failures. Compare valid numeric-division fixtures unchanged, and test new labelled cases plus unlabelled/unknown rejection. Preserve existing listing/detail date priority while recording genuine source discrepancies.
