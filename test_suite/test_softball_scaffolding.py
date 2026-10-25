import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from common.identity import event_identity
from common.prompt_utils import get_scraper_classes
from common.softball_refresh import needs_softball_details
from common.softball_registry import softball_registry
from common.softball_scaffold import SoftballNotReady, SoftballScaffoldScraper, fetch_listing
from scripts.softball_discovery_smoke import run

FIXTURES = Path(__file__).parent / 'fixtures'


class SoftballScaffoldingTests(unittest.TestCase):
    def test_complete_registered_scope_is_excluded_from_baseball_defaults(self):
        registry = softball_registry()
        self.assertEqual(len(registry), 13)
        all_classes = get_scraper_classes(include_disabled=True)
        defaults = get_scraper_classes()
        for cls in registry.values():
            self.assertIn(cls, all_classes)
            self.assertNotIn(cls, defaults)
            self.assertEqual(cls.SPORT, 'softball')
        self.assertNotEqual(registry['topgun_sports'], registry['topgun_events'])

    def test_observed_routes_dedupe_by_id_and_report_partial(self):
        html = (FIXTURES / 'softball_discovery.html').read_text()
        expected = dict(usfa='USFA46019', fasa='15448', topgun_events='123',
                        triplecrown_fastpitch='https://www.triplecrownfastpitch.com/events/test-Case')
        for key, identity in expected.items():
            scraper = softball_registry()[key]()
            links = scraper.parse_listing(html, scraper.DISCOVERY_URL)
            self.assertEqual(len(links), 1)
            self.assertFalse(links.complete)
            self.assertEqual(scraper.get_event_identity(links[0]), identity)
            self.assertIn('|softball|', event_identity(scraper, links[0]))
            with self.assertRaises(SoftballNotReady):
                scraper.parse_listing('<title>Access denied</title>', scraper.DISCOVERY_URL)

    def test_unknown_discovery_fails_before_network(self):
        with patch('requests.Session') as session:
            for cls in [SoftballScaffoldScraper,*softball_registry().values()]:
                if issubclass(cls, SoftballScaffoldScraper) and not cls.DISCOVERY_SELECTOR:
                    scraper = cls()
                    with self.assertRaises(SoftballNotReady):
                        scraper.collect_event_links(None, scraper.DISCOVERY_URL)
            session.assert_not_called()

    def test_json_feed_uses_published_key_and_rejects_unknown_sport_schema(self):
        scraper = softball_registry()['usa_fastpitch']()
        raw = (FIXTURES / 'softball_active.json').read_text()
        links = scraper.parse_listing(raw, scraper.DISCOVERY_URL)
        self.assertEqual(len(links), 2)  # same name/dates/location, distinct IDs
        self.assertNotEqual(event_identity(scraper, links[0]), event_identity(scraper, links[1]))
        self.assertEqual(len(scraper.parse_listing(raw, scraper.DISCOVERY_URL, max_events=1)), 1)
        for invalid in ('{}', 'null', '[null]', '[{"_key":"123","sport":"BB"}]', '[{"sport":"FP"}]'):
            with self.assertRaises(ValueError):
                scraper.parse_listing(invalid, scraper.DISCOVERY_URL)

    def test_identity_rejects_missing_duplicate_and_foreign_ids(self):
        scraper = softball_registry()['fasa']()
        for url in ('https://www.playfasa.com/profile.asp',
                    'https://www.playfasa.com/profile.asp?ID=1&ID=2',
                    'https://foreign.example/profile.asp?ID=1',
                    'https://www.playfasa.com/teamprofile.asp?ID=1'):
            with self.assertRaises(ValueError):
                scraper.get_event_identity(url)
        pg = softball_registry()['pg_softball']()
        self.assertEqual(pg.get_event_identity('https://www.perfectgame.org/Events/Default.aspx?event=120624'), '120624')

    def test_smoke_is_offline_bounded_and_never_calls_details(self):
        with patch('requests.Session') as session, patch.object(SoftballScaffoldScraper, 'extract_event_details') as detail:
            result = run('fasa', fixture=FIXTURES / 'softball_discovery.html', max_events=1)
            self.assertEqual(result['status'], 'partial')
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(result['pages'], 1)
            self.assertFalse(result['complete'])
            session.assert_not_called()
            detail.assert_not_called()
        for options in ({}, {'fixture': 'unused', 'live': True}, {'live': True, 'max_events': 26}):
            with self.assertRaises(ValueError):
                run('fasa', **options)

    def test_http_redirects_denials_and_size_limits_are_not_empty_success(self):
        response = Mock(status_code=200, headers={'Content-Type':'text/html'}, encoding='utf-8')
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        session = Mock()
        session.get.return_value = response
        response.iter_content.return_value = [b'<html>listing</html>']
        self.assertIn('listing', fetch_listing('https://example.org', session=session))
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])
        response.status_code = 302
        with self.assertRaises(ValueError):
            fetch_listing('https://example.org', session=session)
        response.status_code = 200
        response.iter_content.return_value = [b'x' * 4_000_001]
        with self.assertRaises(ValueError):
            fetch_listing('https://example.org', session=session)

    def test_new_changed_and_due_details_ignore_delivery_flags(self):
        now = datetime(2099, 1, 10, tzinfo=timezone.utc)
        scraper = softball_registry()['fasa']()
        entry = dict(acquisition_status='ready', acquisition_refreshed_at=now.isoformat(),
                     observed_discovery_revision='a', acquired_discovery_revision='a',
                     processed=False, export_status='not_applicable')
        self.assertFalse(needs_softball_details(entry, scraper, now=now))
        for change in (dict(acquisition_status='pending'), dict(observed_discovery_revision='b'),
                       dict(acquisition_refreshed_at=(now-timedelta(hours=24)).isoformat()),
                       dict(acquisition_refreshed_at=''), dict(last_attempt_status='failed')):
            self.assertTrue(needs_softball_details(dict(entry, **change), scraper, now=now))
        self.assertFalse(needs_softball_details(dict(entry, processed=True, export_status='exported'), scraper, now=now))

    def test_fasa_change_marker_excludes_visit_counters(self):
        scraper = softball_registry()['fasa']()
        html = (FIXTURES / 'softball_discovery.html').read_text()
        url = scraper.parse_listing(html, scraper.DISCOVERY_URL)[0]
        revision = scraper.get_discovery_revision(url)
        scraper.parse_listing(html.replace('747', '999'), scraper.DISCOVERY_URL)
        self.assertEqual(revision, scraper.get_discovery_revision(url))
        scraper.parse_listing(html.replace('$200 NO PAP', '$250 NO PAP'), scraper.DISCOVERY_URL)
        self.assertNotEqual(revision, scraper.get_discovery_revision(url))
        scraper.parse_listing(html.replace('2099', '2100'), scraper.DISCOVERY_URL)
        self.assertNotEqual(revision, scraper.get_discovery_revision(url))
        self.assertEqual(scraper.get_event_identity(url), '15448')
        tc = softball_registry()['triplecrown_fastpitch']()
        self.assertNotEqual(tc.get_event_identity('https://www.triplecrownfastpitch.com/events/test-Case'),
                            tc.get_event_identity('https://www.triplecrownfastpitch.com/events/test-case'))

    def test_shared_runner_refreshes_changed_and_due_softball_without_excel(self):
        from common.config import OUTPUT_COLUMNS
        from common.runner import process_location
        scraper = softball_registry()['fasa']()
        scraper.BATCH_DELAY_SECONDS = 0
        url = 'https://www.playfasa.com/profile.asp?ID=15448'
        row = {'Tournament-name':'Fixture', 'Start-date':'10/20/2099', 'End-date':'10/21/2099',
               'State-FullName':'Texas', 'Website-url':url}
        events = {'events':{}}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(scraper, 'collect_event_links', return_value=[url]), \
                patch.object(scraper, 'extract_event_details', return_value=row) as detail:
            root = Path(temporary)
            def acquire():
                process_location(location_name='Texas', base_url=scraper.DISCOVERY_URL, scraper=scraper,
                    driver=None, events_data=events, failed_data={'failed_events':[]}, output_columns=OUTPUT_COLUMNS,
                    json_file=str(root/'events.json'), failed_events_file=str(root/'failed.json'),
                    download_dir='', shutdown_check=lambda:False, output_base_folder=str(root/'Sheets'),
                    scheduler_history_file=str(root/'history.json'))
            scraper.discovery_revisions[url] = 'first'
            acquire()
            entry = events['events'][event_identity(scraper, url)]
            self.assertEqual(entry['acquired_discovery_revision'], 'first')
            acquire()
            self.assertEqual(detail.call_count, 1)
            scraper.discovery_revisions[url] = 'changed'
            acquire()
            self.assertEqual(detail.call_count, 2)
            self.assertEqual(entry['acquired_discovery_revision'], 'changed')
            entry['acquisition_refreshed_at'] = '2000-01-01T00:00:00+00:00'
            acquire()
            self.assertEqual(detail.call_count, 3)
            self.assertEqual(entry['export_status'], 'not_applicable')
            self.assertFalse(entry['processed'])
            self.assertFalse(list(root.rglob('*.xlsx')))


if __name__ == '__main__':
    unittest.main()
