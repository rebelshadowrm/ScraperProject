---
name: scraper-event-identity
description: Repair ScraperProject event identity, provider collisions, seasonal refresh, and snapshot selection. Use when events disappear, merge, stay stale, or are exported under the wrong season.
---

Locate ScraperProject from `scrapers/base_scraper.py` and `common/runner.py`; inspect the current audit and improvement tracker before editing.

Follow provider collection, cache keys, runner keys, ledger provenance, and Excel identity together. Prefer a documented or observed stable provider ID; never invent an ID field without response evidence. Where ID is absent, choose a deterministic fallback that preserves distinct tournaments and report its limits. Keep public website URLs independently usable.

Preserve URL path/query case. Do not substitute existing human-readable `get_event_key()` methods for globally unique IDs without checking their contracts. Migrate legacy ledger entries conservatively; an ambiguous old key must not silently become authoritative for multiple new events.

Make incremental versus refresh/snapshot behavior explicit. Persist normalized payloads, refresh eligible events without duplicate append, and scope output to a chosen date interval. Keep the existing strictly-future rule unless the user authorizes a change. Soft-deleted entries stay excluded. Location membership and source identity are separate concepts.

Use fixture-driven tests for equal dates/location/type with different names or IDs, case-sensitive URLs, a changed processed event, and a URL reused next season. Do not clear production processed flags or delete duplicate production rows to make tests pass.
