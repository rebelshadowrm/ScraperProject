# common/runner.py

import os
import time
import random
import logging
from typing import List, Dict, Any, Callable, Optional

from selenium.common.exceptions import WebDriverException

from common.validation import validate_event_data
from common.excel_utils import create_workbook_from_template, append_rows_to_template, get_output_filename

from common.json_utils import (
    load_events,
    save_events,
    load_failed_events,
    save_failed_events,
    add_or_update_event,
    record_attempt,
    mark_processed,
    add_or_update_failure,
    mark_failure_resolved,
)

from common.driver_manager import (
    is_driver_connection_error,
    cleanup_download_dir,
    restart_driver,
)

logger = logging.getLogger(__name__)


def normalize_url(url: str) -> str:
    return url.rstrip('/.').lower()


def ensure_template(output_file: str) -> bool:
    """Copy the Excel template to output_file if it doesn't exist yet. Returns True if created."""
    if not os.path.exists(output_file):
        create_workbook_from_template(output_file)
        return True
    return False


def process_events_in_batches(
    *,
    scraper,
    driver,
    urls_to_process: List[Dict[str, str]],
    events_data: Dict[str, Any],
    failed_data: Dict[str, Any],
    json_file: str,
    failed_events_file: str,
    output_file: str,
    columns: List[str],
    selected_location: str,
    download_dir: str,
    shutdown_check: Callable[[], bool],
    batch_size: int = 10,
    delay_between_batches: int = 30,
    firefox_binary: Optional[str] = None,
):
    """
    Process event detail pages in batches; restart driver once if disconnected.
    Returns (driver, total_appended).
    """
    data_batch: List[Dict[str, Any]] = []
    total_urls = len(urls_to_process)
    total_appended = 0

    for i in range(0, total_urls, batch_size):
        if shutdown_check():
            logger.info("Shutdown flag detected. Exiting batch processing.")
            break

        batch = urls_to_process[i:i + batch_size]
        logger.info(
            "Processing batch %d/%d.",
            (i // batch_size) + 1,
            (total_urls + batch_size - 1) // batch_size
        )

        for event_dict in batch:
            if shutdown_check():
                logger.info("Shutdown flag detected during URL processing. Exiting.")
                break

            # NOTE: in your current flow, "name" is effectively the event key (often normalized URL)
            key = event_dict["name"]
            url = event_dict["url"]

            attempts = 0
            while attempts < 2:
                attempts += 1
                try:
                    event_data = scraper.extract_event_details(url, driver=driver)
                    if not event_data:
                        msg = "Extraction failed"
                        logger.warning("%s for URL: %s.", msg, url)

                        record_attempt(events_data, key=key, status="failed", error=msg)
                        save_events(events_data, json_file)

                        add_or_update_failure(
                            failed_data,
                            name=key,
                            url=url,
                            location=selected_location,
                            reason=msg
                        )
                        save_failed_events(failed_data, failed_events_file)
                        break

                    ok, reason = validate_event_data(event_data)
                    if not ok:
                        msg = f"Validation failed: {reason}"
                        logger.warning("%s — URL: %s", msg, url)

                        record_attempt(events_data, key=key, status="failed", error=msg)
                        save_events(events_data, json_file)

                        add_or_update_failure(
                            failed_data,
                            name=key,
                            url=url,
                            location=selected_location,
                            reason=msg
                        )
                        save_failed_events(failed_data, failed_events_file)
                        break

                    # success
                    data_batch.append(event_data)

                    record_attempt(events_data, key=key, status="success", error="")
                    mark_processed(events_data, key=key)
                    save_events(events_data, json_file)

                    # Mark failure resolved (optional) + remove from active failed list
                    mark_failure_resolved(failed_data, name=key, url=url)
                    failed_data["failed_events"] = [
                        fev for fev in failed_data["failed_events"]
                        if not (fev.get("name") == key and fev.get("url") == url)
                    ]
                    save_failed_events(failed_data, failed_events_file)

                    break

                except WebDriverException as e:
                    # record attempt failure (even if we retry, it's still useful telemetry)
                    record_attempt(events_data, key=key, status="failed", error=str(e))
                    save_events(events_data, json_file)

                    if is_driver_connection_error(e) and attempts < 2:
                        logger.warning("WebDriver disconnected. Restarting and retrying URL: %s", url)
                        driver = restart_driver(
                            driver,
                            firefox_binary=firefox_binary,
                            download_dir=download_dir,
                            headless=True,
                        )
                        continue

                    logger.warning("WebDriver error at %s: %s", url, e)
                    add_or_update_failure(
                        failed_data,
                        name=key,
                        url=url,
                        location=selected_location,
                        reason=str(e)
                    )
                    save_failed_events(failed_data, failed_events_file)
                    break

                except Exception as e:
                    record_attempt(events_data, key=key, status="failed", error=str(e))
                    save_events(events_data, json_file)

                    if is_driver_connection_error(e) and attempts < 2:
                        logger.warning("Driver connection issue detected. Restarting and retrying URL: %s", url)
                        driver = restart_driver(
                            driver,
                            firefox_binary=firefox_binary,
                            download_dir=download_dir,
                            headless=True,
                        )
                        continue

                    logger.error("Error processing %s: %s", url, e)
                    add_or_update_failure(
                        failed_data,
                        name=key,
                        url=url,
                        location=selected_location,
                        reason=str(e)
                    )
                    save_failed_events(failed_data, failed_events_file)
                    break

        # Write this batch to Excel
        if data_batch:
            try:
                append_rows_to_template(
                    output_file=output_file,
                    rows=data_batch,
                    column_order=columns,
                )
                total_appended += len(data_batch)
                logger.info("Saved batch %d to Excel (template append).", (i // batch_size) + 1)
                data_batch.clear()
            except Exception as e:
                logger.error("Error saving batch to Excel: %s", e)

        cleanup_download_dir(download_dir)

        if i + batch_size < total_urls and not shutdown_check():
            base_delay = getattr(scraper, "BATCH_DELAY_SECONDS", delay_between_batches)
            if base_delay and base_delay > 0:
                wait_time = base_delay + random.uniform(0, 5)
                logger.info("Batch %d completed. Waiting %.2fs before next batch.", (i // batch_size) + 1, wait_time)
                time.sleep(wait_time)
            else:
                logger.info("Batch %d completed. No wait (API/fast scraper).", (i // batch_size) + 1)


    return driver, total_appended


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
):
    """
    Process a single location:
      1) collect links
      2) add/update events in JSON (with timestamps/provenance)
      3) filter unprocessed for this location
      4) ensure output template exists
      5) batch scrape + append to Excel
    """
    logger.info("Processing location: %s (%s)", location_name, base_url)

    # 1) Collect event links (restart-on-disconnect once)
    try:
        new_links = scraper.collect_event_links(driver, base_url=base_url)
    except WebDriverException as e:
        if is_driver_connection_error(e):
            logger.warning("WebDriver disconnected during link collection. Restarting and retrying once.")
            driver = restart_driver(
                driver,
                firefox_binary=firefox_binary,
                download_dir=download_dir,
                headless=True,
            )
            new_links = scraper.collect_event_links(driver, base_url=base_url)
        else:
            raise

    logger.info("Collected %d event links from %s for %s.", len(new_links), base_url, location_name)

    # 2) Add/update events with richer schema
    scraper_name = scraper.__class__.__name__
    for link in new_links:
        norm_key = normalize_url(link)

        add_or_update_event(
            events_data,
            key=norm_key,
            url=link,
            location=location_name,
            scraper=scraper_name,
            discovered_from=base_url,
        )

    save_events(events_data, json_file)

    # 3) Filter unprocessed events for this location
    unprocessed_events = [
        {"name": key, "url": details["url"]}
        for key, details in events_data["events"].items()
        if not details.get("processed", False)
        and details.get("location") == location_name
        and not details.get("is_deleted", False)
    ]

    # Deduplicate by normalized URL
    seen = set()
    unique_events_to_process = []
    for ev in unprocessed_events:
        norm = normalize_url(ev["url"])
        if norm not in seen:
            unique_events_to_process.append(ev)
            seen.add(norm)

    logger.info("Total unique events to process for %s: %d", location_name, len(unique_events_to_process))

    if len(unique_events_to_process) == 0:
        logger.info("No unprocessed events for %s. Skipping Excel creation.", location_name)
        return driver

    # 4) Ensure output Excel file exists
    output_file = get_output_filename(location_name)
    created_this_run = ensure_template(output_file)
    logger.info("Excel output file for %s will be: %s", location_name, output_file)

    # 5) Process in batches (returns possibly restarted driver)
    driver, total_appended = process_events_in_batches(
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
    )

    # Safe cleanup: only delete if we created it AND nothing was appended
    if created_this_run and total_appended == 0:
        try:
            os.remove(output_file)
            logger.info("[Excel] Deleted empty staged sheet (no successful rows): %s", output_file)
        except Exception as e:
            logger.warning("[Excel] Failed to delete empty staged sheet %s: %s", output_file, e)

    logger.info("Batch processing complete for %s.", location_name)
    return driver
