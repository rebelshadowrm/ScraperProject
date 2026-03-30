# ui/cli.py

import os
import sys
import json
import signal
import logging

from common.logging_utils import setup_logger, flush_logs
from common.json_utils import load_events, load_failed_events
from common.config import OUTPUT_COLUMNS

from common.prompt_utils import (
    prompt_for_scraper,
    prompt_for_location,
)

from common.driver_manager import (
    initialize_driver,
    safe_quit_driver,
    DEFAULT_DOWNLOAD_DIR,
)

from common.runner import process_location


# -------------------- GLOBALS & CONSTANTS --------------------
shutdown_flag = False

logger = setup_logger(log_file="scraping.log", log_level=logging.INFO)

JSON_FILE = "events.json"
FAILED_EVENTS_FILE = "failed_events.json"
LOCATIONS_FILE = "locations.json"


# -------------------- SIGNAL HANDLER -------------------------
def _signal_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received. Preparing to exit gracefully...")
    shutdown_flag = True
    flush_logs()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _shutdown_check() -> bool:
    return shutdown_flag


# -------------------- LOAD LOCATIONS -------------------------
def load_locations(config_file: str = LOCATIONS_FILE):
    """
    Kept here for now (per your request to delay splitting locations/failure_tracker),
    so CLI remains self-contained.
    """
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as file:
            data = json.load(file)
            if "locations" in data:
                return data["locations"]
            logger.error("Invalid JSON format: Missing 'locations' key.")
            sys.exit(1)

    logger.error("Configuration file %s not found.", config_file)
    sys.exit(1)


# -------------------- CLI ENTRYPOINT -------------------------
def run_cli():
    """
    Runs the CLI workflow:
      1) choose scraper
      2) choose location (or ALL)
      3) run scraping pipeline via common.runner
    """
    # Use the dynamic prompt to select the scraper.
    scraper = prompt_for_scraper()
    logger.info("Selected scraper: %s", scraper.__class__.__name__)

    # Prompt for location using the scraper instance.
    locations = load_locations(LOCATIONS_FILE)
    selected_location, loc_info = prompt_for_location(locations, scraper)
    logger.info("User selected location: %s", selected_location)

    # Load persistent state
    events_data = load_events(JSON_FILE)
    failed_data = load_failed_events(FAILED_EVENTS_FILE)

    # Initialize driver once (runner will restart if it disconnects mid-run).
    driver = None
    firefox_path = "C:/Program Files/Mozilla Firefox/firefox.exe"

    try:
        driver = initialize_driver(
            firefox_binary=firefox_path,
            download_dir=DEFAULT_DOWNLOAD_DIR,
            headless=True,
        )

        if selected_location == "ALL":
            # loc_info is expected to be dict: location_name -> base_url
            for loc_name, loc_url in loc_info.items():
                if _shutdown_check():
                    logger.info("Shutdown detected. Stopping before next location.")
                    break

                driver = process_location(
                    location_name=loc_name,
                    base_url=loc_url,
                    scraper=scraper,
                    driver=driver,
                    events_data=events_data,
                    failed_data=failed_data,
                    output_columns=OUTPUT_COLUMNS,
                    json_file=JSON_FILE,
                    failed_events_file=FAILED_EVENTS_FILE,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    shutdown_check=_shutdown_check,
                    firefox_binary=firefox_path,
                )
        else:
            # loc_info is expected to be a single base_url string
            driver = process_location(
                location_name=selected_location,
                base_url=loc_info,
                scraper=scraper,
                driver=driver,
                events_data=events_data,
                failed_data=failed_data,
                output_columns=OUTPUT_COLUMNS,
                json_file=JSON_FILE,
                failed_events_file=FAILED_EVENTS_FILE,
                download_dir=DEFAULT_DOWNLOAD_DIR,
                shutdown_check=_shutdown_check,
                firefox_binary=firefox_path,
            )

    except Exception as e:
        logger.error("An error occurred in CLI run: %s", e)

    finally:
        safe_quit_driver(driver)
        flush_logs()
        logging.shutdown()
