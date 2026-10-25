import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from common.identity import event_identity
from common.softball_registry import PROFILES, PROVIDERS, softball_catalog, softball_registry
from common.softball_scaffold import SoftballNotReady, SoftballScaffoldScraper
from scripts.softball_discovery_smoke import inspect_event, run

FIXTURES = Path(__file__).parent / 'fixtures'


class SoftballAPIFrameworkTests(unittest.TestCase):
    def test_frozen_before_change_discovery_parity(self):
        baseline = json.loads((FIXTURES / 'softball_definition_baseline.json').read_text())
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Offline replay network')):
            for key, expected in baseline['discovery'].items():
                scraper = softball_registry()[key]()
                links = scraper.parse_listing((FIXTURES/expected['fixture']).read_text(), scraper.DISCOVERY_URL)
                actual = dict(fixture=expected['fixture'], links=list(links),
                    ids=[event_identity(scraper, url) for url in links], complete=links.complete, pages=links.pages)
                self.assertEqual(actual, expected, key)

    def test_catalog_covers_scope_in_api_html_browser_order_without_enabling(self):
        catalog = softball_catalog()
        self.assertEqual(set(catalog), set(PROVIDERS))
        self.assertEqual(set(PROFILES), set(PROVIDERS))
        self.assertEqual(list(catalog)[0], 'usa_fastpitch')
        self.assertTrue(all(not value['default_enabled'] for value in catalog.values()))
        self.assertTrue(all(not value['acquisition_ready'] for value in catalog.values()))
        self.assertEqual(set(softball_catalog(kind='api')), {'usa_fastpitch','usa_softball','nsa_fastpitch','alliance_fastpitch','topgun_sports','pgf'})
        self.assertEqual(catalog['pgf']['discovery'], 'api')
        with self.assertRaises(ValueError):
            softball_catalog(kind='guessed')

    def test_tenant_scoping_same_ids_do_not_collide(self):
        raw = (FIXTURES/'softball_active.json').read_text()
        identities = []
        for key in ('usa_fastpitch','usa_softball','nsa_fastpitch'):
            scraper = softball_registry()[key]()
            url = scraper.parse_listing(raw, scraper.DISCOVERY_URL)[0]
            identities.append(event_identity(scraper,url))
            other = softball_registry()['usa_fastpitch']().parse_listing(raw, '')[0]
            if key != 'usa_fastpitch':
                with self.assertRaises(ValueError):
                    scraper.get_event_identity(other)
        self.assertEqual(len(set(identities)),3)

    def test_nsa_excludes_observed_slowpitch_and_rejects_unknown_sport(self):
        scraper = softball_registry()['nsa_fastpitch']()
        rows = json.loads((FIXTURES/'softball_active.json').read_text())
        rows.insert(0, dict(rows[0],sport='SP'))
        links = scraper.parse_listing(json.dumps(rows),scraper.DISCOVERY_URL)
        self.assertEqual(len(links),2)
        self.assertEqual(links.excluded_other_sport,1)
        rows[0]['sport']='UNKNOWN'
        with self.assertRaises(ValueError):
            scraper.parse_listing(json.dumps(rows),scraper.DISCOVERY_URL)

    def test_alliance_positive_identity_zero_ids_and_partial_coverage(self):
        scraper = softball_registry()['alliance_fastpitch']()
        raw=(FIXTURES/'softball_alliance.json').read_text()
        links=scraper.parse_listing(raw,scraper.DISCOVERY_URL)
        self.assertEqual(len(links),1)
        self.assertEqual(links.unresolved_identities,2)
        self.assertFalse(links.complete)
        self.assertEqual(event_identity(scraper,links[0]),'AllianceFastpitchScraper|softball|5396')
        self.assertIn('/CaseSensitive#alliance-event=5396',links[0])
        self.assertEqual(links.source_total,3)
        report=run('alliance_fastpitch',fixture=FIXTURES/'softball_alliance.json')
        self.assertEqual(report['unresolved_identities'],2)

    def test_alliance_stale_missing_counts_conflicting_ids_and_unsafe_urls_fail(self):
        original=json.loads((FIXTURES/'softball_alliance.json').read_text())
        scraper=softball_registry()['alliance_fastpitch']()
        mutations=[lambda p:p['data'].update(stale=True), lambda p:p['data'].pop('stale'),
                   lambda p:p.update(success=False), lambda p:p['data'].update(totalRecords=1),
                   lambda p:p['data']['events'][0].update({'Event ID':True}),
                   lambda p:p['data']['events'][0].update(event_name="<a href='http://example.org'>Unsafe</a>"),
                   lambda p:p['data']['events'][1].update({'Event ID':5396,'event_name':"<a href='https://other.example.org'>Conflict</a>"})]
        for mutate in mutations:
            payload=copy.deepcopy(original);mutate(payload)
            with self.assertRaises(ValueError):
                scraper.parse_listing(json.dumps(payload),scraper.DISCOVERY_URL)

    def test_detail_inspection_preserves_fee_pairs_and_cannot_be_an_import_row(self):
        payload=json.loads((FIXTURES/'softball_registerplay_details.json').read_text())
        scraper=softball_registry()['usa_fastpitch']()
        url='https://usafastpitch.org/pages/event/detail/7630151141'
        report=scraper.inspect_detail_payloads(url,**payload)
        self.assertEqual(report['divisions'][0]['divisionFee'],'795')
        self.assertEqual(report['divisions'][0]['fee'],'820.08')
        self.assertEqual(report['divisions'][1]['divisionFee'],'0')
        self.assertNotIn('divisionTeams',report['divisions'][1])
        self.assertFalse(report['ingestion_ready'])
        self.assertNotIn('Tournament-name',report)
        with patch.object(scraper,'inspect_event',return_value=report):
            row=scraper.extract_event_details(url)
        self.assertEqual(row['Tournament-name'],'Fixture Classic')
        self.assertEqual(row['Entry-fee.1'],'795')
        self.assertEqual(row['Entry-fee.2'],'0')
        payload['header']['_key']='wrong'
        with self.assertRaises(ValueError):
            scraper.inspect_detail_payloads(url,**payload)

    def test_cold_detail_inspection_uses_only_two_verified_endpoints(self):
        payload=json.loads((FIXTURES/'softball_registerplay_details.json').read_text())
        with patch('common.softball_api.fetch_listing',side_effect=[json.dumps(payload['header']),json.dumps(payload['info'])]) as fetch:
            report=inspect_event('usa_fastpitch','https://usafastpitch.org/pages/event/detail/7630151141')
        self.assertEqual(report['status'],'inspection_only')
        self.assertEqual(fetch.call_count,2)
        self.assertIn('/header4EP/7630151141',fetch.call_args_list[0].args[0])
        self.assertIn('/info-v3.0/desktop/7630151141',fetch.call_args_list[1].args[0])

    def test_ready_flag_cannot_enable_inherited_unsupported_extraction(self):
        class Unfinished(SoftballScaffoldScraper):
            ACQUISITION_READY=True
        with self.assertRaises(SoftballNotReady):
            Unfinished.require_ready()
        from common.provider_access import softball_classes
        manifest=Mock()
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(softball_classes(manifest),[])
        self.assertEqual(manifest.provider.call_count,13)

    def test_html_profile_inspection_matches_source_labels_without_ingestion(self):
        scraper=softball_registry()['fasa']()
        html=(FIXTURES/'softball_fasa_profile.html').read_text()
        url='https://www.playfasa.com/profile.asp?ID=15448'
        result=scraper.inspect_detail_html(url,html)
        self.assertEqual(result['fee_label'],'$200 NO PAP')
        self.assertEqual(result['age_labels'],'10U 12U')
        self.assertEqual(result['organizer']['emails'],['organizer@example.org'])
        self.assertNotIn('participant@example.org',json.dumps(result))
        self.assertFalse(result['ingestion_ready'])
        self.assertFalse(result['cancelled'])
        with self.assertRaises(ValueError):
            scraper.inspect_detail_html(url,html.replace('10/20/2099','Oct 20'))
        with patch('common.softball_scaffold.fetch_listing',return_value=html) as fetch:
            report=inspect_event('fasa',url)
        self.assertEqual(report['requests'],1)
        self.assertEqual(fetch.call_count,1)

    def test_pg_seeded_http_discovery_and_unseeded_fail_before_request(self):
        scraper=softball_registry()['pg_softball']()
        html='<a href="../Events/Default.aspx?event=120624">8U</a><a href="../Events/Default.aspx?event=120625">10U</a>'
        seed='https://www.perfectgame.org/softball/schedule.aspx?'+'&'.join(key+'=observed' for key in ('ty','fg','dt','cl','dv','st','sp','ep','tm'))
        with patch('scrapers.pg_softball_scraper.fetch_listing',return_value=html) as fetch:
            links=scraper.collect_event_links(None,seed,max_events=1)
            self.assertEqual(len(links),1)
            self.assertFalse(links.complete)
            self.assertEqual(scraper.get_event_identity(links[0]),'120624')
            self.assertEqual(fetch.call_count,1)
        with patch('scrapers.pg_softball_scraper.fetch_listing') as fetch:
            for invalid in (scraper.DISCOVERY_URL,seed.replace('www.perfectgame.org','foreign.example'),seed+'&ty=second'):
                with self.assertRaises(SoftballNotReady):
                    scraper.collect_event_links(None,invalid)
            fetch.assert_not_called()


if __name__=='__main__':
    unittest.main()
