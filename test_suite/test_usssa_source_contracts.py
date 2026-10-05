import json
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

import scrapers.usssa_scraper as module
from common.validation import validate_event_data


class USSSASourceContracts(unittest.TestCase):
    def setUp(self):
        self.scraper = module.USSSAScraper()

    def schema(self, **overrides):
        event = {"@type": "Event", "name": "New Year’s Bash", "startDate": "2026-12-28", "endDate": "2027-01-03"}
        event.update(overrides)
        return BeautifulSoup('<script type="application/ld+json">' + json.dumps(event) + '</script>', "html.parser")

    def test_published_iso_dates_correct_reversed_visible_range(self):
        self.assertEqual(self.scraper._repair_dates_from_event_schema(self.schema(), "New Year’s Bash", "Dec 28 2027", "Jan 03 2027"), ("12/28/2026", "01/03/2027"))

    def test_valid_visible_dates_remain_identical_and_bad_schema_cannot_invent_dates(self):
        valid = ("Dec 28 2026", "Dec 30 2026")
        self.assertEqual(self.scraper._repair_dates_from_event_schema(self.schema(), "New Year’s Bash", *valid), valid)
        invalid = ("Dec 28 2027", "Jan 03 2027")
        for overrides in [{"name": "Different Event"}, {"@type": "WebPage"}, {"startDate": "TBD"}, {"endDate": "2025-01-01"}]:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.scraper._repair_dates_from_event_schema(self.schema(**overrides), "New Year’s Bash", *invalid), invalid)

    def test_real_extraction_uses_published_cross_year_dates(self):
        html = '<div class="event-list"><h3 class="_heading">New Year’s Bash</h3><div class="_value">Dec 28 - Jan 03 2027</div></div>' + str(self.schema())
        driver = MagicMock(page_source=html)
        driver.find_element.return_value.text = "8U-14U"
        with patch.object(module, "WebDriverWait", return_value=MagicMock()), patch.object(self.scraper, "_extract_competition_levels_and_gg", return_value=([], "")):
            row = self.scraper.extract_event_details("https://inbaseball.usssa.com/event/new-years-bash/", driver)
        self.assertEqual((row["Start-date"], row["End-date"]), ("12/28/2026", "01/03/2027"))
        self.assertTrue(validate_event_data(dict(row))[0])

    def metadata(self):
        return [{"11": [{"value": "31", "name": "2027 Season"}, {"value": "30", "name": "2026 Season"}]}, {"states": [{"value": 704, "name": "Montana"}]}]

    def test_redirected_empty_checks_all_seasons_with_region_and_no_end_cap(self):
        driver = MagicMock(current_url="https://www.usssa.com/baseball/eventSearch/")
        with patch.object(self.scraper, "_national_api", side_effect=self.metadata() + [{"results": []}, {"results": []}]) as api:
            self.assertEqual(self.scraper._verify_redirected_empty(driver, "https://mtbaseball.usssa.com/events/"), 0)
        calls = api.call_args_list[2:]
        self.assertEqual([c.args[2]["seasonID"] for c in calls], ["31", "30"])
        for call in calls:
            self.assertEqual(call.args[2]["regionID"], "704")
            self.assertEqual(call.args[2]["stateID"], "")
            self.assertEqual(call.args[2]["endDate"], "")
        driver.get.assert_called_once_with("https://www.usssa.com/baseball/eventSearch/")

    def test_redirect_nonempty_or_malformed_source_remains_failure(self):
        driver = MagicMock(current_url="https://www.usssa.com/baseball/eventSearch/")
        for payload in [{"results": [{"ID": 123}]}, {}, {"results": None}]:
            with self.subTest(payload=payload), patch.object(self.scraper, "_national_api", side_effect=self.metadata() + [payload]), self.assertRaises(ValueError):
                self.scraper._verify_redirected_empty(driver, "https://mtbaseball.usssa.com/events/")

    def test_only_recognized_redirect_uses_national_empty_verification(self):
        driver = MagicMock(current_url="https://tournamentgear.com/")
        with patch.object(self.scraper, "_verify_redirected_empty", return_value=0) as verify, patch.object(self.scraper, "_wait_for_listing_state", side_effect=AssertionError("must not wait for absent listing")):
            links = self.scraper.collect_event_links(driver, "https://mtbaseball.usssa.com/events/", pause=0)
        self.assertEqual(list(links), [])
        self.assertTrue(links.complete)
        verify.assert_called_once_with(driver, "https://mtbaseball.usssa.com/events/")


if __name__ == "__main__":
    unittest.main()
