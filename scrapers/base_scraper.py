import abc
from typing import List, Dict
from selenium.webdriver import Firefox

class BaseScraper(abc.ABC):
    """
    BaseScraper defines the interface for any site-specific scraper.
    """

    SCRAPER_NAME = "Base"
    SPORT = "baseball"
    ENABLED_BY_DEFAULT = True
    REQUIRES_DRIVER = True
    BATCH_DELAY_SECONDS = 30
    SUPPORTS_ALL_LOCATIONS = True
    SUPPORTS_SPECIFIC_LOCATIONS = True
    LOCATIONS_SOURCE = "config"
    DYNAMIC_LOCATIONS = False

    def close(self):
        """Release instance-owned transports after a provider run."""
        closed = set()
        for name in ("session", "_http"):
            transport = getattr(self, name, None)
            if transport is not None and id(transport) not in closed:
                closed.add(id(transport))
                transport.close()

    def supports_all_locations(self) -> bool:
        return bool(getattr(self, "SUPPORTS_ALL_LOCATIONS", True))

    def supports_specific_locations(self) -> bool:
        return bool(getattr(self, "SUPPORTS_SPECIFIC_LOCATIONS", True))

    def uses_dynamic_locations(self) -> bool:
        return bool(
            getattr(self, "LOCATIONS_SOURCE", "config") == "dynamic"
            or getattr(self, "DYNAMIC_LOCATIONS", False)
        )

    @abc.abstractmethod
    def collect_event_links(self, driver: Firefox, base_url: str, **kwargs) -> List[str]:
        """
        Collect and return a list of event URLs from the given base_url.
        """
        pass

    @abc.abstractmethod
    def extract_event_details(self, url: str, driver: Firefox, **kwargs) -> Dict:
        """
        Given an event URL, extract and return a dictionary of event data.
        Return an empty dict or None if extraction fails.
        """
        pass

    @abc.abstractmethod
    def get_event_key(self, url: str) -> str:
        """
        Given an event URL, return a unique key for that event.
        This key is used to store and compare events in the events JSON.
        """
        pass
