---
name: scraper-pull-recovery
description: Fix and verify ScraperProject checkpointing, duplicate exports, and interrupted full-pull recovery. Use for changes to its event ledger or Excel commit path.
---

Work from the ScraperProject root, identified by `common/runner.py`, `ui/cli.py`, and `templates/Tournaments-Import-Template.xlsx`. Read `AUDIT_FULL_PULL.md` and `PULL_IMPROVEMENTS.md` when present to distinguish historical findings from completed changes.

Trace discovery -> extraction -> durable staging -> workbook export -> processed checkpoint. Keep source identity separate from public URLs. A workbook write and JSON replacement are separate commits: atomic file writes alone do not prevent duplicate replay. Persist payload and intended destination before export, and make replay idempotent by event identity. Do not mark events exported until export succeeds; distinguish workbook, checkpoint, and extraction failures.

Preserve the 56-column import sheet, repeated header mapping, dropdown tokens, and template formatting. Keep recovery metadata outside the import columns. Read old ledgers without blindly resetting processed flags. Recover pending payloads even when no longer rediscovered. Do not silently use new output destinations when replaying an unfinished export.

Use temporary state and template copies for failure injection. Verify failure before workbook save, failure after workbook save/before checkpoint, retry on another date, and interruption after staging. Require one intended row per identity after replay and explicit failure reporting. Do not rewrite production sheets or initiate a full pull as part of a local implementation test.
