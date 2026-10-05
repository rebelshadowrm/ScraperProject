import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup
from selenium.common.exceptions import WebDriverException, InvalidSessionIdException
from common.collection import validated_records
from scrapers.game7baseball_scraper import Game7BaseballScraper
from scrapers.grandslam_scraper import GrandSlamScraper
from scrapers.triplecrown_scraper import TripleCrownScraper
from scrapers.ripkenbaseball_scraper import RipkenBaseballScraper
from scrapers.pst_scraper import PSTScraper
from scrapers.ncs_scraper import NCSScraper
from scrapers.usssa_scraper import USSSAScraper
from scrapers.pg_scraper import PGScraper
from provider_replay import TC_LISTING, FixedDate


class ProviderSafeguardTests(unittest.TestCase):
    def test_record_lists_distinguish_empty_from_missing_null_and_bad_records(self):
        self.assertEqual(validated_records({"Results": []}, field="Results"), [])
        for payload in ({}, {"Results": None}, {"Results": {}}, {"Results": [None]}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                validated_records(payload, field="Results")

    def test_html_collectors_distinguish_empty_from_changed_or_blocked_html(self):
        for cls, mod in ((Game7BaseballScraper, "game7baseball"), (GrandSlamScraper, "grandslam"),
                         (TripleCrownScraper, "triplecrown")):
            for html, complete in (("<h1>Access denied</h1>", False), ("<p>No events found</p>", True)):
                with self.subTest(provider=cls.__name__, html=html), \
                        patch(f"scrapers.{mod}_scraper._soup", return_value=BeautifulSoup(html, "html.parser")):
                    result = cls().collect_event_links(None, "https://example.com/?state=TX")
                    self.assertEqual(result.complete, complete)

    def test_ripken_rejects_missing_credentials_and_null_programs(self):
        scraper = RipkenBaseballScraper()
        with self.assertRaises(ValueError):
            scraper.collect_event_links(None, "https://example.com")
        with patch.object(scraper.session, "get", return_value=MagicMock(json=lambda: None)), self.assertRaises(ValueError):
            scraper.collect_event_links(None, "https://example.com/?siteId=1&apiKey=fixture")

    def test_pst_malformed_results_returns_incomplete_with_dom_fallback(self):
        scraper = PSTScraper()
        driver = MagicMock()
        with patch("scrapers.pst_scraper.WebDriverWait", return_value=MagicMock()), \
                patch.object(scraper.session, "post", return_value=MagicMock(text="{}", json=lambda: {})), \
                patch.object(scraper, "_extract_event_links_from_dom", return_value=["https://example.com/event"]):
            result = scraper.collect_event_links(driver, "https://example.com/?storeorganizationid=1")
        self.assertFalse(result.complete)
        self.assertEqual(list(result), ["https://example.com/event"])

    def test_driver_disconnects_escape_extraction_and_bootstrap(self):
        for cls in (NCSScraper, USSSAScraper, PSTScraper, PGScraper):
            for exception in (WebDriverException("invalid session id"), ConnectionError("connection refused"),
                              InvalidSessionIdException("WebDriver session does not exist, or is not active")):
                scraper, driver = cls(), MagicMock()
                driver.get.side_effect = exception
                with self.subTest(provider=cls.__name__, exception=type(exception)), self.assertRaises(Exception):
                    if cls is PSTScraper:
                        scraper.collect_event_links(driver, "https://example.com/?storeorganizationid=1")
                    else:
                        scraper.extract_event_details("https://example.com/Details/1", driver)

    def test_triplecrown_unknown_date_marks_partial(self):
        html = TC_LISTING.replace('<div>20</div><div>Jul</div><div>2099</div>', '<div>TBD</div>')
        with patch("scrapers.triplecrown_scraper._soup", return_value=BeautifulSoup(html, "html.parser")), \
                patch("scrapers.triplecrown_scraper.date", FixedDate):
            result = TripleCrownScraper().collect_event_links(None, "https://example.com/?state=TX")
        self.assertFalse(result.complete)
        self.assertIn("date", result.error)

    def test_usssa_reuses_first_page_retains_query_and_detects_repeated_event_content(self):
        driver = MagicMock()
        html = '<div class="pagination initial_query_pagenav"><a class="page-numbers">2</a></div><a href="https://txbaseball.usssa.com/event/classic/">Classic</a>'
        driver.get.side_effect = lambda url: setattr(driver, "page_source", html + f'<!--{url}-->')
        scraper = USSSAScraper()
        with patch.object(scraper, "_wait_for_listing_state"), patch("scrapers.usssa_scraper.time.sleep"):
            result = scraper.collect_event_links(driver, "https://txbaseball.usssa.com/events/?state=TX", pause=0)
        self.assertEqual(driver.get.call_count, 2)
        self.assertIn("state=TX", driver.get.call_args.args[0])
        self.assertIn("paged=2", driver.get.call_args.args[0])
        self.assertFalse(result.complete)
        self.assertEqual(len(result), 1)

    def test_game7_keeps_collected_links_when_later_request_fails(self):
        html = '<div class="media-list media-list-events"><div class="media"><div class="media-body"><div class="h4"><a href="/baseball/Events/Details/123/classic">Classic</a></div></div></div></div><ul class="pagination"><li class="next"><a href="?page=2">Next</a></li></ul>'
        with patch("scrapers.game7baseball_scraper._soup", side_effect=[BeautifulSoup(html, "html.parser"), RuntimeError("network")]):
            result = Game7BaseballScraper().collect_event_links(None, "https://example.com/?state=TX")
        self.assertFalse(result.complete)
        self.assertEqual(len(result), 1)
        self.assertIn("network", result.error)

    def test_perfectgame_repeated_identity_page_is_detected_despite_html_changes(self):
        driver = MagicMock(page_source='<table class="rgMasterTable"><tr class="rgGroupHeader"><a href="GroupedEvents.aspx?gid=123">Classic</a></tr></table>')
        button = MagicMock()
        button.get_attribute.side_effect = lambda name: "" if name == "class" else "go()"
        driver.find_elements.return_value = [button]
        def click(*args):
            driver.page_source += "<!--new request timestamp-->"
        driver.execute_script.side_effect = click
        with patch("scrapers.pg_scraper.WebDriverWait", return_value=MagicMock()), patch("scrapers.pg_scraper.time.sleep"):
            result = PGScraper().collect_event_links(driver, "https://www.perfectgame.org/Schedule/", pause=0)
        self.assertFalse(result.complete)
        self.assertEqual(len(result), 1)
        self.assertIn("repeated", result.error)

    def test_perfectgame_unrecognized_table_is_partial_and_explicit_empty_is_complete(self):
        for text, complete in (("Changed layout", False), ("No records to display", True)):
            driver = MagicMock(page_source=f'<table class="rgMasterTable">{text}</table>')
            with self.subTest(text=text), patch("scrapers.pg_scraper.WebDriverWait", return_value=MagicMock()):
                result = PGScraper().collect_event_links(driver, "https://example.com")
            self.assertEqual(result.complete, complete)


if __name__ == "__main__":
    unittest.main()
