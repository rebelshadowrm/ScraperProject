"""Offline field and finite-run contracts, replaying trimmed public source DOM."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from common.config import OUTPUT_COLUMNS
from common.softball_registry import softball_registry
from common.softball_rows import age_tokens,make_row,source_amount,source_address
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from common.softball_scaffold import SoftballNotReady
from scripts.softball_provider_cycle import CaptureFetcher,run_cycle,ROOT
from scripts.softball_cycle_queue import main as queue_main


FIXTURES=Path(__file__).parent/'fixtures'
SAMPLES=json.loads((FIXTURES/'softball_detail_samples.json').read_text(encoding='utf-8'))


class SoftballDetailCycleTests(unittest.TestCase):
    def setUp(self):
        self.network=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline test attempted network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def extract(self,provider,url,html):
        scraper=softball_registry()[provider]()
        self.addCleanup(scraper.close)
        with patch('scrapers.'+provider+'_scraper.fetch_listing',return_value=html):
            return scraper.extract_event_details(url),scraper

    def test_age_classes_fees_and_all_locations_survive_mapping(self):
        self.assertEqual(age_tokens('10u, 12uA, 12uB, 14uA, 14uB, 16u, 18u'),['10u','12u','14u','16u','18u'])
        self.assertEqual(age_tokens('HS graduation 2028'),['HS'])
        self.assertEqual(age_tokens('graduation 2028'),[])
        self.assertEqual(source_amount('$0'),'0')
        self.assertEqual(source_amount('$900-1200'),'')
        self.assertEqual(source_amount('$200 NO PAP'),'')
        places=[source_address('Park','1 Main St, Austin, TX 78701'),dict(venue='Second park')]
        row=make_row(title='Fixture',start='2099-10-20',end='2099-10-21',url='https://example.org/event',
                     state='TX',locations=places,divisions=[dict(label='12uA',entry_fee=500),dict(label='12uB',entry_fee=400)])
        self.assertEqual(set(row),set(OUTPUT_COLUMNS))
        self.assertEqual([row['Age-groups.1'],row['Age-groups.2']],['12u','12u'])
        self.assertEqual([row['Entry-fee.1'],row['Entry-fee.2']],['500','400'])
        self.assertEqual(row['Are-there-multiple_locations'],'Yes')
        self.assertIn('12uB',row['More-information-and-additional-tournament-locations'])
        self.assertEqual(row['Zip'],'78701')

    def test_topgun_two_current_layouts_preserve_age_fee_partitions(self):
        row,_=self.extract('topgun_events','https://app.tagup.com/topgun/event/tournament/658/',SAMPLES['topgun_658'])
        self.assertEqual(row['Start-date'],'10/23/2026')
        self.assertEqual(row['Entry-fee.1'],'895.00')
        self.assertEqual(row['Entry-fee.2'],'1450.00')
        self.assertEqual(row['City'],'Papillion')
        row,_=self.extract('topgun_events','https://app.tagup.com/topgun/event/tournament/675/',SAMPLES['topgun_675'])
        self.assertEqual(row['State-FullName'],'Arkansas')
        self.assertEqual(row['Age-groups.1'],'14u')
        self.assertEqual(row['Entry-fee.3'],'1350')
        self.assertEqual(row['Guaranteed-games'],'4')
        self.assertEqual(row['Phone-number'],'913-342-8847')

    def test_topgun_missing_fee_partition_fails_instead_of_assigning_another_fee(self):
        html=SAMPLES['topgun_658'].replace('12u: $895.00','unpublished price')
        with self.assertRaisesRegex(SourceEventIncomplete,'partitions'):
            self.extract('topgun_events','https://app.tagup.com/topgun/event/tournament/658/',html)

    def test_usssa_verified_read_post_preserves_three_division_fees(self):
        scraper=softball_registry()['usssa_fastpitch']()
        with patch('scrapers.usssa_fastpitch_scraper.fetch_listing',side_effect=[SAMPLES['usssa_detail'],SAMPLES['usssa_divisions']]) as fetch:
            url='https://txfastpitch.usssa.com/event/c-classhalloween-spook-outmvp/'
            row=scraper.extract_event_details(url)
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(fetch.call_args.kwargs['method'],'POST')
        self.assertEqual(fetch.call_args.kwargs['data']['action'],'fetch_event_divisions_ajax')
        self.assertEqual([row[f'Age-groups.{i}'] for i in [1,2,3]],['8u','10u','12u'])
        self.assertEqual([row[f'Entry-fee.{i}'] for i in [1,2,3]],['185','185','185'])
        self.assertIn('/baseball/',scraper.source_details[url]['schema_registration_url'])
        self.assertEqual(row['State-FullName'],'Texas')

    def test_usfa_distinct_and_uniform_fee_contracts(self):
        for identity in ['USFA46019','USFA46020']:
            row,_=self.extract('usfa','https://usfaevents.com/TTContent.aspx?tid='+identity,SAMPLES[identity])
            self.assertTrue(row['Start-date'].endswith('/2027'))
            self.assertEqual(row['State-FullName'],'Florida')
            self.assertTrue(row['Age-groups.1'])
        self.assertEqual(row['Entry-fee.1'],'895')

    def test_triplecrown_occurrence_and_class_suffixed_ages(self):
        url='https://www.triplecrownfastpitch.com/events/triple-crown-okc#tc-start=2027-06-04'
        row,scraper=self.extract('triplecrown_fastpitch',url,SAMPLES['tc_triple-crown-okc'])
        self.assertEqual([row[f'Age-groups.{i}'] for i in range(1,6)],['10u','12u','14u','16u','18u'])
        self.assertTrue(all(not row[f'Entry-fee.{i}'] for i in range(1,14)))
        self.assertEqual(row['Guaranteed-games'],'5')
        self.assertIn('12uB',row['More-information-and-additional-tournament-locations'])
        with self.assertRaisesRegex(SourceEventIncomplete,'changed'):
            self.extract('triplecrown_fastpitch',url.replace('06-04','06-05'),SAMPLES['tc_triple-crown-okc'])
        other=url.replace('triple-crown-okc','valley-invite').replace('06-04','06-18')
        with self.assertRaisesRegex(SourceEventIncomplete,'age range'):
            self.extract('triplecrown_fastpitch',other,SAMPLES['tc_valley-invite'])
        self.assertIn('tc-start=2027-06-04',scraper.get_event_identity(url))

    def test_alliance_external_pages_preserve_missing_and_package_prices(self):
        url='https://www.connectsportsevents.com/ray-seymour-invitational-2026/#alliance-event=5396'
        row,scraper=self.extract('alliance_fastpitch',url,SAMPLES['alliance_connect'])
        self.assertEqual(row['Start-date'],'10/23/2026')
        self.assertEqual(row['Email'],'customerservice@connectsportsevents.com')
        self.assertEqual(row['Entry-fee.1'],'')
        self.assertIn('$1,925',row['More-information-and-additional-tournament-locations'])
        url='https://tourneymachine.com/Public/Results/Tournament.aspx?IDTournament=h202602251830400303715868c734846#alliance-event=6201'
        row,scraper=self.extract('alliance_fastpitch',url,SAMPLES['alliance_tourney'])
        self.assertEqual(row['Start-date'],'10/24/2026')
        self.assertEqual(row['Email'],'') # Do not capture platform support as organizer.
        self.assertEqual(row['Are-there-multiple_locations'],'Yes')
        self.assertEqual(len(scraper.source_details[url]['locations']),3)
        with self.assertRaises(SoftballNotReady):
            scraper.extract_event_details('https://unverified.example/event#alliance-event=123')

    def test_capture_replay_and_post_contract_are_network_free(self):
        with tempfile.TemporaryDirectory() as root:
            capture=CaptureFetcher(root,max_requests=1)
            with patch('scripts.softball_provider_cycle.fetch_listing',return_value='captured') as fetch:
                self.assertEqual(capture('https://example.org/api',content_type='json',method='POST',data={'nonce':'a'}),'captured')
                self.assertEqual(capture('https://example.org/api',content_type='json',method='POST',data={'nonce':'a'}),'captured')
                fetch.assert_called_once()
                self.assertEqual(capture.requests,1)
            replay=CaptureFetcher(root,replay=True)
            self.assertEqual(replay('https://example.org/api',content_type='json',method='POST',data={'nonce':'a'}),'captured')
            with self.assertRaisesRegex(RuntimeError,'no network fallback'):
                replay('https://example.org/missing')
            self.assertEqual(replay.requests,0)

    def test_cycle_reports_source_exclusions_and_restricts_output(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'Tests') as root:
            root=Path(root)
            with patch('scrapers.fasa_scraper.FASAScraper.extract_event_details',side_effect=SourceEventOutOfScope('Cancelled by source')):
                report=run_cycle('fasa',output=root/'report.json',capture_dir=root/'captures',replay=True,
                                 event_urls=['https://www.playfasa.com/profile.asp?ID=15448'])
            self.assertEqual(report['counts']['out_of_scope'],1)
            self.assertEqual(report['requests'],0)
            self.assertFalse(report['coverage_complete'])
        with self.assertRaisesRegex(ValueError,'Tests/'):
            run_cycle('fasa',output=ROOT/'unsafe-report.json',capture_dir=ROOT/'Tests/captures')

    def test_queue_continues_after_error_and_timeout_without_retrying(self):
        for first in [RuntimeError('launch failed'),dict(exit_code=124,timed_out=True)]:
            with tempfile.TemporaryDirectory(dir=ROOT/'Tests') as root:
                with patch('scripts.softball_cycle_queue.run_bounded',side_effect=[first,dict(exit_code=0,timed_out=False)]) as run:
                    result=queue_main(['--providers','fasa','usfa','fasa','--run-dir',root,'--workers','1','--timeout','5'])
                self.assertEqual(result,2)
                self.assertEqual(run.call_count,2)
                report=json.loads((Path(root)/'queue.json').read_text())
                self.assertEqual(report['remaining'],0)
                self.assertEqual(len(report['providers']),2)


if __name__=='__main__':unittest.main()
