import unittest
from unittest.mock import MagicMock, patch
from datetime import date
from bs4 import BeautifulSoup
from scrapers.game7baseball_scraper import Game7BaseballScraper, _parse_date_range as game7_dates
from scrapers.ncs_scraper import NCSScraper
from scrapers.pg_scraper import PGScraper
from scrapers.grandslam_scraper import GrandSlamScraper, _parse_date_range as grandslam_dates, _extract_date_raw_from_details_soup


class LiveDateTests(unittest.TestCase):
    def test_published_dates_have_no_2099_cutoff(self):
        for parse in (game7_dates, grandslam_dates):
            self.assertEqual(parse('Jul 20-22, 2100', 2026), (date(2100, 7, 20), date(2100, 7, 22)))
        soup = BeautifulSoup('<h1 class="mv-sm">Future Classic</h1><hgroup><h3 class="inline-block m0">Jul 20-22, 2100</h3></hgroup>', 'html.parser')
        self.assertEqual(NCSScraper()._parse_details_page(soup)[1:], ('Jul 20 2100', 'Jul 22 2100'))
        scraper = PGScraper()
        self.assertEqual(scraper._parse_date_range('Jul 20-22, 2100', ''), ('Jul 20 2100', 'Jul 22 2100'))
        self.assertEqual(scraper._parse_date_range('Nov 4 - Jan 6', '1/6/2101 12:00:00 AM'), ('Nov 4 2100', 'Jan 6 2101'))

    def test_explicit_tbd_is_a_retryable_source_exclusion_without_fabricated_dates(self):
        from common.source_exclusions import SourceEventIncomplete
        soup = BeautifulSoup('<a id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventNameNew">2025 7U Kentaro New Event Create</a><span id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblDatesNew">DATE TBD</span>', 'html.parser')
        with self.assertRaises(SourceEventIncomplete), patch.object(PGScraper, '_get_soup', return_value=soup):
            PGScraper().extract_event_details('https://www.perfectgame.org/Events/Default.aspx?event=150423', driver=None)

    def test_explicit_year_is_authoritative_for_published_next_season(self):
        for parse in (game7_dates, grandslam_dates):
            for text in ("Jun 11-13, 2027", "Jun 11–13, 2027", "Jun 11-13 2027"):
                with self.subTest(parser=parse.__module__, text=text):
                    self.assertEqual(parse(text, 2026), (date(2027, 6, 11), date(2027, 6, 13)))
            self.assertEqual(parse("May 9, 2027", 2026), (date(2027, 5, 9), date(2027, 5, 9)))
            self.assertEqual(parse("Feb 28 - Mar 1, 2027", 2026), (date(2027, 2, 28), date(2027, 3, 1)))
            self.assertEqual(parse("Jun 11-13", 2026), (date(2026, 6, 11), date(2026, 6, 13)))

    def test_live_game7_header_parses_2027_start_and_end(self):
        soup = BeautifulSoup('<hgroup><h1>WORLD SERIES 2027</h1><h3>Springfield, MO</h3><h3>Killian Sports Complex</h3><h3>Jun 11-13, 2027</h3></hgroup>', 'html.parser')
        result = Game7BaseballScraper()._parse_details_page(soup, "MO")
        self.assertEqual(result["Start-date"], "06/11/2027")
        self.assertEqual(result["End-date"], "06/13/2027")

    def test_grandslam_verified_empty_server_feed_is_complete(self):
        for feed, complete in (("[]", True), ('[{id: 123}]', False)):
            html = f'<form id="formEventSearch"></form><div class="media-list media-list-events"></div><script>var events = {feed};</script>'
            with self.subTest(feed=feed), patch('scrapers.grandslam_scraper._soup', return_value=BeautifulSoup(html, 'html.parser')):
                result = GrandSlamScraper().collect_event_links(None, 'https://example.com/?state=AK')
            self.assertEqual(result.complete, complete)

    def test_grandslam_date_header_keeps_year_comma_and_full_month(self):
        soup = BeautifulSoup('<hgroup><h3>Panama City Beach, FL</h3><h3>June 11-13, 2027</h3></hgroup>', 'html.parser')
        self.assertEqual(_extract_date_raw_from_details_soup(soup), 'June 11-13, 2027')

    def test_explicit_cross_year_range_preserves_both_years(self):
        text = 'Dec 1, 2026 - Feb 24, 2027'
        soup = BeautifulSoup(f'<h1 class="mv-sm">Tucson Winter League</h1><hgroup><h3 class="inline-block m0">{text}</h3></hgroup>', 'html.parser')
        self.assertEqual(NCSScraper()._parse_details_page(soup)[1:], ('12/01/2026', '02/24/2027'))
        for parse in (game7_dates, grandslam_dates):
            self.assertEqual(parse(text, 2026), (date(2026, 12, 1), date(2027, 2, 24)))
        self.assertEqual(PGScraper()._parse_date_range(text, text), ('12/01/2026', '02/24/2027'))

    def test_perfectgame_explicit_year_not_duplicated(self):
        scraper = PGScraper()
        self.assertEqual(scraper._parse_date_range('Jun 11-13, 2027', 'Jun 11-13, 2027'), ('Jun 11 2027', 'Jun 13 2027'))
        self.assertEqual(scraper._parse_date_range('May 9, 2027', 'May 9, 2027'), ('May 9 2027', 'May 9 2027'))

    def test_perfectgame_winter_range_uses_published_endpoint_year(self):
        scraper = PGScraper()
        self.assertEqual(scraper._parse_date_range('Nov 4 - Feb 4', '2/4/2026 12:00:00 AM'), ('Nov 4 2025', 'Feb 4 2026'))
        self.assertEqual(scraper._parse_date_range('Jul 20-22', '7/22/2027 12:00:00 AM'), ('Jul 20 2027', 'Jul 22 2027'))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            scraper._parse_date_range('Jul 20-22', '7/23/2027 12:00:00 AM')

    def test_perfectgame_single_event_uses_published_title_year(self):
        soup = BeautifulSoup('<a id="ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventNameNew">2027 10U Spring Classic (AA)</a><span id="ContentTopLevel_ContentPlaceHolder1_lblEventDateNew">May 9-10</span>', 'html.parser')
        row = PGScraper()._parse_single_event_page(None, 'https://example.com/event', soup=soup)
        self.assertEqual((row['Start-date'], row['End-date']), ('May 9 2027', 'May 10 2027'))

    def test_perfectgame_card_sibling_endpoint_anchors_winter_year(self):
        soup = BeautifulSoup('''<div class="row">
          <input id="prefix_repSchedule_hlEndDate_0" value="1/6/2027 12:00:00 AM">
          <a href="/Events/Default.aspx?event=171989"><div class="pgds-EventCard">
          <span id="prefix_repSchedule_lblEventDate_0">Nov 4 - Jan 6</span>
          <span id="prefix_repSchedule_lblEventName_0">2026 8U PG Fall League (Open)</span>
          <span id="prefix_repSchedule_lblDivision_0">8U (Open)</span>
          </div></a></div>''', 'html.parser')
        event = PGScraper()._parse_grouped_page(None, soup)[0]
        self.assertEqual((event['start_date'], event['end_date']), ('Nov 4 2026', 'Jan 6 2027'))

    def test_perfectgame_group_http_failure_uses_one_bounded_browser_load(self):
        scraper, driver = PGScraper(), MagicMock()
        driver.page_source = '<body><h1>Real event</h1></body>'
        url = 'https://www.perfectgame.org/Schedule/GroupedEvents.aspx?gid=1'
        with patch.object(scraper, '_get_soup_requests', return_value=None), patch('scrapers.pg_scraper.WebDriverWait'):
            soup = scraper._get_soup(driver, url)
        self.assertEqual(soup.h1.text, 'Real event')
        driver.get.assert_called_once_with(url)
        driver.set_page_load_timeout.assert_called_once_with(55)
        self.assertTrue(scraper._last_soup_used_browser)

    def test_perfectgame_successful_group_http_fetch_keeps_browser_unused(self):
        scraper, driver = PGScraper(), MagicMock()
        expected = BeautifulSoup('<body>Group</body>', 'html.parser')
        with patch.object(scraper, '_get_soup_requests', return_value=expected):
            self.assertIs(scraper._get_soup(driver, 'https://example.com/GroupedEvents.aspx?gid=1'), expected)
        driver.get.assert_not_called()
        self.assertFalse(scraper._last_soup_used_browser)
