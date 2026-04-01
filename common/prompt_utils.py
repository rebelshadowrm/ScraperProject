# common/prompt_utils.py

import os
import sys
import pkgutil
import importlib
import importlib.util
import inspect
from typing import Dict, Tuple
from scrapers.base_scraper import BaseScraper
import scrapers  # This ensures the scrapers package is imported


ALL_SCRAPERS_SENTINEL = "ALL_SCRAPERS"
VALIDATION_SCRAPERS_SENTINEL = "VALIDATION_SCRAPERS"

def get_scraper_classes():
    """
    Discover and return a list of all classes in the scrapers package
    that are subclasses of BaseScraper (excluding BaseScraper itself).
    """
    scraper_classes = []
    excluded_names = {
        "FiveToolScraper",
        "Play365GetScraper",
        "Play365GetAllScraper",
        "Play365PostScraper",
    }
    for module_info in pkgutil.iter_modules(scrapers.__path__):
        module_name = module_info.name
        module = importlib.import_module(f"scrapers.{module_name}")
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ != module.__name__:
                continue
            if name in excluded_names:
                continue
            if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                scraper_classes.append(obj)
    return scraper_classes

def prompt_main_action() -> str:
    """
    Prompt the user to select between:
    1) Data Control
    2) Normal Scrape
    3) Test Scrape
    Returns one of '1'..'3'.
    """
    print("What do you want to do?\n")
    print("1) Data Control")
    print("2) Normal scraper menus")
    print("3) Test scraper run (isolated output)\n")

    while True:
        choice = input("Enter choice [1-3]: ").strip()
        if choice in ["1", "2", "3"]:
            return choice
        print("Invalid choice. Please enter a number from 1 to 3.")

def prompt_for_scraper():
    """
    Prompts the user to select a scraper from the dynamically discovered list.
    Returns an instance of the selected scraper.
    """
    scraper_classes = get_scraper_classes()
    
    if not scraper_classes:
        print("No scrapers found! Exiting...")
        sys.exit(1)

    print("Which site do you want to scrape?")
    print("-1) Validation subset (NCS, PerfectGame, USSSA)")
    print("0) Pull all scrapers")
    for idx, scraper_cls in enumerate(scraper_classes, start=1):
        display_name = getattr(scraper_cls, 'SCRAPER_NAME', scraper_cls.__name__)
        print(f"{idx}) {display_name}")

    while True:
        choice = input(f"Enter choice [-1-{len(scraper_classes)}]: ").strip()
        try:
            choice_int = int(choice)
            if choice_int == -1:
                print("Selected: Validation subset")
                return VALIDATION_SCRAPERS_SENTINEL
            if choice_int == 0:
                print("Selected: All scrapers")
                return ALL_SCRAPERS_SENTINEL
            if 1 <= choice_int <= len(scraper_classes):
                selected_scraper = scraper_classes[choice_int - 1]()
                print(f"Selected: {getattr(selected_scraper, 'SCRAPER_NAME', selected_scraper.__class__.__name__)}")
                return selected_scraper
            else:
                print(f"Please choose a number between -1 and {len(scraper_classes)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

def _prompt_from_mapping(
    options: Dict[str, str],
    *,
    title: str,
    allow_all: bool = True,
    show_values: bool = True,
    all_label: str = "All locations for mass pull",
) -> Tuple[str, str]:
    if not options:
        return "None Found", ""

    print(f"\n{title}:\n-------------------")
    if allow_all:
        print(f"0) {all_label}")

    for i, (name, value) in enumerate(options.items(), start=1):
        if show_values:
            print(f"{i}) {name} ({value})")
        else:
            print(f"{i}) {name}")

    while True:
        try:
            prompt = "Select a location by number"
            if allow_all:
                prompt += " (or 0 for all)"
            choice = int(input(f"{prompt}: "))
            if allow_all and choice == 0:
                return "ALL", ""
            if 1 <= choice <= len(options):
                selected_name = list(options.keys())[choice - 1]
                return selected_name, options[selected_name]
            low = 0 if allow_all else 1
            print(f"Invalid selection. Choose a number between {low} and {len(options)}.")
        except ValueError:
            print("Please enter a valid number.")


def _load_scraper_from_reference(reference: str) -> BaseScraper:
    raw = reference.strip()

    if ":" not in raw:
        raise ValueError(f"Invalid scraper reference '{reference}'. Expected '<path_or_module>:<ClassName>'.")

    target, class_name = raw.rsplit(":", 1)
    target = target.strip()
    class_name = class_name.strip()

    if not target or not class_name:
        raise ValueError(f"Invalid scraper reference '{reference}'.")

    if target.endswith(".py") or target.startswith(".") or os.path.sep in target or "/" in target:
        abs_path = os.path.abspath(os.path.expanduser(target))
        module_name = f"_dynamic_scraper_{os.path.splitext(os.path.basename(abs_path))[0]}"
        spec = importlib.util.spec_from_file_location(module_name, abs_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Could not load scraper module from path: {abs_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)

    scraper_cls = getattr(module, class_name, None)
    if scraper_cls is None or not inspect.isclass(scraper_cls) or not issubclass(scraper_cls, BaseScraper):
        raise TypeError(f"Reference '{reference}' does not resolve to a BaseScraper subclass.")

    return scraper_cls()


def _is_play365_base_scraper(scraper) -> bool:
    return getattr(scraper, "SCRAPER_NAME", scraper.__class__.__name__) == "Play365Base"


def prompt_for_location(all_locations: Dict[str, str], scraper):
    """
    Prompt the user to select a location.
    - If scraper provides dynamic locations (e.g., TopGun), use those.
    - Otherwise, fall back to filtering locations.json by "(SCRAPER_NAME)".
    """
    active_scraper = scraper
    scraper_name = getattr(scraper, 'SCRAPER_NAME', scraper.__class__.__name__)

    if _is_play365_base_scraper(scraper):
        play365_targets = {
            name: url for name, url in all_locations.items()
            if "(Play365Base)" in name
        }

        if not play365_targets:
            print("\nNo Play365 sub-scrapers are configured in locations.json.")
            return "None Found", "", scraper

        selected_target, scraper_ref = _prompt_from_mapping(
            play365_targets,
            title="Available Play365 scrapers",
            allow_all=True,
            show_values=False,
            all_label="Pull all Play365 scrapers",
        )
        if selected_target == "ALL":
            return "ALL_PLAY365_SCRAPERS", play365_targets, scraper
        try:
            active_scraper = _load_scraper_from_reference(scraper_ref)
        except Exception as e:
            print(f"\nFailed to load Play365 sub-scraper '{selected_target}': {e}")
            return "None Found", "", scraper

        scraper_name = getattr(active_scraper, 'SCRAPER_NAME', active_scraper.__class__.__name__)

        if (
            getattr(active_scraper, "PREPARE_DUMP_IN_PROMPT", True)
            and hasattr(active_scraper, "_default_dump_path")
            and hasattr(active_scraper, "_ensure_dump_exists")
        ):
            try:
                active_scraper._ensure_dump_exists(active_scraper._default_dump_path())
            except Exception as e:
                print(f"\nFailed to prepare dump for {scraper_name}: {e}")
                return "None Found", "", scraper

    supports_all = active_scraper.supports_all_locations() if hasattr(active_scraper, "supports_all_locations") else True
    supports_specific = (
        active_scraper.supports_specific_locations()
        if hasattr(active_scraper, "supports_specific_locations")
        else True
    )

    # --- Dynamic locations path (TopGun-style) ---
    if getattr(active_scraper, "uses_dynamic_locations", None) and active_scraper.uses_dynamic_locations() and hasattr(active_scraper, "get_dynamic_locations"):
        try:
            filtered_locations = active_scraper.get_dynamic_locations()
        except Exception as e:
            print(f"\nFailed to load dynamic locations for {scraper_name}: {e}")
            filtered_locations = {}
    else:
        # --- Existing locations.json path ---
        filtered_locations = {
            name: url for name, url in all_locations.items()
            if f"({scraper_name})" in name
        }

    if not filtered_locations:
        print("\nNo matching locations found for this scraper type!")
        return "None Found", "", active_scraper

    if not supports_specific and supports_all:
        print(f"\n{scraper_name} will process all available locations.")
        return "ALL", filtered_locations, active_scraper

    selected_name, selected_value = _prompt_from_mapping(
        filtered_locations,
        title="Available locations",
        allow_all=supports_all,
    )

    if selected_name == "ALL":
        return "ALL", filtered_locations, active_scraper

    return selected_name, selected_value, active_scraper


def prompt_for_sheets_subfolder(sheets_folder: str = "Sheets") -> str:
    """
    Lists subfolders under the given sheets_folder and
    prompts the user to select one.
    Returns the name of the selected subfolder.
    """
    if not os.path.isdir(sheets_folder):
        print(f"Folder '{sheets_folder}' does not exist. Exiting...")
        sys.exit(1)

    # List only subdirectories (not files)
    subfolders = [
        d for d in os.listdir(sheets_folder)
        if os.path.isdir(os.path.join(sheets_folder, d))
    ]

    if not subfolders:
        print(f"No subfolders found in '{sheets_folder}'. Exiting...")
        sys.exit(1)

    # Sort in descending order (most recent first) assuming YYYY-MM-DD format
    subfolders = sorted(subfolders, reverse=True)

    print("\nWhich subfolder in 'Sheets' do you want to consolidate?")
    for idx, folder_name in enumerate(subfolders, start=1):
        print(f"{idx}) {folder_name}")

    while True:
        choice = input(f"Enter choice [1-{len(subfolders)}]: ").strip()
        try:
            choice_int = int(choice)
            if 1 <= choice_int <= len(subfolders):
                return subfolders[choice_int - 1]
            else:
                print(f"Please choose a number between 1 and {len(subfolders)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")
