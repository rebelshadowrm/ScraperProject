import base64
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException

from scrapers.pg_scraper import PGScraper


def saved_url(state='AZ'):
    encode = lambda text: base64.b64encode(text.encode()).decode()
    return 'https://www.perfectgame.org/Schedule/?' + urlencode({
        'st': encode(state), 'ty': encode('3,6,7,12,13,14'),
        'sp': encode('1/13/2026 12:00:00 AM'), 'ep': encode('1/13/2027 12:00:00 AM')})


def response(ids, page=1, following=None, total=None, pages=1):
    return MagicMock(json=lambda: {'data': [{'eventGroupId': value} for value in ids],
        'currentPage': page, 'nextPage': following, 'totalItems': len(ids) if total is None else total,
        'totalPages': pages})


class FutureDiscoveryTests(unittest.TestCase):
    def test_recognized_search_redirect_uses_original_filters_and_verified_api_completion(self):
        scraper, driver = PGScraper(), MagicMock()
        driver.current_url = 'https://search.perfectgame.org/?endDate=2026-12-31'
        with patch('scrapers.pg_scraper.WebDriverWait') as wait, patch.object(scraper._http, 'get', return_value=response([26499])) as get:
            result = scraper.collect_event_links(driver, saved_url('AL'))
        self.assertTrue(result.complete, result.error)
        self.assertEqual(get.call_args.kwargs['params']['states[0]'], 'AL')
        self.assertNotIn('dateTo', get.call_args.kwargs['params'])
        wait.assert_not_called()

    def test_deferred_search_redirect_uses_api_but_real_timeout_still_fails(self):
        for target, expected in [('https://search.perfectgame.org/', True), ('https://www.perfectgame.org/Schedule/', False)]:
            scraper, driver = PGScraper(), MagicMock()
            driver.current_url = saved_url('AL')
            def timeout(*args):
                driver.current_url = target
                raise TimeoutException('fixture delayed navigation')
            wait = MagicMock()
            wait.until.side_effect = timeout
            with self.subTest(target=target), patch('scrapers.pg_scraper.WebDriverWait', return_value=wait), patch.object(scraper._http, 'get', return_value=response([26499])) as get:
                links = scraper.collect_event_links(driver, saved_url('AL'))
            self.assertEqual(links.complete, expected, links.error)
            self.assertEqual([url.split('gid=')[1] for url in links], ['26499'])
            self.assertEqual(get.call_args.kwargs['params']['states[0]'], 'AL')
            self.assertNotIn('dateTo', get.call_args.kwargs['params'])
            scraper.close()

    def test_pagination_redirect_keeps_legacy_links_and_requires_complete_api(self):
        for api in [response([26499]), RuntimeError('fixture API unavailable')]:
            scraper, driver = PGScraper(), MagicMock()
            driver.current_url = saved_url('AL')
            driver.page_source = '<table class="rgMasterTable"><tr class="rgGroupHeader"><td><a href="GroupedEvents.aspx?gid=23100">Existing</a></td></tr></table>'
            next_button = MagicMock()
            next_button.get_attribute.side_effect = lambda key: ''
            driver.find_elements.return_value = [next_button]
            def click(*args):
                driver.current_url = 'https://search.perfectgame.org/'
            driver.execute_script.side_effect = click
            wait = MagicMock()
            wait.until.side_effect = [MagicMock(), TimeoutException('fixture migration')]
            with self.subTest(api=type(api).__name__), patch('scrapers.pg_scraper.WebDriverWait', return_value=wait), patch.object(scraper._http, 'get', side_effect=[api]):
                links = scraper.collect_event_links(driver, saved_url('AL'), pause=0)
            self.assertEqual(links.complete, not isinstance(api, Exception), links.error)
            self.assertEqual(links[0].split('gid=')[1], '23100')
            if not isinstance(api, Exception):
                self.assertEqual(links[1].split('gid=')[1], '26499')
            scraper.close()

    def test_deferred_redirect_without_saved_state_cannot_be_verified_empty(self):
        scraper, driver = PGScraper(), MagicMock()
        driver.current_url = 'https://www.perfectgame.org/Schedule/'
        def timeout(*args):
            driver.current_url = 'https://search.perfectgame.org/'
            raise TimeoutException('fixture migration without state')
        wait = MagicMock()
        wait.until.side_effect = timeout
        with patch('scrapers.pg_scraper.WebDriverWait', return_value=wait), patch.object(scraper._http, 'get') as get:
            links = scraper.collect_event_links(driver, 'https://www.perfectgame.org/Schedule/')
        self.assertFalse(links.complete)
        self.assertEqual(list(links), [])
        get.assert_not_called()
        scraper.close()

    def test_only_groups_with_all_explicit_past_child_starts_are_skipped(self):
        for dates, expected in ((['1/1/2000 12:00:00 AM'], True),
                                (['1/1/2000 12:00:00 AM', '1/1/2099 12:00:00 AM'], False),
                                (['Unknown'], False), ([''], False), ([], False)):
            html = '<table><tr class="rgGroupHeader"><td>Group</td></tr>'
            for date in dates:
                html += f'<tr><td><a href="../Events/Default.aspx?event=1">Event</a><input id="row_hfStartDate" value="{date}"></td></tr>'
            html += '<tr class="rgGroupHeader"><td>Next group</td></tr></table>'
            with self.subTest(dates=dates):
                header = BeautifulSoup(html, 'html.parser').select_one('tr.rgGroupHeader')
                self.assertEqual(PGScraper._group_listing_is_non_future(header), expected)

    def test_supplement_has_no_end_date_and_keeps_saved_state_and_active_types(self):
        scraper = PGScraper()
        with patch.object(scraper._http, 'get', return_value=response([23100, 26499])) as get:
            result = scraper._collect_future_api_links(saved_url())
        self.assertTrue(result.complete, result.error)
        self.assertEqual([url.split('gid=')[1] for url in result], ['23100', '26499'])
        params = get.call_args.kwargs['params']
        self.assertNotIn('dateTo', params)
        self.assertEqual(params['states[0]'], 'AZ')
        self.assertEqual({v for k, v in params.items() if k.startswith('eventTypes[')},
                         {'PGBA Affiliated', 'WWBA Tournament', 'BCS Tournament', 'Super25', 'PG Youth Tournament'})
        self.assertEqual(get.call_args.kwargs['timeout'], (10, 30))

    def test_legacy_links_are_preserved_in_order_with_supplemental_links_added(self):
        scraper, driver = PGScraper(), MagicMock()
        driver.page_source = '<table class="rgMasterTable"><tr class="rgGroupHeader"><td><a href="GroupedEvents.aspx?gid=23100">Existing</a></td></tr></table>'
        driver.find_elements.return_value = []
        with patch('scrapers.pg_scraper.WebDriverWait'), patch.object(scraper._http, 'get', return_value=response([23100, 26499])):
            result = scraper.collect_event_links(driver, saved_url(), pause=0)
        self.assertTrue(result.complete, result.error)
        self.assertEqual([url.split('gid=')[1] for url in result], ['23100', '26499'])

    def test_multiple_pages_and_verified_empty(self):
        for responses, expected in (([response([1], following=2, total=2, pages=2), response([2], page=2, total=2, pages=2)], 2),
                                    ([response([], pages=0)], 0)):
            scraper = PGScraper()
            with self.subTest(expected=expected), patch.object(scraper._http, 'get', side_effect=responses):
                result = scraper._collect_future_api_links(saved_url())
            self.assertTrue(result.complete, result.error)
            self.assertEqual(len(result), expected)

    def test_truncated_repeated_and_failed_pagination_retain_first_page(self):
        for last in (response([], page=2, total=2, pages=2), response([1], page=2, total=2, pages=2), RuntimeError('Unavailable')):
            scraper = PGScraper()
            with self.subTest(last=last), patch.object(scraper._http, 'get', side_effect=[response([1], following=2, total=2, pages=2), last]):
                result = scraper._collect_future_api_links(saved_url())
            self.assertFalse(result.complete)
            self.assertEqual(len(result), 1)

    def test_invalid_state_is_failure_before_fetch(self):
        scraper = PGScraper()
        with patch.object(scraper._http, 'get') as get:
            result = scraper._collect_future_api_links(saved_url('BAD'))
        self.assertFalse(result.complete)
        get.assert_not_called()


    def test_overlapping_page_boundaries_reconcile_all_distinct_groups(self):
        scraper = PGScraper()
        self.addCleanup(scraper.close)
        responses = [response([1, 2], following=2, total=4, pages=2), response([2, 3], page=2, total=4, pages=2),
                     response([1, 4], following=2, total=4, pages=2), response([2, 3], page=2, total=4, pages=2)]
        with patch.object(scraper._http, 'get', side_effect=responses):
            result = scraper._collect_future_api_links(saved_url())
        self.assertTrue(result.complete, result.error)
        self.assertEqual([url.split('gid=')[1] for url in result], ['1', '2', '3', '4'])

    def test_unreconciled_distinct_group_gap_remains_partial(self):
        scraper = PGScraper()
        self.addCleanup(scraper.close)
        pages = [response([1, 2], following=2, total=4, pages=2), response([2, 3], page=2, total=4, pages=2)]
        with patch.object(scraper._http, 'get', side_effect=pages * 3):
            result = scraper._collect_future_api_links(saved_url())
        self.assertFalse(result.complete)
        self.assertIn('reconciliation', result.error)
        self.assertEqual([url.split('gid=')[1] for url in result], ['1', '2', '3'])
