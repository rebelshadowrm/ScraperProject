"""Captured USA descriptions and registration changes through durable delivery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from common.validation import is_future_event
from common.config import OUTPUT_COLUMNS
from common.delivery import DeliveryStore
from common.identity import event_identity
from common.normalize import normalize_row
from common.softball_description import description
from common.softball_registerplay_metadata import division_metadata
from common.softball_rows import age_tokens
from common.softball_national import discover_state
from common.softball_tournamentconnect import TC_LISTING
from scrapers.pgf_scraper import PGFScraper
from scripts.acquire_softball_national import acquire
from scrapers.triplecrown_fastpitch_scraper import TripleCrownFastpitchScraper
from common.source_exclusions import SourceEventOutOfScope
from scrapers.usa_softball_scraper import USASoftballScraper


FIXTURES = Path(__file__).parent / 'fixtures/softball_quality'
URL = 'https://usa.registerplay.com/pages/event/detail/7484010695'


class SoftballQualityTests(unittest.TestCase):
    def test_embedded_media_does_not_turn_valid_source_text_into_an_oversized_import(self):
        html='<p>First paragraph.</p><p>Second <strong>important</strong> paragraph.</p><img src="data:image/png;base64,'+'A'*300000+'" alt="Official flyer">'
        row,details=self.extract(html=html)
        self.assertTrue(details['description_media_omitted'])
        self.assertIn('First paragraph.\n\nSecond important paragraph.',details['description']['text'])
        self.assertNotIn('data:image',details['description']['html'])
        self.assertLess(len(json.dumps({'fields':row,'source_details':details}).encode()),256*1024)
    def setUp(self):
        guard = patch('requests.sessions.Session.request', side_effect=AssertionError('Offline replay attempted network'))
        guard.start()
        self.addCleanup(guard.stop)

    def extract(self, fixture='usa_pinkout_open.json', *, html=None):
        payload = json.loads((FIXTURES / fixture).read_text(encoding='utf-8'))
        if html is not None:
            payload['info']['eventInfo']['info'] = html
        scraper = USASoftballScraper()
        self.addCleanup(scraper.close)
        with patch('common.softball_api.fetch_listing', side_effect=[json.dumps(payload['header']), json.dumps(payload['info'])]) as fetch:
            row = normalize_row(scraper.extract_event_details(URL))
        self.assertEqual(fetch.call_count, 2)
        return row, scraper.source_details[URL]

    def test_complicated_real_descriptions_survive_row_normalization_and_json(self):
        examples = {
            '7695094890': ['Important Dates:', '14/16/18U', 'Check In- 7/20', '10/12U', 'MORE DETAILS COMING SOON!'],
            '6200780587': ['UMPIRE FEES / PAP (PER TEAM):', 'ROSTERS:', 'DUE 10-23-2026 @ NOON- NO LATE ENTRIES', 'TEAM CHECK-IN:']}
        for identity, expected in examples.items():
            html = (FIXTURES / ('usa_description_' + identity + '.html')).read_text(encoding='utf-8')
            row, details = self.extract(html=html)
            rich = details['description']
            for fragment in expected:
                self.assertIn(fragment, rich['text'])
                self.assertIn(fragment, row['More-information-and-additional-tournament-locations'])
            self.assertIn('\n\n', rich['text'])
            self.assertTrue('<strong>' in rich['html'] or '<h2>' in rich['html'])
            self.assertEqual(json.loads(json.dumps(details, ensure_ascii=False))['description'], rich)
            self.assertEqual(set(row), set(OUTPUT_COLUMNS))

    def test_safe_subset_emphasis_lists_links_and_plain_fallback(self):
        rich = description('<h1>Rules</h1><p>First <span style="font-weight:700;color:red">important</span><br>line.</p>'
            '<p>Second.</p><ol><li>Same</li><li>Same</li></ol><ul><li>Bring balls</li></ul>'
            '<a href="/register">Register</a><a href="javascript:alert(1)">bad</a>'
            '<img src="data:image/png;base64,secret" onerror="alert(1)"><script>alert(1)</script>'
            '<svg><script>bad</script></svg><iframe src="https://bad.example"></iframe>', base_url=URL)
        self.assertIn('<h2>Rules</h2>', rich['html'])
        self.assertIn('<strong>important</strong>', rich['html'])
        self.assertIn('important\nline.', rich['text'])
        self.assertIn('1. Same\n\n2. Same', rich['text'])
        self.assertIn('- Bring balls', rich['text'])
        self.assertIn('https://usa.registerplay.com/register', rich['html'])
        for removed in ['javascript:', 'onerror', 'secret', 'script', 'style=', '<img', '<svg', '<iframe']:
            self.assertNotIn(removed, rich['html'])
        self.assertEqual(description(rich['html']), rich)
        plain = description('First line\ncontinued\n\nSecond paragraph')
        self.assertEqual(plain['text'], 'First line\ncontinued\n\nSecond paragraph')
        self.assertIn('<br>', plain['html'])

    def test_open_class_paired_prices_and_yearless_deadline(self):
        row, details = self.extract()
        combined, _, _, youngest = details['divisions']
        self.assertEqual(combined['age_groups'], ['16u', '18u'])
        self.assertEqual(combined['competition_class'], 'Open')
        self.assertEqual(youngest['name'], '10O')
        self.assertEqual(youngest['pricing']['base_fee'], '100')
        self.assertEqual(youngest['pricing']['advertised_fee'], '104.33')
        self.assertEqual(youngest['pricing']['additional_card_fee'], '4.33')
        self.assertEqual(youngest['pricing']['observed_range'], ['100', '104.33'])
        self.assertEqual(youngest['registration']['deadline_label'], 'Oct 20')
        self.assertIsNone(youngest['registration']['deadline_iso'])
        self.assertEqual(youngest['registration']['spots_remaining'], 24)
        self.assertEqual(youngest['registration']['status'], 'open')
        self.assertEqual(row['Entry-fee.5'], '100')
        self.assertFalse(details['organizer_contact']['email_published'])
        self.assertEqual(row['Email'], '')
        self.assertEqual(details['facilities'][0]['address'], '1713 38th')
        self.assertEqual(details['organizer_contact']['form']['method'],'source_page_dialog')
        self.assertIn('McMahon%20Park%20Ball%20Fields',details['facilities'][0]['directions_url'])
        self.assertNotIn('divisionTeams', json.dumps(details))

    def test_ambiguous_surcharge_zero_fee_waitlist_and_zero_spots(self):
        item = {'name':'12B', 'divisionFee':'0', 'fee':'4.33',
                'spotsRemaining':{'showEnter':False, 'spotsRemaining':0}, 'enterByCutDate':'Nov 16'}
        fact = division_metadata(item, cc_fee_extra=None)
        self.assertEqual(fact['pricing']['base_fee'], '0')
        self.assertNotIn('additional_card_fee', fact['pricing'])
        self.assertEqual(fact['registration']['status'], 'unknown')
        self.assertEqual(fact['registration']['spots_remaining'], 0)
        item['spotsRemaining']['message'] = 'Join Waitlist'
        self.assertEqual(division_metadata(item,cc_fee_extra=True)['registration']['status'], 'waitlist')
        item.update(enterByCutDate='2099-11-16')
        self.assertEqual(division_metadata(item,cc_fee_extra=True)['registration']['deadline_iso'], '2099-11-16')

    def test_hpp_remains_program_and_is_excluded_from_team_tournaments(self):
        payload = json.loads((FIXTURES/'usa_hpp.json').read_text(encoding='utf-8'))
        scraper = USASoftballScraper()
        url = 'https://usa.registerplay.com/pages/event/detail/7992042197'
        with patch('common.softball_api.fetch_listing', side_effect=[json.dumps(payload['header']),json.dumps(payload['info'])]):
            with self.assertRaises(SourceEventOutOfScope):
                scraper.extract_event_details(url)
        details = scraper.source_details[url]
        self.assertEqual(details['program'], 'HPP')
        self.assertEqual(details['divisions'][0]['divisionType'], 'Individual Entry - HPP Local Identifier')
        self.assertEqual(details['divisions'][0]['registration']['deadline_label'], 'Mar 21')

    @patch('common.delivery.is_future_event', lambda row: is_future_event(row, today=date(2026, 10, 1)))
    def test_registration_update_survives_sqlite_reimport_without_reusing_receipt(self):
        row, details = self.extract()
        closed_row, closed = self.extract('usa_pinkout_closed.json')
        key = event_identity(USASoftballScraper(), URL)
        ledger = {'events':{key:{'scraper':'USASoftballScraper','sport':'softball',
            'sport_verified':True,'source_event_id':'7484010695','url':URL,
            'event_data':row,'source_details':details,'processed':True,'export_status':'exported'}}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'outbox.sqlite3'
            store = DeliveryStore(path)
            store.ingest(ledger)
            store.db.execute("UPDATE deliveries SET delivered_sequence=desired_sequence, delivered_revision=desired_revision,status='delivered'")
            store.db.commit()
            self.assertEqual(store.ingest(ledger)['unchanged'], 1)
            ledger['events'][key].update(event_data=closed_row,source_details=closed)
            self.assertEqual(store.ingest(ledger)['staged'], 1)
            delivery = store.db.execute('SELECT * FROM deliveries').fetchone()
            self.assertEqual(delivery['desired_sequence'], 2)
            self.assertEqual(delivery['delivered_sequence'], 1)
            self.assertEqual(delivery['status'], 'pending')
            self.assertEqual(len(store.history(key)), 2)
            store.close()
            store = DeliveryStore(path)
            payload = json.loads(store.db.execute('SELECT payload FROM events').fetchone()[0])
            self.assertEqual(payload['source_details']['divisions'][0]['registration']['status'], 'closed')
            self.assertEqual(payload['operation'], 'upsert')
            self.assertTrue(ledger['events'][key]['processed'])
            self.assertEqual(store.ingest(ledger)['unchanged'], 1)
            store.close()

    def test_observed_nsa_hyphenated_class_is_labelled_age_not_graduation_year(self):
        self.assertEqual(age_tokens('15/16u-B -  @ LaPorte'), ['15u','16u'])
        self.assertEqual(age_tokens('2027-B'), [])
        self.assertEqual(age_tokens('23u - Women'), [])

    def test_real_state_feed_includes_events_absent_from_default_hpp_view(self):
        source = (FIXTURES/'usa_state_tx.json').read_text(encoding='utf-8')
        scraper = USASoftballScraper()
        with patch('common.softball_national.fetch_listing',return_value=source):
            first = discover_state(scraper,'TX',today=date(2026,10,20))
            revisions = dict(scraper.discovery_revisions)
            repeated = discover_state(scraper,'TX',today=date(2026,10,20))
        self.assertEqual(first.source_total,38)
        self.assertGreater(len(first),2)
        self.assertEqual(first,repeated)
        self.assertEqual(revisions,scraper.discovery_revisions)
        self.assertNotIn('https://usa.registerplay.com/pages/event/detail/7992042197',first)
        counts = first.filter_counts
        self.assertEqual(counts['source_records'],counts['not_future']+counts['unresolved_date']
            +counts['unresolved_location']+counts['other_sport']+counts['selected_records'])

    def test_pgf_explicit_division_label_is_not_its_eligibility_range(self):
        payload = json.loads((FIXTURES/'pgf_5165.json').read_text(encoding='utf-8'))
        url = TC_LISTING+'#tc-event=5165'
        scraper = PGFScraper()
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(payload)):
            row = scraper.extract_event_details(url)
            repeated = scraper.extract_event_details(url)
        self.assertEqual(row,repeated)
        self.assertEqual([row[f'Age-groups.{i}'] for i in range(1,6)],['10u','12u','14u','16u','18u'])
        self.assertEqual(row['Age-groups.6'],'')
        self.assertEqual(row['Entry-fee.1'],'800')
        self.assertEqual(len(scraper.source_details[url]['divisions']),15)
        self.assertEqual(scraper.source_details[url]['divisions'][0]['minimum_age'],8)
        self.assertEqual(scraper.source_details[url]['divisions'][0]['competition_class'],'B Level')
        missing_upper = json.loads((FIXTURES/'pgf_5251.json').read_text(encoding='utf-8'))
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(missing_upper)):
            self.assertEqual(scraper.extract_event_details(TC_LISTING+'#tc-event=5251')['Age-groups.1'],'10u')
        from common.source_exclusions import SourceEventIncomplete
        for label in ['8U - 10U','2027','12U']:
            invalid = copy.deepcopy(payload)
            invalid['divisions'][0]['division'] = label
            with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(invalid)):
                with self.assertRaises(SourceEventIncomplete):
                    scraper.extract_event_details(url)

    def test_verified_metro_only_event_keeps_state_classes_lists_and_identity(self):
        payload = json.loads((FIXTURES/'usa_metro_dfw.json').read_text(encoding='utf-8'))
        url = 'https://usa.registerplay.com/pages/event/detail/5108764366'
        scraper = USASoftballScraper()
        with patch('common.softball_api.fetch_listing',side_effect=[json.dumps(payload['header']),json.dumps(payload['info'])]):
            row = scraper.extract_event_details(url)
        self.assertEqual(row['State-FullName'],'Texas')
        self.assertEqual(row['City'],'Irving')
        self.assertEqual([row[f'Age-groups.{i}'] for i in range(1,6)],['14u','18u','12u','10u','8u'])
        details = scraper.source_details[url]
        self.assertEqual(details['state'],'TX-DFW')
        self.assertEqual(details['divisions'][0]['competition_class'],'C/Rec')
        self.assertEqual(details['divisions'][1]['competition_class'],'Rec')
        self.assertIn('<ul>',details['description']['html'])
        self.assertIn('<ol>',details['description']['html'])
        self.assertIn('<em>',details['description']['html'])
        self.assertIn('- Official game balls will be provided',details['description']['text'])
        self.assertEqual(details['divisions'][0]['registration']['status'],'closed')
        record={'_key':'5108764366','sport':'FP','starts':payload['info']['eventInfo']['starts'],
                'location':'Irving, TX'}
        with patch('common.softball_national.fetch_listing',return_value=json.dumps([record])):
            first=discover_state(scraper,'TX-DFW',today=date(2026,10,21))
            repeated=discover_state(scraper,'TX-DFW',today=date(2026,10,21))
        self.assertEqual(first,[url])
        self.assertEqual(first,repeated)
        self.assertEqual(event_identity(scraper,url),'USASoftballScraper|softball|5108764366')
        with patch('common.softball_national.fetch_listing',return_value='[]'):
            empty=discover_state(scraper,'TX-DFW')
        self.assertTrue(empty.complete)
        self.assertEqual(empty,[])
        with self.assertRaises(ValueError):
            scraper.configure_search(state='TX-UNKNOWN',period='d|10|2026')

    def test_default_runner_includes_verified_metro_and_bounded_scope_stays_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            with patch('common.softball_national.fetch_listing',return_value='[]') as fetch:
                report=acquire(run_dir=root/'default',ledger=root/'ledger.json',
                    registry={'usa_softball':USASoftballScraper})
            scopes=report['providers']['usa_softball']['states']
            self.assertEqual(len(scopes),66) # 51 state/DC + 15 published US affiliate/national tokens.
            self.assertIn('KSC',scopes)
            self.assertIn('CA-Northern',scopes)
            self.assertIn('USA',scopes)
            self.assertIn('TX-DFW',scopes)
            self.assertTrue(any(call.kwargs['json_body']['stateID']=='TX-DFW' for call in fetch.call_args_list))
            self.assertFalse(report['coverage_complete'])
            with patch('common.softball_national.fetch_listing',return_value='[]') as fetch:
                report=acquire(run_dir=root/'bounded',ledger=root/'ledger.json',
                    registry={'usa_softball':USASoftballScraper},state_codes=['TX'])
            self.assertEqual(list(report['providers']['usa_softball']['states']),['TX'])
            self.assertEqual(fetch.call_count,1)

    def test_triple_crown_identified_event_details_retain_source_rules(self):
        samples=json.loads((FIXTURES.parent/'softball_detail_samples.json').read_text(encoding='utf-8'))
        source=samples['tc_triple-crown-okc']+(FIXTURES/'tc_event_details.html').read_text(encoding='utf-8')
        url='https://www.triplecrownfastpitch.com/events/triple-crown-okc#tc-start=2027-06-04'
        scraper=TripleCrownFastpitchScraper()
        with patch('scrapers.triplecrown_fastpitch_scraper.fetch_listing',return_value=source):
            row=scraper.extract_event_details(url)
        rich=scraper.source_details[url]['description']
        self.assertIn('Umpire Fees',rich['text'])
        self.assertIn('\n\n',rich['text'])
        self.assertIn('Umpire Fees',row['More-information-and-additional-tournament-locations'])


if __name__ == '__main__':
    unittest.main()
