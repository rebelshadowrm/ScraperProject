import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from provider_replay import PG_DETAIL
from scrapers.pg_scraper import PGScraper


class PerfectGameContactRecovery(unittest.TestCase):
    def setUp(self):
        self.scraper = PGScraper()
        self.addCleanup(self.scraper.close)
        self.detail = BeautifulSoup(PG_DETAIL, "html.parser")

    def test_valid_loaded_contact_remains_identical_without_extra_request(self):
        expected = self.scraper._parse_contact_soup(self.detail)
        with patch.object(self.scraper, "_get_soup", return_value=self.detail), patch.object(self.scraper, "_get_soup_requests") as http:
            self.assertEqual(self.scraper._parse_contact_info(None, "123"), expected)
        http.assert_not_called()

    def test_unrecognized_browser_body_recovers_published_fields(self):
        challenge = BeautifulSoup('<h1>Just a moment</h1>', "html.parser")
        with patch.object(self.scraper, "_get_soup", return_value=challenge), patch.object(self.scraper, "_get_soup_requests", return_value=self.detail) as http:
            self.assertEqual(self.scraper._parse_contact_info(None, "123"), self.scraper._parse_contact_soup(self.detail))
        http.assert_called_once_with('https://www.perfectgame.org/Events/Default.aspx?event=123', timeout=(10, 30))

    def test_failed_detail_sources_do_not_silently_return_empty_contacts(self):
        with patch.object(self.scraper, "_get_soup", side_effect=RuntimeError("fixture navigation failed")), patch.object(self.scraper, "_get_soup_requests", return_value=None), self.assertRaisesRegex(ValueError, "Unable to verify"):
            self.scraper._parse_contact_info(None, "123")

    def test_verified_unpublished_contacts_remain_blank(self):
        self.detail.select_one('#pnlContactPG').decompose()
        with patch.object(self.scraper, "_get_soup", return_value=self.detail), patch.object(self.scraper, "_get_soup_requests", return_value=self.detail):
            contact = self.scraper._parse_contact_info(None, "123")
        self.assertEqual((contact['contact_name'], contact['email'], contact['phone']), ('', '', ''))
        self.assertEqual(contact['guaranteed_games'], '3')

    def test_timed_out_navigation_with_blocked_http_retries_verified_browser_once(self):
        with patch.object(self.scraper, '_get_soup', side_effect=[RuntimeError('navigation timeout'), self.detail]) as browser, patch.object(self.scraper, '_get_soup_requests', return_value=None) as http:
            contact = self.scraper._parse_contact_info(object(), '123')
        self.assertEqual(contact, self.scraper._parse_contact_soup(self.detail))
        self.assertEqual(browser.call_count, 2)
        self.assertEqual(http.call_count, 1)

    def test_browser_retry_remains_bounded_and_rejects_unverified_bodies(self):
        challenge = BeautifulSoup('<h1>Just a moment</h1>', 'html.parser')
        with patch.object(self.scraper, '_get_soup', return_value=challenge) as browser, patch.object(self.scraper, '_get_soup_requests', return_value=None), self.assertRaisesRegex(ValueError, 'Unable to verify'):
            self.scraper._parse_contact_info(object(), '123')
        self.assertEqual(browser.call_count, 2)


if __name__ == '__main__':
    unittest.main()
