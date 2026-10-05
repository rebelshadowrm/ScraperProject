---
name: scraper-live-parity
description: Compare ScraperProject HTTP and browser acquisition on bounded live provider samples before changing rendering, filters, layout, or season defaults. Use after offline provider parity passes; not for production seasonal sweeps.
---

Read PROVIDER_UPGRADE_VALIDATION.md and the affected provider's acquisition rules first. An offline fixture pass does not establish that live HTML contains browser-rendered fields or that different server filters return equivalent coverage.

Choose representative events and state/site filters for the specific proposed change, with a finite request/navigation count and time budget. Include the changed layout or host family, multiple divisions/venues when relevant, and a real empty state if available. Use temporary output, no production ledger/workbook writes, and preserve cookies, CSRF tokens, browser-observed season parameters, and source timestamps in protected local captures. Do not publish credentials in fixtures or reports.

Capture live responses once and replay both frozen baseline and candidate against the same artifacts where possible. For HTTP-versus-browser trials, compare actual received/rendered content and all required and optional exported fields, paired fees/ages, event identities, and location membership. Recheck drifting source data before attributing a difference to code. A blocked or unavailable source is unresolved evidence, not empty success or permission to drop fields.

Report exact sampled coverage, row differences, requests, browser navigations, and phase timings. Keep fixture-derived savings separate from observed live timings. Adopt a changed acquisition default only after the affected fields and coverage have parity; otherwise retain the existing path and record the pending experiment. Keep strict future-only dates and durable export recovery unchanged.
