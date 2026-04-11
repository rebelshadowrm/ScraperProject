# ui/cli.py

import os
import sys
import json
import signal
import logging
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.logging_utils import (
    setup_logger,
    flush_logs,
    log_progress,
    build_run_log_path,
    cleanup_old_logs,
)
from common.json_utils import load_events, load_failed_events
from common.config import OUTPUT_COLUMNS
from common.data_control import rollback_events
from common.scheduler_history import load_scheduler_history, save_scheduler_history

from common.prompt_utils import (
    ALL_SCRAPERS_SENTINEL,
    VALIDATION_SCRAPERS_SENTINEL,
    get_scraper_classes,
    prompt_main_action,
    prompt_for_scraper,
    prompt_for_location,
    _load_scraper_from_reference,
)

from common.driver_manager import (
    initialize_driver,
    safe_quit_driver,
    DEFAULT_DOWNLOAD_DIR,
)

from common.runner import process_location
from scrapers.play365_base import Play365ManualFallbackRequired


# -------------------- GLOBALS & CONSTANTS --------------------
shutdown_flag = False

cleanup_old_logs(logs_root="Logs", keep_runs=50, keep_days=60)
logger = setup_logger(log_file=build_run_log_path("startup", logs_root="Logs"), log_level=logging.INFO)

JSON_FILE = "events.json"
FAILED_EVENTS_FILE = "failed_events.json"
SCHEDULER_HISTORY_FILE = "scheduler_history.json"
LOCATIONS_FILE = "locations.json"


# -------------------- SIGNAL HANDLER -------------------------
def _signal_handler(sig, frame):
    global shutdown_flag
    logger.info("Shutdown signal received. Preparing to exit gracefully...")
    shutdown_flag = True
    flush_logs()
    raise KeyboardInterrupt


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def _shutdown_check() -> bool:
    return shutdown_flag


# -------------------- LOAD LOCATIONS -------------------------
def load_locations(config_file: str = LOCATIONS_FILE):
    if os.path.exists(config_file):
        with open(config_file, "r", encoding="utf-8") as file:
            data = json.load(file)
            if "locations" in data:
                return data["locations"]
            logger.error("Invalid JSON format: Missing 'locations' key.")
            sys.exit(1)

    logger.error("Configuration file %s not found.", config_file)
    sys.exit(1)


def _build_test_run_paths(scraper_name: str) -> tuple[str, str, str]:
    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M%S")
    scraper_folder = scraper_name or "UnknownScraper"

    base_dir = os.path.join("Tests", date_str, f"{time_str}_{scraper_folder}")
    sheets_dir = os.path.join(base_dir, "Sheets")

    os.makedirs(sheets_dir, exist_ok=True)

    return (
        os.path.join(base_dir, "events.json"),
        os.path.join(base_dir, "failed_events.json"),
        sheets_dir,
    )


def _downloads_manual_play365_dir() -> str:
    return str(Path.home() / "Downloads" / "ScraperProject" / "ManualPlay365")


def _cleanup_manual_play365_workspace(workspace_root: str, scraper=None):
    root = Path(workspace_root)
    if not root.exists():
        return

    targets = []
    if scraper is not None:
        targets.append(root / scraper.SCRIPT_FILENAME)
        targets.append(root / scraper.DUMP_FILENAME)
    else:
        targets.extend(root.glob("export_*.js"))
        targets.extend(root.glob("*_dump.json"))

    targets.append(root / "README.txt")

    for path in targets:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except Exception:
            pass

    try:
        if root.exists() and not any(root.iterdir()):
            root.rmdir()
    except Exception:
        pass

    for parent in (root.parent, root.parent.parent):
        try:
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
        except Exception:
            pass


def _run_play365_batch(
    *,
    play365_targets: dict[str, str],
    events_data,
    failed_data,
    json_file: str,
    failed_events_file: str,
    output_base_folder: str,
    scheduler_history_file: str,
    run_mode: str,
):
    firefox_path = "C:/Program Files/Mozilla Firefox/firefox.exe"
    manual_failures: list[tuple[str, str]] = []

    for display_name, scraper_ref in play365_targets.items():
        if _shutdown_check():
            log_progress(logger, "Shutdown detected. Stopping Play365 automatic sweep.")
            break

        scraper = _load_scraper_from_reference(scraper_ref)
        log_progress(logger, "[Play365] Automatic phase: %s", scraper.SCRAPER_NAME)
        locations = scraper.get_dynamic_locations()

        try:
            driver = None
            if getattr(scraper, "REQUIRES_DRIVER", True):
                driver = initialize_driver(
                    firefox_binary=firefox_path,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    headless=True,
                )

            for loc_name, loc_url in locations.items():
                if _shutdown_check():
                    break
                driver = process_location(
                    location_name=loc_name,
                    base_url=loc_url,
                    scraper=scraper,
                    driver=driver,
                    events_data=events_data,
                    failed_data=failed_data,
                    output_columns=OUTPUT_COLUMNS,
                    json_file=json_file,
                    failed_events_file=failed_events_file,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    shutdown_check=_shutdown_check,
                    firefox_binary=firefox_path,
                    output_base_folder=output_base_folder,
                    collect_kwargs={"allow_manual_fallback": False},
                    scheduler_history_file=scheduler_history_file,
                    run_mode=run_mode,
                )
            safe_quit_driver(driver)
        except Play365ManualFallbackRequired:
            safe_quit_driver(driver)
            logger.warning("[Play365] Automatic phase failed for %s. Queued for manual fallback.", scraper.SCRAPER_NAME)
            manual_failures.append((display_name, scraper_ref))
        except Exception:
            safe_quit_driver(driver)
            raise

    if not manual_failures or _shutdown_check():
        return

    failed_names = ", ".join(_load_scraper_from_reference(ref).SCRAPER_NAME for _, ref in manual_failures)
    print(
        "\nAutomatic Play365 pulling finished.\n"
        f"Manual fallback is available for: {failed_names}\n"
        "Generate manual extraction scripts and process them now?\n"
        "1) Yes\n"
        "2) No\n"
    )

    while True:
        choice = input("Enter choice [1-2]: ").strip()
        if choice in {"1", "2"}:
            break
        print("Invalid choice. Please enter 1 or 2.")

    if choice == "2":
        return

    workspace_root = _downloads_manual_play365_dir()
    for _, scraper_ref in manual_failures:
        if _shutdown_check():
            break

        scraper = _load_scraper_from_reference(scraper_ref)
        log_progress(logger, "[Play365] Manual fallback phase: %s", scraper.SCRAPER_NAME)
        locations = scraper.get_dynamic_locations()
        driver = None
        try:
            if getattr(scraper, "REQUIRES_DRIVER", True):
                driver = initialize_driver(
                    firefox_binary=firefox_path,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    headless=True,
                )

            for loc_name, loc_url in locations.items():
                if _shutdown_check():
                    break
                driver = process_location(
                    location_name=loc_name,
                    base_url=loc_url,
                    scraper=scraper,
                    driver=driver,
                    events_data=events_data,
                    failed_data=failed_data,
                    output_columns=OUTPUT_COLUMNS,
                    json_file=json_file,
                    failed_events_file=failed_events_file,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    shutdown_check=_shutdown_check,
                    firefox_binary=firefox_path,
                    output_base_folder=output_base_folder,
                    collect_kwargs={
                        "allow_manual_fallback": True,
                        "manual_workspace_root": workspace_root,
                        "skip_direct_api": True,
                        "prompt_for_manual_fallback": False,
                    },
                    scheduler_history_file=scheduler_history_file,
                    run_mode=run_mode,
                )
        finally:
            safe_quit_driver(driver)
            _cleanup_manual_play365_workspace(workspace_root, scraper=scraper)

    _cleanup_manual_play365_workspace(workspace_root)


def _resolve_all_locations_for_scraper(all_locations, scraper):
    scraper_name = getattr(scraper, "SCRAPER_NAME", scraper.__class__.__name__)

    if scraper_name == "Play365Base":
        play365_targets = {
            name: url for name, url in all_locations.items()
            if "(Play365Base)" in name
        }
        if not play365_targets:
            return "None Found", "", scraper
        return "ALL_PLAY365_SCRAPERS", play365_targets, scraper

    if getattr(scraper, "uses_dynamic_locations", None) and scraper.uses_dynamic_locations() and hasattr(scraper, "get_dynamic_locations"):
        filtered_locations = scraper.get_dynamic_locations()
    else:
        filtered_locations = {
            name: url for name, url in all_locations.items()
            if f"({scraper_name})" in name
        }

    if not filtered_locations:
        return "None Found", "", scraper

    return "ALL", filtered_locations, scraper


def _run_selected_scraper(*, scraper, selected_location, loc_info, test_mode: bool = False):
    global logger

    json_file = JSON_FILE
    failed_events_file = FAILED_EVENTS_FILE
    scheduler_history_file = SCHEDULER_HISTORY_FILE
    output_base_folder = "Sheets"
    run_label = scraper.__class__.__name__

    if test_mode:
        batch_name = "Play365Batch" if selected_location == "ALL_PLAY365_SCRAPERS" else scraper.__class__.__name__
        json_file, failed_events_file, output_base_folder = _build_test_run_paths(batch_name)
        scheduler_history_file = os.path.join(os.path.dirname(json_file), "scheduler_history.json")
        run_label = batch_name
        log_progress(logger, "Test mode enabled. Writing outputs under: %s", os.path.dirname(json_file))
    elif selected_location == "ALL_PLAY365_SCRAPERS":
        run_label = "Play365Batch"

    logger = setup_logger(log_file=build_run_log_path(run_label, logs_root="Logs"), log_level=logging.INFO)
    log_progress(logger, "Run log created for %s.", run_label)

    events_data = load_events(json_file)
    failed_data = load_failed_events(failed_events_file)
    if not os.path.exists(scheduler_history_file):
        save_scheduler_history(load_scheduler_history(scheduler_history_file), scheduler_history_file)

    driver = None
    firefox_path = "C:/Program Files/Mozilla Firefox/firefox.exe"
    manual_workspace_root = _downloads_manual_play365_dir() if hasattr(scraper, "_manual_dump_path") else None

    try:
        if selected_location == "ALL_PLAY365_SCRAPERS":
            _run_play365_batch(
                play365_targets=loc_info,
                events_data=events_data,
                failed_data=failed_data,
                json_file=json_file,
                failed_events_file=failed_events_file,
                output_base_folder=output_base_folder,
                scheduler_history_file=scheduler_history_file,
                run_mode="test" if test_mode else "live",
            )
            return

        if getattr(scraper, "REQUIRES_DRIVER", True):
            driver = initialize_driver(
                firefox_binary=firefox_path,
                download_dir=DEFAULT_DOWNLOAD_DIR,
                headless=True,
            )
        else:
            log_progress(logger, "Skipping Selenium startup for %s.", scraper.__class__.__name__)

        if selected_location == "ALL":
            collect_kwargs = {}
            if manual_workspace_root:
                collect_kwargs["manual_workspace_root"] = manual_workspace_root
            for loc_name, loc_url in loc_info.items():
                if _shutdown_check():
                    log_progress(logger, "Shutdown detected. Stopping before next location.")
                    break

                driver = process_location(
                    location_name=loc_name,
                    base_url=loc_url,
                    scraper=scraper,
                    driver=driver,
                    events_data=events_data,
                    failed_data=failed_data,
                    output_columns=OUTPUT_COLUMNS,
                    json_file=json_file,
                    failed_events_file=failed_events_file,
                    download_dir=DEFAULT_DOWNLOAD_DIR,
                    shutdown_check=_shutdown_check,
                    firefox_binary=firefox_path,
                    output_base_folder=output_base_folder,
                    collect_kwargs=collect_kwargs,
                    scheduler_history_file=scheduler_history_file,
                    run_mode="test" if test_mode else "live",
                )
        else:
            collect_kwargs = {}
            if manual_workspace_root:
                collect_kwargs["manual_workspace_root"] = manual_workspace_root
            driver = process_location(
                location_name=selected_location,
                base_url=loc_info,
                scraper=scraper,
                driver=driver,
                events_data=events_data,
                failed_data=failed_data,
                output_columns=OUTPUT_COLUMNS,
                json_file=json_file,
                failed_events_file=failed_events_file,
                download_dir=DEFAULT_DOWNLOAD_DIR,
                shutdown_check=_shutdown_check,
                firefox_binary=firefox_path,
                output_base_folder=output_base_folder,
                collect_kwargs=collect_kwargs,
                scheduler_history_file=scheduler_history_file,
                run_mode="test" if test_mode else "live",
            )

    except Exception as e:
        logger.error("An error occurred in CLI run: %s", e)

    finally:
        safe_quit_driver(driver)
        if manual_workspace_root:
            _cleanup_manual_play365_workspace(manual_workspace_root, scraper=scraper)
            _cleanup_manual_play365_workspace(manual_workspace_root)
        flush_logs()
        logging.shutdown()


def _run_scrape_flow(*, test_mode: bool = False):
    scraper = prompt_for_scraper()
    locations = load_locations(LOCATIONS_FILE)

    if scraper in {ALL_SCRAPERS_SENTINEL, VALIDATION_SCRAPERS_SENTINEL}:
        selected_batch = scraper
        log_progress(logger, "Selected scraper: %s", selected_batch)
        scraper_classes = get_scraper_classes()
        if selected_batch == VALIDATION_SCRAPERS_SENTINEL:
            wanted = {"NCS", "PerfectGame", "USSSA"}
            scraper_classes = [
                cls for cls in scraper_classes
                if getattr(cls, "SCRAPER_NAME", cls.__name__) in wanted
            ]

        for scraper_cls in scraper_classes:
            if _shutdown_check():
                log_progress(logger, "Shutdown detected. Stopping all-scraper batch.")
                break

            active_scraper = scraper_cls()
            selected_location, loc_info, active_scraper = _resolve_all_locations_for_scraper(locations, active_scraper)
            if selected_location == "None Found":
                logger.warning("Skipping %s because no locations were resolved.", active_scraper.__class__.__name__)
                continue

            log_progress(logger, "Effective scraper: %s", active_scraper.__class__.__name__)
            log_progress(logger, "Selected location: %s", selected_location)
            _run_selected_scraper(
                scraper=active_scraper,
                selected_location=selected_location,
                loc_info=loc_info,
                test_mode=test_mode,
            )
        return

    log_progress(logger, "Selected scraper: %s", scraper.__class__.__name__)
    selected_location, loc_info, scraper = prompt_for_location(locations, scraper)
    log_progress(logger, "Effective scraper: %s", scraper.__class__.__name__)
    log_progress(logger, "Selected location: %s", selected_location)
    _run_selected_scraper(
        scraper=scraper,
        selected_location=selected_location,
        loc_info=loc_info,
        test_mode=test_mode,
    )


def _run_data_control():
    print("\nData Control\n------------")
    print("1) Roll back newly added events")
    print("2) Return\n")

    while True:
        choice = input("Enter choice [1-2]: ").strip()
        if choice in {"1", "2"}:
            break
        print("Invalid choice. Please enter 1 or 2.")

    if choice == "2":
        return

    created_after = input("Roll back events created on/after (YYYY-MM-DD or ISO datetime): ").strip()
    scraper_filter = input("Optional scraper filter (blank for all): ").strip()
    location_filter = input("Optional location filter (blank for all): ").strip()
    reason = input("Optional rollback reason: ").strip()

    try:
        result = rollback_events(
            events_path=JSON_FILE,
            failed_events_path=FAILED_EVENTS_FILE,
            created_after=created_after,
            scraper_filter=scraper_filter,
            location_filter=location_filter,
            reason=reason,
        )
    except Exception as e:
        logger.error("Data Control rollback failed: %s", e)
        print(f"\nRollback failed: {e}")
        return

    print(
        "\nRollback complete:\n"
        f"  Cutoff: {result['cutoff']}\n"
        f"  Rolled back: {result['rolled_back']}\n"
        f"  Already deleted: {result['already_deleted']}\n"
        f"  Failed entries removed: {result['affected_failures_removed']}\n"
    )


# -------------------- CLI ENTRYPOINT -------------------------
def run_cli():
    try:
        action = prompt_main_action()

        if action == "1":
            _run_data_control()
        elif action == "2":
            _run_scrape_flow(test_mode=False)
        elif action == "3":
            _run_scrape_flow(test_mode=True)
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        flush_logs()
        logging.shutdown()


if __name__ == "__main__":
    run_cli()
