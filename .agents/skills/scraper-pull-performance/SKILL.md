---
name: scraper-pull-performance
description: Measure and optimize ScraperProject ledger, source caches, and workbook throughput. Use after export recovery is reliable or for isolated provider caching fixes.
---

Read the current recovery implementation before optimizing `common/json_utils.py`, `common/runner.py`, `common/excel_utils.py`, or provider caches. The growing ledger historically underwent whole-schema scans and whole-file saves per event; benchmark current behavior instead of repeating old audit numbers as current measurements.

Validate legacy schema at load boundaries, update one event at a time, and checkpoint committed batches. Avoid trading fewer writes for unrecoverable extracted rows. Cache all-source feeds once per scraper run and use a separate loaded flag so a valid empty result is cached too. Check response shape and collection completeness before marking loaded. Index cached payloads by state only when repeated filtering is material.

Start browsers only where the current paths require them. Preserve browser fallback for PST. Increase workbook batch size only after replay tests pass. Add concurrency only with explicit session/driver ownership, per-host limits, and a safe single writer; mutable per-location caches cannot be shared freely.

Measure checkpoint, parsing, network, and export separately with temporary files. Report fixture size, repetitions, and actual times; distinguish extrapolated full-run savings from observed timings. Never run a production full pull merely to benchmark a local optimization.
