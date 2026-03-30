# common/driver_manager.py

import os
import time
import logging
from typing import Optional

from selenium import webdriver
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.firefox.options import Options
from selenium.common.exceptions import WebDriverException
from webdriver_manager.firefox import GeckoDriverManager

logger = logging.getLogger(__name__)

# Where Firefox will put any forced downloads (e.g., HTML served as an attachment).
DEFAULT_DOWNLOAD_DIR = os.path.abspath(os.path.join(os.getcwd(), "_selenium_downloads"))

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

def safe_quit_driver(driver) -> None:
    try:
        if driver is not None:
            driver.quit()
    except Exception:
        pass

def cleanup_download_dir(download_dir: str, max_delete: int = 500) -> None:
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
            logger.info("Cleaned up %d downloaded .htm/.html files in %s", deleted, download_dir)
    except Exception:
        # Never let cleanup fail the run
        pass

def initialize_driver(
    firefox_binary: Optional[str] = None,
    download_dir: Optional[str] = None,
    headless: bool = True,
    page_load_timeout_sec: Optional[int] = None,
):
    """
    Set up a Firefox driver with webdriver-manager.

    - Forces any "attachment" downloads into download_dir so you don't get random .htm files
      in your real Downloads folder.
    - Optionally sets a page load timeout to prevent certain hang scenarios.
    """
    download_dir = download_dir or DEFAULT_DOWNLOAD_DIR
    os.makedirs(download_dir, exist_ok=True)

    options = Options()
    if firefox_binary:
        options.binary_location = firefox_binary

    options.headless = headless
    options.accept_insecure_certs = True

    # Keep downloads contained (headless can't handle "Open/Save" prompts well)
    options.set_preference("browser.download.folderList", 2)  # custom location
    options.set_preference("browser.download.dir", download_dir)
    options.set_preference("browser.download.useDownloadDir", True)
    options.set_preference("browser.download.manager.showWhenStarting", False)
    options.set_preference("browser.download.alwaysOpenPanel", False)
    options.set_preference("browser.download.manager.focusWhenStarting", False)
    options.set_preference("browser.download.manager.alertOnEXEOpen", False)
    options.set_preference("browser.helperApps.alwaysAsk.force", False)
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

    if page_load_timeout_sec:
        try:
            driver.set_page_load_timeout(page_load_timeout_sec)
        except Exception:
            pass

    logger.info("Selenium WebDriver (Firefox) initialized. Download dir: %s", download_dir)
    return driver

def restart_driver(
    driver,
    *,
    firefox_binary: Optional[str] = None,
    download_dir: Optional[str] = None,
    headless: bool = True,
    page_load_timeout_sec: Optional[int] = None,
    sleep_after_sec: float = 2.0,
):
    """Quit an existing driver safely and return a fresh one."""
    safe_quit_driver(driver)
    new_driver = initialize_driver(
        firefox_binary=firefox_binary,
        download_dir=download_dir,
        headless=headless,
        page_load_timeout_sec=page_load_timeout_sec,
    )
    time.sleep(sleep_after_sec)
    return new_driver
