# ui/cli.py

import os
import sys
import json
import signal
import logging
import argparse
from datetime import date
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
from common.storage import ledger_lock
from common.pull_options import PullOptions
from common.run_manifest import RunManifest
from common.excel_utils import validate_template
from common.identity import pending_locations

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
_pull_running = False

logger = logging.getLogger(__name__)

JSON_FILE = "events.json"
FAILED_EVENTS_FILE = "failed_events.json"
SCHEDULER_HISTORY_FILE = "scheduler_history.json"
LOCATIONS_FILE = "locations.json"


# -------------------- SIGNAL HANDLER -------------------------
def _signal_handler(sig, frame):
    global shutdown_flag
    if shutdown_flag or not _pull_running:
        shutdown_flag = True
        raise KeyboardInterrupt
    logger.info("Shutdown signal received. Preparing to exit gracefully...")
    shutdown_flag = True
    flush_logs()


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
    pull_options=None,
    manifest=None,
    unattended=False,
):
    firefox_path = os.environ.get("SCRAPER_FIREFOX_BINARY", "C:/Program Files/Mozilla Firefox/firefox.exe")
    manual_failures: list[tuple[str, str]] = []

    for display_name, scraper_ref in play365_targets.items():
        if _shutdown_check():
            log_progress(logger, "Shutdown detected. Stopping Play365 automatic sweep.")
            break

        scraper = None
        driver = None
        try:
            scraper = _load_scraper_from_reference(scraper_ref)
            log_progress(logger, "[Play365] Automatic phase: %s", scraper.SCRAPER_NAME)
            locations = scraper.get_dynamic_locations()
            if manifest:
                manifest.provider(display_name, "running", locations=locations)
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
                driver = _process_location_safe(
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
                    pull_options=pull_options,
                    run_date=manifest.run_date if manifest else None,
                    manifest=manifest,
                    provider_label=display_name,
                )
            if manifest:
                manifest.provider(display_name, "interrupted" if _shutdown_check() else "complete", locations=locations)
            safe_quit_driver(driver)
        except Play365ManualFallbackRequired:
            safe_quit_driver(driver)
            logger.warning("[Play365] Automatic phase failed for %s. Queued for manual fallback.", display_name)
            manual_failures.append((display_name, scraper_ref))
            if manifest:
                manifest.provider(display_name, "queued", error="Manual fallback required")
        except Exception as exc:
            safe_quit_driver(driver)
            logger.exception("Play365 provider failed: %s", display_name)
            if manifest:
                manifest.provider(display_name, "failed", error=str(exc))

    if not manual_failures or _shutdown_check():
        return
    if unattended:
        logger.warning("Deferred manual Play365 work: %s", ", ".join(name for name, _ in manual_failures))
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
    for display_name, scraper_ref in manual_failures:
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
                driver = _process_location_safe(
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
                    pull_options=pull_options,
                    run_date=manifest.run_date if manifest else None,
                    manifest=manifest,
                    provider_label=display_name,
                )
            if manifest:
                manifest.provider(display_name, "interrupted" if _shutdown_check() else "complete", locations=locations)
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


def _run_selected_scraper(**kwargs):
    try:
        if kwargs.get("test_mode", False):
            return _run_selected_scraper_unlocked(**kwargs)
        with ledger_lock(JSON_FILE):
            return _run_selected_scraper_unlocked(**kwargs)
    finally:
        close = getattr(kwargs["scraper"], "close", None)
        if callable(close):
            close()


def _process_location_safe(*, manifest=None, provider_label=None, **kwargs):
    scraper = kwargs["scraper"]
    provider = provider_label or scraper.__class__.__name__
    try:
        driver = process_location(**kwargs)
        if manifest:
            manifest.location(provider, kwargs["location_name"], getattr(scraper, "_last_location_result", {"status": "complete"}))
        return driver
    except Play365ManualFallbackRequired:
        raise
    except Exception as exc:
        logger.exception("Location failed; continuing independent locations: %s", kwargs["location_name"])
        if manifest:
            manifest.location(provider, kwargs["location_name"], {"status": "failed", "error": str(exc)})
        return getattr(scraper, "_active_driver", kwargs["driver"])


def _run_selected_scraper_unlocked(*, scraper, selected_location, loc_info, test_mode: bool = False,
                                  pull_options=None, manifest=None, unattended=False):
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
    if manifest and pull_options and pull_options.mode == "snapshot":
        output_base_folder = manifest.output_root
    validate_template(OUTPUT_COLUMNS)

    logger = setup_logger(log_file=build_run_log_path(run_label, logs_root="Logs"), log_level=logging.INFO)
    log_progress(logger, "Run log created for %s.", run_label)

    events_data = load_events(json_file)
    failed_data = load_failed_events(failed_events_file)
    if selected_location == "ALL":
        for name, source in pending_locations(events_data, scraper).items():
            loc_info.setdefault(name, source)
    if not os.path.exists(scheduler_history_file):
        save_scheduler_history(load_scheduler_history(scheduler_history_file), scheduler_history_file)

    driver = None
    firefox_path = os.environ.get("SCRAPER_FIREFOX_BINARY", "C:/Program Files/Mozilla Firefox/firefox.exe")
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
                pull_options=pull_options,
                manifest=manifest,
                unattended=unattended,
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

                driver = _process_location_safe(
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
                    pull_options=pull_options,
                    run_date=manifest.run_date if manifest else None,
                    manifest=manifest,
                )
        else:
            collect_kwargs = {}
            if manual_workspace_root:
                collect_kwargs["manual_workspace_root"] = manual_workspace_root
            driver = _process_location_safe(
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
                pull_options=pull_options,
                run_date=manifest.run_date if manifest else None,
                manifest=manifest,
            )

    except Exception as e:
        logger.error("An error occurred in CLI run: %s", e)
        if manifest:
            manifest.provider(scraper.__class__.__name__, "failed", error=str(e))
        raise

    finally:
        safe_quit_driver(driver)
        if manual_workspace_root:
            _cleanup_manual_play365_workspace(manual_workspace_root, scraper=scraper)
            _cleanup_manual_play365_workspace(manual_workspace_root)
        flush_logs()


def _run_scrape_flow(*, test_mode=False, all_scrapers=False, pull_options=None, interactive_fallback=False, sport="baseball"):
    global _pull_running
    scraper = ALL_SCRAPERS_SENTINEL if all_scrapers else prompt_for_scraper()
    locations = load_locations(LOCATIONS_FILE)
    batch = scraper in {ALL_SCRAPERS_SENTINEL, VALIDATION_SCRAPERS_SENTINEL}
    options = pull_options or PullOptions("snapshot" if batch else "incremental")
    manifest = RunManifest(options, test_mode=test_mode)
    manifest.data["sport"] = sport
    manifest.save()
    _pull_running = True
    log_progress(logger, "Pull mode: %s. Run manifest: %s", options.mode, manifest.path)
    try:
        if batch:
            scraper_classes = get_scraper_classes()
            scraper_classes = [cls for cls in scraper_classes if sport == "all" or getattr(cls, "SPORT", "baseball") == sport]
            if sport in ("softball", "all"):
                from common.provider_access import softball_classes
                scraper_classes.extend(softball_classes(manifest))
            if scraper == VALIDATION_SCRAPERS_SENTINEL:
                wanted = {"NCS", "PerfectGame", "USSSA"}
                scraper_classes = [cls for cls in scraper_classes if cls.SCRAPER_NAME in wanted]
            for cls in scraper_classes:
                manifest.provider(cls.__name__, "planned")
                if cls.SCRAPER_NAME == "Play365Base":
                    for label in locations:
                        if "(Play365Base)" in label:
                            manifest.provider(label, "planned")
            for cls in scraper_classes:
                if _shutdown_check():
                    break
                name = cls.__name__
                try:
                    active = cls()
                    discovery_error = ""
                    try:
                        selection, info, active = _resolve_all_locations_for_scraper(locations, active)
                    except Exception as exc:
                        recovery = {} if test_mode else pending_locations(load_events(JSON_FILE), active)
                        if not recovery:
                            raise
                        logger.exception("Location discovery failed; attempting staged export recovery for %s", name)
                        discovery_error = str(exc)
                        selection, info = "ALL", recovery
                    if selection == "None Found" and not test_mode:
                        recovery = pending_locations(load_events(JSON_FILE), active)
                        if recovery:
                            selection, info = "ALL", recovery
                    if selection == "None Found":
                        manifest.provider(name, "skipped", error="No locations resolved")
                        continue
                    manifest.provider(name, "running", locations=info)
                    _run_selected_scraper(scraper=active, selected_location=selection, loc_info=info,
                                          test_mode=test_mode, pull_options=options, manifest=manifest,
                                          unattended=not interactive_fallback)
                    outcomes = [result["status"] for key, result in manifest.data["locations"].items()
                                if key.startswith(name + "|")]
                    manifest.provider(name, "interrupted" if _shutdown_check() else
                                      "partial" if discovery_error or any(s != "complete" for s in outcomes) else "complete",
                                      locations=info, error=discovery_error)
                except Exception as exc:
                    logger.exception("Provider failed; continuing automatic sweep: %s", name)
                    manifest.provider(name, "failed", error=str(exc))
        else:
            name = scraper.__class__.__name__
            selection, info, scraper = prompt_for_location(locations, scraper)
            manifest.provider(name, "running", locations=info if isinstance(info, dict) else [selection])
            if selection == "None Found":
                manifest.provider(name, "skipped", error="No locations resolved")
            else:
                try:
                    _run_selected_scraper(scraper=scraper, selected_location=selection, loc_info=info,
                                          test_mode=test_mode, pull_options=options, manifest=manifest)
                    manifest.provider(name, "complete")
                except Exception as exc:
                    manifest.provider(name, "failed", error=str(exc))
    except Exception as exc:
        manifest.provider("Run setup", "failed", error=str(exc))
        raise
    finally:
        _pull_running = False
        manifest.finish(interrupted=_shutdown_check())
        log_progress(logger, "Pull finished: %s. Manifest: %s", manifest.data["status"], manifest.path)
    return manifest


def _repair_snapshot(path):
    """Retry incomplete providers in the same destination, keeping successful output."""
    global _pull_running
    # Hold one lock through loading, discovery, retries, and the final checkpoint.
    with ledger_lock(JSON_FILE):
        manifest = RunManifest.load_for_repair(path)
        options = PullOptions("snapshot",
                              date.fromisoformat(manifest.data["start_date"]) if manifest.data["start_date"] else None,
                              date.fromisoformat(manifest.data["end_date"]) if manifest.data["end_date"] else None)
        configured = load_locations(LOCATIONS_FILE)
        manifest.begin_repair()
        _pull_running = True
        try:
            repair_classes = get_scraper_classes()
            if manifest.data.get("sport") in ("softball", "all"):
                from common.provider_access import softball_classes
                repair_classes.extend(softball_classes(manifest))
            for cls in repair_classes:
                if _shutdown_check():
                    break
                name = cls.__name__
                expected = manifest.data["providers"].get(name)
                if expected is None:
                    continue
                is_dispatcher = cls.SCRAPER_NAME == "Play365Base"
                if is_dispatcher:
                    targets = {label: ref for label, ref in configured.items()
                               if "(Play365Base)" in label and
                               (manifest.data["providers"].get(label, {}).get("status") != "complete" or
                                any(v.get("status") != "complete" for k, v in manifest.data["locations"].items()
                                    if k.startswith(label + "|")))}
                    if not targets:
                        manifest.provider(name, "complete")
                        continue
                else:
                    outcomes = [v for k, v in manifest.data["locations"].items() if k.startswith(name + "|")]
                    missing = any(f"{name}|{loc}" not in manifest.data["locations"] for loc in expected.get("locations", []))
                    if expected["status"] == "complete" and not missing and all(v.get("status") == "complete" for v in outcomes):
                        continue
                active = None
                try:
                    active = cls()
                    selection, info, active = _resolve_all_locations_for_scraper(configured, active)
                    if selection == "None Found":
                        raise ValueError("No locations resolved for incomplete provider")
                    if is_dispatcher:
                        info = targets
                    else:
                        info = {loc: url for loc, url in info.items()
                                if manifest.data["locations"].get(f"{name}|{loc}", {}).get("status") != "complete"}
                        unresolved = [loc for loc in expected.get("locations", [])
                                      if manifest.data["locations"].get(f"{name}|{loc}", {}).get("status") != "complete" and loc not in info]
                        if unresolved:
                            raise ValueError("Previously expected locations missing from discovery: " + ", ".join(unresolved))
                    manifest.provider(name, "running")
                    _run_selected_scraper_unlocked(scraper=active, selected_location=selection, loc_info=info,
                                                   pull_options=options, manifest=manifest, unattended=True)
                    if is_dispatcher:
                        statuses = [manifest.data["providers"].get(label, {}).get("status")
                                    for label in expected.get("locations", [])]
                    else:
                        statuses = [manifest.data["locations"].get(f"{name}|{loc}", {}).get("status")
                                    for loc in set(expected.get("locations", [])) | set(info)]
                        expected["locations"] = list(dict.fromkeys(expected.get("locations", []) + list(info)))
                    manifest.provider(name, "interrupted" if _shutdown_check() else
                                      "complete" if all(s == "complete" for s in statuses) else "partial",
                                      locations=expected.get("locations", []))
                except Exception as exc:
                    logger.exception("Snapshot repair failed for %s; continuing other providers", name)
                    manifest.provider(name, "failed", error=str(exc))
                finally:
                    close = getattr(active, "close", None)
                    if callable(close):
                        close()
        finally:
            _pull_running = False
            manifest.finish(interrupted=_shutdown_check())
            manifest.data["repair_attempts"][-1]["finished_at"] = manifest.data["finished_at"]
            manifest.data["repair_attempts"][-1]["status"] = manifest.data["status"]
            manifest.save()
        return manifest


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
def run_cli(argv=None):
    global logger, shutdown_flag
    parser = argparse.ArgumentParser(description="Pull baseball tournaments with recoverable exports.")
    parser.add_argument("--all", action="store_true", help="Run every provider without scraper/location menus")
    parser.add_argument("--sport", choices=("baseball", "softball", "all"), default="baseball",
                        help="Sport selection for --all; softball providers require recorded source permission")
    parser.add_argument("--repair-snapshot", help="Retry unfinished work in a finished snapshot from today")
    parser.add_argument("--test", action="store_true", help="Use isolated test state and output")
    parser.add_argument("--mode", choices=("incremental", "refresh", "snapshot"),
                        help="All-provider runs default to refreshed snapshots")
    parser.add_argument("--start-date", type=date.fromisoformat, help="Inclusive event start-date lower bound (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=date.fromisoformat, help="Inclusive event start-date upper bound (YYYY-MM-DD)")
    parser.add_argument("--interactive-fallback", action="store_true", help="Allow manual fallback prompts during a full sweep")
    args = parser.parse_args(argv)
    if args.sport != "baseball" and (not args.all or args.repair_snapshot):
        parser.error("--sport softball/all requires --all; repair uses recorded sport settings")
    if args.repair_snapshot and (args.all or args.test or args.mode or args.start_date or args.end_date or args.interactive_fallback):
        parser.error("--repair-snapshot uses the original run settings and cannot be combined with pull options")
    if args.start_date and args.end_date and args.start_date > args.end_date:
        parser.error("--start-date must be on or before --end-date")
    options = PullOptions(args.mode or ("snapshot" if args.all or args.start_date or args.end_date else "incremental"), args.start_date, args.end_date)
    if not args.mode and not args.all:
        options = None if not (args.start_date or args.end_date) else options
    shutdown_flag = False
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    cleanup_old_logs(logs_root="Logs", keep_runs=50, keep_days=60)
    logger = setup_logger(log_file=build_run_log_path("startup", logs_root="Logs"), log_level=logging.INFO)
    try:
        if args.repair_snapshot:
            manifest = _repair_snapshot(args.repair_snapshot)
            return 0 if manifest.data["status"] == "complete" else 1
        if args.all:
            manifest = _run_scrape_flow(test_mode=args.test, all_scrapers=True, pull_options=options,
                                      interactive_fallback=args.interactive_fallback, sport=args.sport)
            return 0 if manifest.data["status"] == "complete" else 1
        action = prompt_main_action()

        if action == "1":
            _run_data_control()
        elif action == "2":
            _run_scrape_flow(test_mode=args.test, pull_options=options,
                             interactive_fallback=args.interactive_fallback)
        elif action == "3":
            _run_scrape_flow(test_mode=True, pull_options=options,
                             interactive_fallback=args.interactive_fallback)
    except KeyboardInterrupt:
        print("\nOperation cancelled.")
        flush_logs()
        return 130
    except Exception:
        logger.exception("Pull failed")
        return 1
    finally:
        logging.shutdown()


if __name__ == "__main__":
    run_cli()
