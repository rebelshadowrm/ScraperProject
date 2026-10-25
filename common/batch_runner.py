"""Durable staging and idempotent workbook replay for event batches."""

import logging
import os
import random
import time
from collections import defaultdict

from common.driver_manager import cleanup_download_dir, is_driver_connection_error, restart_driver
from common.excel_utils import append_rows_to_template, create_workbook_from_template
from common.json_utils import add_or_update_failure, mark_processed, record_attempt, save_events, save_failed_events
from common.validation import is_future_event, validate_event_data
from common.pull_options import PullOptions
from common.logging_utils import log_progress
from common.normalize import normalize_row
from common.source_exclusions import SourceEventIncomplete
from common.sport import scraper_sport, payload_revision

logger = logging.getLogger(__name__)


def process_events_in_batches(
    *, scraper, driver, urls_to_process, events_data, failed_data, json_file,
    failed_events_file, output_file, columns, selected_location, download_dir,
    shutdown_check, batch_size=10, delay_between_batches=30, firefox_binary=None,
    pull_options=None,
):
    """Stage payloads before export and retain them until a durable acknowledgement."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    metrics = dict(validation_failures=0, extraction_failures=0, non_future_skipped=0, date_scope_skipped=0,
                   batches_attempted=0, batches_written=0, persistence_failures=0,
                   events_attempted=0, interrupted=False, source_incomplete_skipped=0, driver_restarts=0)
    total_written = 0
    options = pull_options or PullOptions()
    sport = scraper_sport(scraper)

    def failure(key, url, reason):
        add_or_update_failure(failed_data, name=key, url=url, location=selected_location, reason=reason)

    def resolve(keys):
        failed_data["failed_events"] = [item for item in failed_data["failed_events"]
                                         if item.get("name") not in keys]

    def persistence_failure(exc, pending, phase):
        metrics["persistence_failures"] += 1
        reason = f"{phase} failed: {str(exc) or type(exc).__name__}"
        logger.error("%s", reason, exc_info=True)
        for key in pending:
            entry = events_data["events"][key]
            entry["export_error"] = reason
            failure(key, entry["url"], reason)
        # Best effort: keep explicit failures if the unavailable destination recovers.
        for save, data, path in ((save_events, events_data, json_file),
                                 (save_failed_events, failed_data, failed_events_file)):
            try:
                save(data, path)
            except Exception:
                logger.exception("Could not persist recovery state to %s", path)

    def commit(pending):
        nonlocal total_written
        groups = defaultdict(list)
        for key in pending:
            destination = events_data["events"][key].get("export_file") or os.path.abspath(output_file)
            groups[destination].append(key)
        batch_failed = False
        for destination, keys in groups.items():
            try:
                if not os.path.exists(destination):
                    create_workbook_from_template(destination)
                append_rows_to_template(destination, [events_data["events"][key]["event_data"] for key in keys],
                                        columns, event_keys=[events_data["events"][key].get("export_identity", key)
                                                             for key in keys])
            except Exception as exc:
                persistence_failure(exc, keys, "Workbook export")
                batch_failed = True
                break
            total_written += len(keys)
            previous = {key: dict(events_data["events"][key]) for key in keys}
            for key in keys:
                mark_processed(events_data, key=key)
                entry = events_data["events"][key]
                entry.update(export_status="exported", export_error="",
                             excel_delivered_revision=entry.get("payload_revision") or payload_revision(entry["event_data"]))
            try:
                save_events(events_data, json_file)
            except Exception as exc:
                # Leave in-memory and on-disk state replayable after an ambiguous commit.
                events_data["events"].update(previous)
                persistence_failure(exc, keys, "Export acknowledgement")
                batch_failed = True
                break
            resolve(set(keys))
            try:
                save_failed_events(failed_data, failed_events_file)
            except Exception as exc:
                persistence_failure(exc, [], "Failure-ledger acknowledgement")
                batch_failed = True
                break
        if pending and not batch_failed:
            metrics["batches_written"] += 1
        return not batch_failed

    deferred = []
    for offset in range(0, len(urls_to_process), batch_size):
        if shutdown_check():
            metrics["interrupted"] = True
            break
        metrics["batches_attempted"] += 1
        log_progress(logger, "Processing %s batch %d/%d.", selected_location,
                     offset // batch_size + 1, (len(urls_to_process) + batch_size - 1) // batch_size)
        pending = []
        expired = []
        for item in urls_to_process[offset:offset + batch_size]:
            if shutdown_check():
                metrics["interrupted"] = True
                break
            key, url = item["name"], item["url"]
            entry = events_data["events"][key]
            if sport != "baseball" and entry.get("export_status") == "pending":
                raise ValueError("Softball cannot replay a legacy baseball Excel export")
            if entry.get("export_status") == "pending" and isinstance(entry.get("event_data"), dict):
                pending.append(key)
                continue
            for attempt in range(2):
                try:
                    metrics["events_attempted"] += 1
                    payload = scraper.extract_event_details(url, driver=driver, selected_location=selected_location)
                    if not payload:
                        metrics["extraction_failures"] += 1
                        record_attempt(events_data, key=key, status="failed", error="Extraction failed")
                        failure(key, url, "Extraction failed")
                        break
                    valid, reason = validate_event_data(payload)
                    if not valid:
                        metrics["validation_failures"] += 1
                        record_attempt(events_data, key=key, status="failed", error=f"Validation failed: {reason}")
                        failure(key, url, f"Validation failed: {reason}")
                        break
                    payload = normalize_row(payload)
                    record_attempt(events_data, key=key, status="success")
                    entry.pop("source_exclusion", None)
                    entry["event_data"] = payload
                    entry.update(sport=sport, payload_revision=payload_revision(payload))
                    entry["sport_verified"] = True
                    if hasattr(scraper, "events_by_url"):
                        # The existing Play365 baseball contract uses sport_id=1.
                        # Other/missing IDs are not translated into a guessed sport.
                        source_sport = scraper.events_by_url.get(url, {}).get("sport_id")
                        entry.update(source_sport_id=source_sport,
                                     sport_verified=sport == "baseball" and type(source_sport) in (int, str)
                                     and source_sport in (1, "1"))
                        entry.setdefault("source_details", {})["sport_id"] = source_sport
                    elif scraper.__class__.__name__ == "PSTScraper":
                        entry["sport_verified"] = False  # organization filters do not prove sport
                    if hasattr(scraper, "get_event_identity"):
                        entry["source_event_id"] = scraper.get_event_identity(url)
                    if hasattr(scraper, "source_details"):
                        entry["source_details"] = {**(payload.get("source_details") or {}), **scraper.source_details.get(url, {})}
                    if sport != "baseball":
                        # Durable acquisition independent of the baseball workbook contract.
                        eligible = options.includes(payload)
                        if not eligible:
                            future = is_future_event(payload)
                            metrics["non_future_skipped" if not future else "date_scope_skipped"] += 1
                        entry.update(acquisition_status="ready" if eligible else "filtered", export_status="not_applicable")
                        expired.append(key)  # resolve earlier extraction failures after staging
                        break
                    if not options.includes(payload):
                        future = is_future_event(payload)
                        metrics["non_future_skipped" if not future else "date_scope_skipped"] += 1
                        entry["export_status"] = "expired" if not future else "filtered"
                        mark_processed(events_data, key=key)
                        expired.append(key)
                    else:
                        entry.update(export_status="pending", export_file=os.path.abspath(output_file),
                                     export_location=selected_location,
                                     processed=False, processed_at="", export_error="")
                        pending.append(key)
                    break
                except SourceEventIncomplete as exc:
                    metrics["source_incomplete_skipped"] += 1
                    record_attempt(events_data, key=key, status="source_incomplete", error=str(exc))
                    entry.update(processed=False, export_status="source_incomplete", source_exclusion=str(exc))
                    resolve({key})
                    logger.warning("Source event excluded until required information is published: %s: %s", url, exc)
                    break
                except KeyboardInterrupt:
                    metrics["interrupted"] = True
                    break
                except Exception as exc:
                    record_attempt(events_data, key=key, status="failed", error=str(exc))
                    if (attempt == 0 and is_driver_connection_error(exc)
                            and getattr(scraper, "REQUIRES_DRIVER", True) and driver is not None):
                        try:
                            driver = restart_driver(driver, firefox_binary=firefox_binary,
                                                    download_dir=download_dir, headless=True)
                            metrics["driver_restarts"] += 1
                        except Exception as restart_error:
                            metrics["extraction_failures"] += 1
                            failure(key, url, f"Driver restart failed: {restart_error}")
                            break
                        continue
                    metrics["extraction_failures"] += 1
                    failure(key, url, str(exc))
                    break
            if metrics["interrupted"]:
                break

        # Acknowledgements cannot precede durable payload and destination staging.
        resolve(set(expired))
        try:
            save_events(events_data, json_file)
            save_failed_events(failed_data, failed_events_file)
        except Exception as exc:
            persistence_failure(exc, pending, "Staging checkpoint")
            break

        if options.mode == "snapshot":
            deferred.extend(pending)
            batch_failed = False
        else:
            batch_failed = not commit(pending)
        cleanup_download_dir(download_dir)
        if batch_failed or metrics["interrupted"] or shutdown_check():
            metrics["interrupted"] = metrics["interrupted"] or shutdown_check()
            break
        if offset + batch_size < len(urls_to_process):
            delay = getattr(scraper, "BATCH_DELAY_SECONDS", delay_between_batches)
            deadline = time.monotonic() + (delay + random.uniform(0, 5) if delay > 0 else 0)
            while time.monotonic() < deadline and not shutdown_check():
                time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    if deferred and not metrics["persistence_failures"]:
        commit(deferred)
    return driver, total_written, metrics
