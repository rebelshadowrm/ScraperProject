import unittest
import tempfile
from pathlib import Path
from common.age_groups import age_tokens
from common.normalize import normalize_age_group, normalize_row
from common.softball_rows import make_row
from common.delivery import DeliveryStore
from test_suite.test_independent_delivery import entry


class ExplicitEligibilityTests(unittest.TestCase):
    def test_usssa_hs_only_source_reaches_normalized_outbox_with_fee_and_contact(self):
        import json
        from datetime import date
        from functools import partial
        from unittest.mock import patch
        from common.validation import is_future_event
        from scrapers.usssa_fastpitch_scraper import USSSAFastpitchScraper

        samples = json.loads((Path(__file__).parent / 'fixtures/softball_detail_samples.json').read_text(encoding='utf-8'))
        response = json.loads(samples['usssa_divisions'])
        response['result'] = '<tr><td>High School B</td><td>$425</td></tr>'
        url = 'https://txfastpitch.usssa.com/event/c-classhalloween-spook-outmvp/'
        scraper = USSSAFastpitchScraper()
        self.addCleanup(scraper.close)
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline replay attempted network')), \
                patch('scrapers.usssa_fastpitch_scraper.fetch_listing', side_effect=[samples['usssa_detail'], json.dumps(response)]):
            row = scraper.extract_event_details(url)
        self.assertEqual(row['Age-groups.1'], 'HS')
        self.assertEqual(row['Entry-fee.1'], '425')
        self.assertTrue(row['Contact-name'])
        row = normalize_row(row)
        with tempfile.TemporaryDirectory() as temp:
            store = DeliveryStore(Path(temp) / 'outbox.sqlite3')
            try:
                fixture = entry('softball', provider='USSSAFastpitchScraper')
                fixture.update(event_data=row, source_details=scraper.source_details[url], sport_verified=True)
                with patch('common.delivery.is_future_event', partial(is_future_event, today=date(2026, 10, 1))):
                    result = store.ingest({'events': {'USSSAFastpitchScraper|softball|42': fixture}})
                self.assertEqual(result['staged'], 1)
                body = json.loads(store.db.execute('select payload from events').fetchone()[0])
                self.assertEqual(body['fields']['Age-groups.1'], 'HS')
                self.assertEqual(body['fields']['Contact-name'], row['Contact-name'])
                division = body['source_details']['divisions'][0]
                self.assertEqual(division['label'], 'High School B')
                self.assertEqual(division['competition_class'], 'B')
                self.assertEqual(division['entry_fee'], '$425')
                self.assertNotIn('18u', division.get('age_groups', []))
            finally:
                store.close()

    def test_hs_and_numeric_divisions_are_distinct(self):
        for label in ['HS', 'hs', 'High School']:
            self.assertEqual(normalize_age_group(label), 'HS')
            self.assertEqual(age_tokens(label), ['HS'])
        self.assertEqual(age_tokens('18U'), ['18u'])
        self.assertEqual(age_tokens('11/13U A'), ['11u', '13u'])
        self.assertEqual(age_tokens('2027 graduates'), [])
        self.assertEqual(age_tokens('HS / 18U'), ['18u', 'HS'])

    def test_multi_age_and_hs_rich_metadata_survive_single_cell_normalization(self):
        row = normalize_row({'Age-groups.1': '11/13U', 'Entry-fee.1': '300', 'Age-groups.2': 'HS', 'Entry-fee.2': '450'})
        self.assertEqual(row['Age-groups.2'], 'HS')
        self.assertEqual(row['source_details']['divisions'][0]['age_groups'], ['11u', '13u'])
        self.assertEqual(row['source_details']['divisions'][1]['entry_fee'], '450')

    def test_overflow_and_distinct_prices_reach_website_without_extra_excel_columns(self):
        divisions = [{'label': '12U B', 'entry_fee': str(200+i)} for i in range(15)] + [{'label': 'High School B', 'entry_fee': '425'}]
        row = make_row(title='Source Cup', start='2099-06-01', end='2099-06-02', url='https://example.org/event', state='IL', city='Chicago', divisions=divisions)
        self.assertNotIn('Age-groups.14', row)
        self.assertEqual(len(row['source_details']['divisions']), 16)
        with tempfile.TemporaryDirectory() as temp:
            store = DeliveryStore(Path(temp)/'outbox.sqlite3')
            try:
                fixture = entry('softball', provider='FixtureScraper')
                fixture['event_data'] = row
                fixture['sport_verified'] = True
                store.ingest({'events': {'FixtureScraper|softball|42': fixture}})
                body = store.db.execute('select payload from events').fetchone()[0]
                import json
                self.assertEqual(json.loads(body)['source_details']['divisions'][-1]['age_groups'], ['HS'])
                self.assertEqual(fixture['export_status'], 'exported')
            finally:
                store.close()


if __name__ == '__main__':
    unittest.main()
