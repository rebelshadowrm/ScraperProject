# common/prompt_utils.py

import os
import sys
import pkgutil
import importlib
import inspect
from typing import Dict
from scrapers.base_scraper import BaseScraper
import scrapers  # This ensures the scrapers package is imported

def get_scraper_classes():
    """
    Discover and return a list of all classes in the scrapers package
    that are subclasses of BaseScraper (excluding BaseScraper itself).
    """
    scraper_classes = []
    for module_info in pkgutil.iter_modules(scrapers.__path__):
        module_name = module_info.name
        module = importlib.import_module(f"scrapers.{module_name}")
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseScraper) and obj is not BaseScraper:
                scraper_classes.append(obj)
    return scraper_classes

def prompt_main_action() -> str:
    """
    Prompt the user to select between:
    1) Scrape
    2) Consolidate Excel Files
    Returns either '1' or '2'.
    """
    print("What do you want to do?\n")
    print("1) Scrape websites (using discovered scrapers)")
    print("2) Consolidate Excel files (by folder)\n")

    while True:
        choice = input("Enter choice [1 or 2]: ").strip()
        if choice in ["1", "2"]:
            return choice
        print("Invalid choice. Please enter '1' or '2'.")

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
    for idx, scraper_cls in enumerate(scraper_classes, start=1):
        display_name = getattr(scraper_cls, 'SCRAPER_NAME', scraper_cls.__name__)
        print(f"{idx}) {display_name}")

    while True:
        choice = input(f"Enter choice [1-{len(scraper_classes)}]: ").strip()
        try:
            choice_int = int(choice)
            if 1 <= choice_int <= len(scraper_classes):
                selected_scraper = scraper_classes[choice_int - 1]()
                print(f"Selected: {getattr(selected_scraper, 'SCRAPER_NAME', selected_scraper.__class__.__name__)}")
                return selected_scraper
            else:
                print(f"Please choose a number between 1 and {len(scraper_classes)}.")
        except ValueError:
            print("Invalid input. Please enter a number.")

def prompt_for_location(all_locations: Dict[str, str], scraper):
    """
    Prompt the user to select a location.
    - If scraper provides dynamic locations (e.g., TopGun), use those.
    - Otherwise, fall back to filtering locations.json by "(SCRAPER_NAME)".
    """
    scraper_name = getattr(scraper, 'SCRAPER_NAME', scraper.__class__.__name__)

    # --- Dynamic locations path (TopGun-style) ---
    if getattr(scraper, "DYNAMIC_LOCATIONS", False) and hasattr(scraper, "get_dynamic_locations"):
        try:
            filtered_locations = scraper.get_dynamic_locations()
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
        return "None Found", ""

    print("\nAvailable locations:\n-------------------")
    print("0) All locations for mass pull")
    for i, (name, url) in enumerate(filtered_locations.items(), start=1):
        print(f"{i}) {name} ({url})")

    while True:
        try:
            choice = int(input("Select a location by number (or 0 for all): "))
            if choice == 0:
                return "ALL", filtered_locations
            elif 1 <= choice <= len(filtered_locations):
                selected_name = list(filtered_locations.keys())[choice - 1]
                return selected_name, filtered_locations[selected_name]
            else:
                print(f"Invalid selection. Choose a number between 0 and {len(filtered_locations)}.")
        except ValueError:
            print("Please enter a valid number.")


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
