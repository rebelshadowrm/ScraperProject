# main.py

import os
import sys
import signal
import logging
import time
import json
import random
import re
from datetime import datetime
from typing import List, Dict
from urllib.parse import urlparse, parse_qs

# ======================== COMMON UTILS ========================
from common.logging_utils import setup_logger, flush_logs
from common.validation import validate_event_data
from common.excel_utils import (
    get_output_filename,
    consolidate_org_files,
    create_workbook_from_template,
    append_rows_to_template  
)
from common.json_utils import (
    load_events,
    save_events,
    load_failed_events,
    save_failed_events
)
from common.config import OUTPUT_COLUMNS, TEMPLATE_SHEET

def ensure_template(output_file: str):
    """Copy the Excel template to output_file if it doesn't exist yet."""
    import os
    if not os.path.exists(output_file):
        create_workbook_from_template(output_file)


# ======================== DRIVER SETUP ========================
from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import WebDriverException
from webdriver_manager.firefox import GeckoDriverManager


# -------------------- GLOBALS & CONSTANTS --------------------
shutdown_flag = False
logger = setup_logger(log_file="scraping.log", log_level=logging.INFO)

JSON_FILE = "events.json"
FAILED_EVENTS_FILE = "failed_events.json"
LOCATIONS_FILE = "locations.json"

# Where Firefox will put any forced downloads (e.g., HTML served as an attachment).
# Keeps your real Downloads folder clean.
DOWNLOAD_DIR = os.path.abspath(os.path.join(os.getcwd(), "_selenium_downloads"))


# -------------------- SIGNAL HANDLER -------------------------
def signal_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received. Preparing to exit gracefully...")
    shutdown_flag = True
    flush_logs()

# Register the signal handler for graceful shutdown
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# -------------------- DRIVER HEALTH / RESTART --------------------
_DRIVER_DEAD_MARKERS = (
    "Tried to run command without establishing a connection",
    "invalid session id",
    "session not created",
    "disconnected",
    "connection refused",
    "Max retries exceeded with url: /session",
    "read timed out",
    "cannot connect to the service",
)

def is_driver_connection_error(exc: Exception) -> bool:
    msg = str(exc) or ""
    return any(m.lower() in msg.lower() for m in _DRIVER_DEAD_MARKERS)

def safe_quit_driver(driver):
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass

def cleanup_download_dir(download_dir: str, max_delete: int = 500):
    """Remove stray downloaded HTML files so they don't accumulate."""
    try:
        if not download_dir or not os.path.isdir(download_dir):
            return
        deleted = 0
        for fn in os.listdir(download_dir):
            if deleted >= max_delete:
                break
            lower = fn.lower()
            if lower.endswith(".htm") or lower.endswith(".html"):
                try:
                    os.remove(os.path.join(download_dir, fn))
                    deleted += 1
                except Exception:
                    pass
        if deleted:
            logger.info(f"Cleaned up {deleted} downloaded .htm/.html files in {download_dir}")
    except Exception:
        # Never let cleanup fail the run
        pass



# -------------------- DRIVER INIT ----------------------------
def initialize_driver(firefox_binary: str = "C:/Program Files/Mozilla Firefox/firefox.exe"):
    """
    Set up a headless Firefox driver with webdriver-manager.

    Also forces any "attachment" downloads (Perfect Game occasionally serves HTML as an attachment)
    into DOWNLOAD_DIR so you don't end up with random .htm files in your real Downloads folder.
    """
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    options = Options()
    options.binary_location = firefox_binary
    options.headless = True
    options.accept_insecure_certs = True

    # Keep downloads contained (headless can't handle "Open/Save" prompts well).
    options.set_preference("browser.download.folderList", 2)  # custom location
    options.set_preference("browser.download.dir", DOWNLOAD_DIR)
    options.set_preference("browser.download.useDownloadDir", True)
    options.set_preference("browser.download.manager.showWhenStarting", False)
    options.set_preference("browser.download.alwaysOpenPanel", False)
    options.set_preference("browser.download.manager.focusWhenStarting", False)
    options.set_preference("browser.download.manager.alertOnEXEOpen", False)
    options.set_preference("browser.helperApps.alwaysAsk.force", False)
    # If the server forces an "attachment", Firefox will download it; this prevents prompts/hangs.
    options.set_preference(
        "browser.helperApps.neverAsk.saveToDisk",
        ",".join([
            "application/octet-stream",
            "text/html",
            "application/xhtml+xml",
            "text/plain",
        ])
    )

    service = Service(GeckoDriverManager().install())
    driver = webdriver.Firefox(service=service, options=options)
    logger.info(f"Selenium WebDriver (Firefox) initialized. Download dir: {DOWNLOAD_DIR}")
    return driver


# -------------------- LOAD LOCATIONS -------------------------
def load_locations(config_file=LOCATIONS_FILE) -> Dict[str, str]:
    if os.path.exists(config_file):
        with open(config_file, "r") as file:
            data = json.load(file)
            if "locations" in data:
                return data["locations"]
            else:
                logger.error("Invalid JSON format: Missing 'locations' key.")
                sys.exit(1)
    else:
        logger.error(f"Configuration file {config_file} not found.")
        sys.exit(1)


# -------------------- IMPORT PROMPT FUNCTIONS -----------------
from common.prompt_utils import (
    prompt_main_action,
    prompt_for_scraper,
    prompt_for_location,
    prompt_for_sheets_subfolder
)

# ----------------- NORMALIZATION -----------------------
def normalize_url(url: str) -> str:
    return url.rstrip('/.').lower()


# ----------------- FAILED EVENTS FILTER -----------------------
def filter_failed_events_by_domain(failed_data, base_url, selected_location):
    """
    Filter failed events by domain. For failed events that lack a location,
    only the base domain is checked. For those with a location, the location must match.
    """
    from urllib.parse import urlparse
    base_domain = urlparse(base_url).netloc

    filtered_events = [
        ev for ev in failed_data["failed_events"]
        if urlparse(ev["url"]).netloc == base_domain and
           (ev.get("location") is None or ev.get("location") == selected_location)
    ]
    return filtered_events


def reset_processed_flags_for_failed_events(filtered_failed, events_data):
    """
    For each failed event in 'filtered_failed', reset the 'processed' 
    flag in events.json so that main can re-process them.
    """
    failed_urls = {normalize_url(ev["url"]) for ev in filtered_failed}
    for name, details in events_data["events"].items():
        if normalize_url(details["url"]) in failed_urls:
            details["processed"] = False
            logger.info(f"Reset processed flag for event: {name}")


# ----------------- BATCH PROCESSING --------------------------
def process_events_in_batches(scraper, driver, urls_to_process, events_data, failed_data, output_file, columns, selected_location):
    """
    Batches event processing (scraping details) to control pace and reduce load.
    Robustness upgrades:
      - If the Firefox WebDriver session dies mid-run ("Tried to run command without establishing a connection"),
        we restart the driver and retry the current URL once.
      - We also periodically clean up any downloaded .htm/.html attachments into a safe folder.
    Returns the (possibly restarted) driver.
    """
    batch_size = 10
    delay_between_batches = 30  # seconds
    data_batch = []
    total_urls = len(urls_to_process)

    for i in range(0, total_urls, batch_size):
        if shutdown_flag:
            logger.info("Shutdown flag detected. Exiting batch processing.")
            break

        batch = urls_to_process[i:i + batch_size]
        logger.info(
            f"Processing batch {i // batch_size + 1}/"
            f"{(total_urls + batch_size - 1) // batch_size}."
        )

        for event_dict in batch:
            if shutdown_flag:
                logger.info("Shutdown flag detected during URL processing. Exiting.")
                break

            name = event_dict["name"]
            url = event_dict["url"]

            attempts = 0
            while attempts < 2:
                attempts += 1
                try:
                    event_data = scraper.extract_event_details(url, driver=driver)
                    if not event_data:
                        logger.warning(f"Extraction failed for URL: {url}.")
                        _append_or_update_failure(
                            failed_data, FAILED_EVENTS_FILE, name, url, selected_location, "Extraction failed"
                        )
                        break

                    # Validate the extracted data (pure validator: returns (ok, reason))
                    ok, reason = validate_event_data(event_data)
                    if not ok:
                        logger.warning(f"Validation failed: {reason} — URL: {url}")
                        _append_or_update_failure(
                            failed_data,
                            FAILED_EVENTS_FILE,
                            name,
                            url,
                            selected_location,
                            f"Validation failed: {reason}"
                        )
                        break

                    # Data is good; add to batch
                    data_batch.append(event_data)

                    # Mark event as processed
                    events_data["events"][name]["processed"] = True
                    save_events(events_data, JSON_FILE)

                    # Remove from failed events if it was previously there
                    failed_data["failed_events"] = [
                        fev for fev in failed_data["failed_events"] if fev["name"] != name
                    ]
                    save_failed_events(failed_data, FAILED_EVENTS_FILE)
                    break

                except WebDriverException as e:
                    # If the driver connection died, restart once and retry this URL.
                    if is_driver_connection_error(e) and attempts < 2:
                        logger.warning(f"WebDriver appears disconnected. Restarting driver and retrying URL: {url}")
                        safe_quit_driver(driver)
                        driver = initialize_driver()
                        time.sleep(2)
                        continue

                    logger.warning(f"WebDriver error at {url}: {e}")
                    _append_or_update_failure(failed_data, FAILED_EVENTS_FILE, name, url, selected_location, str(e))
                    break

                except Exception as e:
                    # Same idea for non-WebDriver exception that still indicates a dead session.
                    if is_driver_connection_error(e) and attempts < 2:
                        logger.warning(f"Driver connection issue detected. Restarting driver and retrying URL: {url}")
                        safe_quit_driver(driver)
                        driver = initialize_driver()
                        time.sleep(2)
                        continue

                    logger.error(f"Error processing {url}: {e}")
                    _append_or_update_failure(failed_data, FAILED_EVENTS_FILE, name, url, selected_location, str(e))
                    break

        # Write this batch to Excel (append into the template)
        if data_batch:
            try:
                append_rows_to_template(
                    output_file=output_file,
                    rows=data_batch,                   # list[dict], already in your format
                    column_order=columns               # OUTPUT_COLUMNS
                )
                logger.info(f"Saved batch {(i // batch_size) + 1} to Excel (template append).")
                data_batch.clear()
            except Exception as e:
                logger.error(f"Error saving batch to Excel: {e}")

        # Clean up any HTML attachment downloads PerfectGame might have triggered
        cleanup_download_dir(DOWNLOAD_DIR)

        # Sleep between batches
        if i + batch_size < total_urls:
            wait_time = delay_between_batches + random.uniform(0, 5)
            logger.info(f"Batch {i // batch_size + 1} completed. Waiting {wait_time:.2f}s before next batch.")
            time.sleep(wait_time)

    return driver


def _append_or_update_failure(failed_data, failed_file, name, url, location, reason):
    """
    Helper method to add or update an entry in failed_data['failed_events'].
    Comparison is done based on normalized URL rather than name.
    """
    norm_url = normalize_url(url)
    existing = next((e for e in failed_data["failed_events"] if normalize_url(e["url"]) == norm_url), None)
    if existing:
        existing["reason"] = reason
        # Optionally update location if not set:
        if "location" not in existing or not existing["location"]:
            existing["location"] = location
    else:
        failed_data["failed_events"].append({
            "name": name,
            "url": url,
            "location": location,  # Save location along with other data.
            "reason": reason
        })
    save_failed_events(failed_data, failed_file)


def process_location(location_name: str, base_url: str, scraper, driver, events_data, failed_data, output_columns):
    """
    Process a single location: collect links, update events_data,
    create the output Excel file, and process events in batches.

    Returns the (possibly restarted) driver.
    """
    logger.info(f"Processing location: {location_name} ({base_url})")

    # 1. Collect new event links for this location (with restart-on-disconnect).
    try:
        new_links = scraper.collect_event_links(driver, base_url=base_url)
    except WebDriverException as e:
        if is_driver_connection_error(e):
            logger.warning("WebDriver appears disconnected during link collection. Restarting driver and retrying once.")
            safe_quit_driver(driver)
            driver = initialize_driver()
            new_links = scraper.collect_event_links(driver, base_url=base_url)
        else:
            raise

    logger.info(f"Collected {len(new_links)} event links from {base_url} for {location_name}.")

    # 2. Add new links to events_data if not present.
    for link in new_links:
        normalized_key = normalize_url(link)
        display_name = scraper.get_event_key(link)
        if not any(normalize_url(details["url"]) == normalized_key for details in events_data["events"].values()):
            events_data["events"][normalized_key] = {
                "url": normalized_key,
                "processed": False,
                "location": location_name
            }
            logger.info(f"Added new event for {location_name} to JSON: {display_name} - {link}")
    save_events(events_data, JSON_FILE)

    # 3. Filter out the unprocessed events for this location.
    unprocessed_events = [
        {"name": name, "url": details["url"]}
        for name, details in events_data["events"].items()
        if not details.get("processed", False) and details.get("location") == location_name
    ]
    seen = set()
    unique_events_to_process = []
    for ev in unprocessed_events:
        norm = normalize_url(ev["url"])
        if norm not in seen:
            unique_events_to_process.append(ev)
            seen.add(norm)
    logger.info(f"Total unique events to process for {location_name}: {len(unique_events_to_process)}")

    # 4. Create the Excel output file for this location.
    output_file = get_output_filename(location_name)
    ensure_template(output_file)
    logger.info(f"Excel output file for {location_name} will be: {output_file}")

    # 5. Process events in batches (returns possibly restarted driver).
    driver = process_events_in_batches(
        scraper=scraper,
        driver=driver,
        urls_to_process=unique_events_to_process,
        events_data=events_data,
        failed_data=failed_data,
        output_file=output_file,
        columns=output_columns,
        selected_location=location_name
    )
    logger.info(f"Batch processing complete for {location_name}.")
    return driver



# ----------------- MAIN SCRIPT -------------------------------
def main():
    global shutdown_flag

    # Ask: Scrape or Consolidate?
    action = prompt_main_action()
    if action == "2":
        # User wants to consolidate Excel files
        # Prompt for the date folder (e.g., "2025-03-06")
        subfolder = prompt_for_sheets_subfolder("Sheets")
        date_folder = os.path.join("Sheets", subfolder)
        
        logger.info(f"Consolidating Excel files in date folder: {date_folder}")
        
        # List organization subfolders (each should be like "NCS", "USSSA", etc.)
        org_subfolders = [
            d for d in os.listdir(date_folder)
            if os.path.isdir(os.path.join(date_folder, d))
        ]
        
        if not org_subfolders:
            logger.info(f"No organization subfolders found in {date_folder}. Nothing to consolidate.")
            sys.exit(0)
        
        # For each organization subfolder, consolidate its Excel files
        for org in org_subfolders:
            org_folder = os.path.join(date_folder, org)
            logger.info(f"Consolidating Excel files in organization folder: {org_folder}")
            result = consolidate_org_files(org_folder, org, subfolder)
            if result:
                logger.info(f"Consolidation for {org} complete. Output: {result}")
            else:
                logger.info(f"No files consolidated for {org} or result was empty.")
        
        # Exit after consolidation
        sys.exit(0)


    # Use the dynamic prompt to select the scraper.
    scraper = prompt_for_scraper()  # This now displays all discovered scrapers.
    logger.info(f"Selected: {scraper.__class__.__name__}")


    # Prompt for location using the scraper instance.
    locations = load_locations(LOCATIONS_FILE)
    selected_location, loc_info = prompt_for_location(locations, scraper)
    logger.info(f"User selected location: {selected_location}")

    # Initialize driver
    driver = initialize_driver()

    # Load or create data JSON
    events_data = load_events(JSON_FILE)
    failed_data = load_failed_events(FAILED_EVENTS_FILE)

    try:
        if selected_location == "ALL":
            # Mass pull: Process each location individually.
            for loc_name, loc_url in loc_info.items():
                driver = process_location(loc_name, loc_url, scraper, driver, events_data, failed_data, OUTPUT_COLUMNS)
            sys.exit(0)
        else:
            # Single location processing.
            base_url = loc_info
            driver = process_location(selected_location, base_url, scraper, driver, events_data, failed_data, OUTPUT_COLUMNS)

    except Exception as e:
        logger.error(f"An error occurred in main(): {e}")
    finally:
        # Gracefully close the driver
        try:
            driver.quit()
            logger.info("WebDriver quit successfully.")
        except Exception as e:
            logger.error(f"Error quitting WebDriver: {e}")
        flush_logs()
        logging.shutdown()


# Standard Python entry point
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received. Exiting gracefully...")
        flush_logs()
        logging.shutdown()
        sys.exit(0)
