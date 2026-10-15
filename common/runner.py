# common/runner.py

import os
import logging
from datetime import datetime
from typing import List, Dict, Any, Callable, Optional

from common.logging_utils import log_progress
from common.identity import normalize_url, event_identity, discovered_identities
from common.excel_utils import create_workbook_from_template, get_output_filename, write_snapshot
from common.pull_options import PullOptions
from common.collection import CollectedLinks
from common.sport import scraper_sport
from common.validation import validate_event_data
from scrapers.play365_base import Play365ManualFallbackRequired

from common.json_utils import (
    save_events,
    save_failed_events,
    add_or_update_event,
    add_or_update_failure,
)
from common.scheduler_history import (
    load_scheduler_history,
    save_scheduler_history,
    record_location_run,
)

from common.driver_manager import (
    is_driver_connection_error,
    restart_driver,
)
from common.batch_runner import process_events_in_batches

logger = logging.getLogger(__name__)


def _can_use_driver(scraper, driver) -> bool:
    return bool(getattr(scraper, "REQUIRES_DRIVER", True) and driver is not None)


def _should_log_source_init(scraper) -> bool:
    return bool(
        getattr(scraper, "DYNAMIC_LOCATIONS", False)
        and hasattr(scraper, "_all_events_cache")
        and not getattr(scraper, "_all_events_cache", None)
    )


def ensure_template(output_file: str) -> bool:
    """Copy the Excel template to output_file if it doesn't exist yet. Returns True if created."""
    if not os.path.exists(output_file):
        create_workbook_from_template(output_file)
        return True
    return False


def process_location(
    *,
    location_name: str,
    base_url: str,
    scraper,
    driver,
    events_data: Dict[str, Any],
    failed_data: Dict[str, Any],
    output_columns: List[str],
    json_file: str,
    failed_events_file: str,
    download_dir: str,
    shutdown_check: Callable[[], bool],
    firefox_binary: Optional[str] = None,
    output_base_folder: str = "Sheets",
    collect_kwargs: Optional[Dict[str, Any]] = None,
    scheduler_history_file: str = "scheduler_history.json",
    run_mode: str = "live",
    pull_options: PullOptions | None = None,
    run_date: str | None = None,
):
    """
    Process a single location:
      1) collect links
      2) add/update events in JSON (with timestamps/provenance)
      3) filter unprocessed for this location
      4) ensure output template exists
      5) batch scrape + append to Excel
    """
    location_started_at = datetime.now()
    options = pull_options or PullOptions()
    sport = scraper_sport(scraper)

    if _should_log_source_init(scraper):
        log_progress(logger, "Initializing event source for %s...", getattr(scraper, "SCRAPER_NAME", scraper.__class__.__name__))
    else:
        log_progress(logger, "Processing location: %s", location_name)

    for attempt in range(2):
        try:
            new_links = scraper.collect_event_links(driver, base_url=base_url, **(collect_kwargs or {}))
            if not isinstance(new_links, list):
                raise ValueError("Event discovery must return a list of links")
            break
        except Play365ManualFallbackRequired:
            raise
        except Exception as exc:
            if attempt == 0 and is_driver_connection_error(exc) and _can_use_driver(scraper, driver):
                logger.warning("Driver disconnected during discovery; restarting once.")
                driver = restart_driver(driver, firefox_binary=firefox_binary, download_dir=download_dir, headless=True)
                scraper._active_driver = driver
                continue
            logger.exception("Discovery failed for %s", location_name)
            new_links = CollectedLinks(complete=False, error=str(exc))
            break
    # A catalogue can omit a still-published future event. Recheck its actual
    # source in snapshot mode; never export the cached payload merely to keep it.
    if options.mode == "snapshot" and getattr(scraper, "REFRESH_KNOWN_FUTURE_EVENTS", False):
        for entry in events_data["events"].values():
            payload = entry.get("event_data")
            if (entry.get("scraper") == scraper.__class__.__name__
                    and location_name in entry.get("locations", [entry.get("location")])
                    and not entry.get("is_deleted") and isinstance(payload, dict)
                    and validate_event_data(payload)[0] and options.includes(payload)
                    and entry.get("url") and entry["url"] not in new_links):
                new_links.append(entry["url"])
                logger.info("Rechecking known future event absent from listing: %s", entry["url"])
    collection_complete = getattr(new_links, "complete", True)
    collection_error = getattr(new_links, "error", "")
    collection_non_future = getattr(new_links, "non_future_skipped", 0)
    if type(collection_non_future) is not int or collection_non_future < 0:
        raise ValueError("Discovery non-future skip count must be a nonnegative integer")

    source_label = getattr(scraper, "_last_collection_source", "") or base_url
    logger.info("Collected %d event links using %s for %s.", len(new_links), source_label, location_name)

    if getattr(scraper, "DYNAMIC_LOCATIONS", False):
        log_progress(logger, "Filtering loaded events into output: %s", location_name)

    # 2) Add/update events with richer schema
    scraper_name = scraper.__class__.__name__
    discovered_now = discovered_identities(events_data, failed_data, scraper, new_links, location_name)
    for link in new_links:
        norm_key = event_identity(scraper, link)

        add_or_update_event(
            events_data,
            key=norm_key,
            url=link,
            location=location_name,
            scraper=scraper_name,
            discovered_from=base_url,
        )
        entry = events_data["events"][norm_key]
        if entry.get("sport", sport) != sport:
            raise ValueError("Event sport conflicts with stored provenance")
        entry["sport"] = sport

    save_events(events_data, json_file)

    # Rediscovered records are refreshable; durably staged exports remain replayable
    # even if the listing no longer contains them.
    unprocessed_events = [
        {"name": key, "url": details["url"]}
        for key, details in events_data["events"].items()
        if (not (details.get("processed", False) if sport == "baseball" else
                 details.get("acquisition_status") == "ready") or options.mode != "incremental")
        and (details.get("scraper") == scraper_name or
             (sport == "baseball" and not details.get("scraper") and details.get("export_status") == "pending"))
        and details.get("sport", "baseball") == sport
        and (details.get("location") == location_name or location_name in details.get("locations", []))
        and not details.get("is_deleted", False)
        and (key in discovered_now or details.get("export_status") == "pending")
    ]

    stale_carryover_count = sum(
        1
        for key, details in events_data["events"].items()
        if not details.get("processed", False)
        and details.get("location") == location_name
        and not details.get("is_deleted", False)
        and key not in discovered_now
        and details.get("export_status") != "pending"
    )
    if stale_carryover_count:
        logger.info(
            "Skipping %d stale unprocessed event(s) for %s because they were not rediscovered in the current run.",
            stale_carryover_count,
            location_name,
        )

    # Deduplicate by provider event identity.
    seen = set()
    unique_events_to_process = []
    for ev in unprocessed_events:
        norm = ev["name"]
        if norm not in seen:
            unique_events_to_process.append(ev)
            seen.add(norm)

    logger.info("Total unique events to process for %s: %d", location_name, len(unique_events_to_process))

    if len(unique_events_to_process) == 0:
        logger.info("No unprocessed events for %s. Skipping Excel creation.", location_name)
        if options.mode == "snapshot" and sport == "baseball":
            destination = get_output_filename(location_name, base_folder=output_base_folder, run_date=run_date)
            if os.path.exists(destination):
                write_snapshot(destination, [], output_columns, [])
        history_data = load_scheduler_history(scheduler_history_file)
        entry = record_location_run(
            history_data,
            scraper=scraper.__class__.__name__,
            location=location_name,
            run_mode=run_mode,
            runtime_seconds=(datetime.now() - location_started_at).total_seconds(),
            links_collected=len(new_links) + collection_non_future,
            candidates_processed=0,
            rows_written=0,
            stale_skipped=stale_carryover_count,
            validation_failures=0,
            extraction_failures=0,
            non_future_skipped=collection_non_future,
            batches_attempted=0,
            batches_written=0,
            collection_complete=collection_complete,
            collection_error=collection_error,
        )
        scraper._last_location_result = dict(entry["recent_runs"][-1])
        save_scheduler_history(history_data, scheduler_history_file)
        return driver

    # 4) Ensure output Excel file exists
    output_file = get_output_filename(location_name, base_folder=output_base_folder, run_date=run_date)
    created_this_run = not os.path.exists(output_file)
    logger.info("Excel output file for %s will be: %s", location_name, output_file)

    # 5) Process in batches (returns possibly restarted driver)
    recovered_keys = {key for key in discovered_now if events_data["events"][key].get("export_status") == "pending"}
    driver, total_appended, batch_metrics = process_events_in_batches(
        scraper=scraper,
        driver=driver,
        urls_to_process=unique_events_to_process,
        events_data=events_data,
        failed_data=failed_data,
        json_file=json_file,
        failed_events_file=failed_events_file,
        output_file=output_file,
        columns=output_columns,
        selected_location=location_name,
        download_dir=download_dir,
        shutdown_check=shutdown_check,
        firefox_binary=firefox_binary,
        pull_options=options,
    )
    scraper._active_driver = driver
    if recovered_keys and options.mode != "incremental" and not batch_metrics["persistence_failures"] and not batch_metrics["interrupted"]:
        driver, refreshed_rows, refreshed_metrics = process_events_in_batches(
            scraper=scraper, driver=driver,
            urls_to_process=[{"name": key, "url": events_data["events"][key]["url"]} for key in sorted(recovered_keys)],
            events_data=events_data, failed_data=failed_data, json_file=json_file,
            failed_events_file=failed_events_file, output_file=output_file, columns=output_columns,
            selected_location=location_name, download_dir=download_dir, shutdown_check=shutdown_check,
            firefox_binary=firefox_binary, pull_options=options,
        )
        scraper._active_driver = driver
        total_appended += refreshed_rows
        for metric, value in refreshed_metrics.items():
            batch_metrics[metric] = (batch_metrics[metric] or value) if metric == "interrupted" else batch_metrics[metric] + value

    if options.mode == "snapshot" and sport == "baseball" and not batch_metrics["persistence_failures"] and not batch_metrics["interrupted"]:
        snapshot_keys = [key for key in discovered_now
                         if events_data["events"][key].get("event_data")
                         and events_data["events"][key].get("last_attempt_status") == "success"
                         and events_data["events"][key].get("export_status") == "exported"
                         and not events_data["events"][key].get("is_deleted")
                         and options.includes(events_data["events"][key]["event_data"])]
        snapshot_keys.sort()
        try:
            write_snapshot(output_file, [events_data["events"][key]["event_data"] for key in snapshot_keys],
                           output_columns, [events_data["events"][key].get("export_identity", key)
                                            for key in snapshot_keys])
            total_appended = len(snapshot_keys)
        except Exception as exc:
            batch_metrics["persistence_failures"] += 1
            logger.exception("Could not finalize location snapshot: %s", output_file)
            for key in snapshot_keys:
                entry = events_data["events"][key]
                entry.update(processed=False, export_status="pending", export_file=os.path.abspath(output_file))
                add_or_update_failure(failed_data, name=key, url=entry["url"], location=location_name,
                                      reason=f"Snapshot export failed: {exc}")
            save_events(events_data, json_file)
            save_failed_events(failed_data, failed_events_file)

    # Safe cleanup: only delete if we created it AND nothing was appended
    if created_this_run and total_appended == 0 and os.path.exists(output_file) and not batch_metrics["persistence_failures"]:
        try:
            os.remove(output_file)
            logger.info("[Excel] Deleted empty staged sheet (no successful rows): %s", output_file)
        except Exception as e:
            logger.warning("[Excel] Failed to delete empty staged sheet %s: %s", output_file, e)

    history_data = load_scheduler_history(scheduler_history_file)
    entry = record_location_run(
        history_data,
        scraper=scraper.__class__.__name__,
        location=location_name,
        run_mode=run_mode,
        runtime_seconds=(datetime.now() - location_started_at).total_seconds(),
        links_collected=len(new_links) + collection_non_future,
        candidates_processed=len(unique_events_to_process),
        rows_written=total_appended,
        stale_skipped=stale_carryover_count,
        validation_failures=batch_metrics["validation_failures"],
        extraction_failures=batch_metrics["extraction_failures"],
        non_future_skipped=batch_metrics["non_future_skipped"] + collection_non_future,
        batches_attempted=batch_metrics["batches_attempted"],
        batches_written=batch_metrics["batches_written"],
        persistence_failures=batch_metrics["persistence_failures"],
        events_attempted=batch_metrics["events_attempted"],
        interrupted=batch_metrics["interrupted"],
        collection_complete=collection_complete,
        collection_error=collection_error,
        date_scope_skipped=batch_metrics["date_scope_skipped"],
        source_incomplete_skipped=batch_metrics["source_incomplete_skipped"],
        driver_restarts=batch_metrics["driver_restarts"],
    )
    scraper._last_location_result = dict(entry["recent_runs"][-1])
    scraper._active_driver = driver
    save_scheduler_history(history_data, scheduler_history_file)

    log_progress(logger, "Completed location: %s", location_name)
    return driver
