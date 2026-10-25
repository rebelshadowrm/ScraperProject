import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
from common.delivery import DeliveryStore
from common.source_exclusions import SourceEventOutOfScope
from scripts.reconcile_softball_recovery import merge
from scripts.reprocess_softball import reprocess,ROOT
from scrapers.usssa_fastpitch_scraper import USSSAFastpitchScraper
from scrapers.pg_softball_scraper import PGSoftballScraper
from test_suite.test_independent_delivery import entry,response,committed


class ReprocessingTests(unittest.TestCase):
    def test_malformed_optional_source_zip_is_omitted_without_losing_the_venue(self):
        from scripts.softball_site_run import site_locations
        details={'locations':[{'venue':'PASEO PARK','city':'Glendale','state':'AZ','zip':'858282'}]}
        mapped=site_locations(details)
        self.assertEqual(mapped[0]['postalCode'],'');self.assertEqual(mapped[0]['city'],'Glendale')
        self.assertEqual(details['locations'][0]['zip'],'858282')
    def test_only_explicit_cancellation_retains_last_good_event_and_history(self):
        from common.softball_lifecycle import retain_cancellation
        old=entry('softball');old['event_data']['City']='Dallas'
        before=copy.deepcopy(old)
        self.assertFalse(retain_cancellation(old,{'spots_remaining':0,'status':'closed'},observed_at='2026-10-21T00:00:00Z'))
        self.assertEqual(old,before)
        self.assertFalse(retain_cancellation(None,{'cancelled':True},observed_at='2026-10-21T00:00:00Z'))
        self.assertTrue(retain_cancellation(old,{'cancelled':True},observed_at='2026-10-21T00:00:00Z'))
        self.assertEqual(old['event_data'],before['event_data']);self.assertTrue(old['processed'])
        self.assertTrue(old['source_details']['cancelled'])

    def test_usssa_cancellation_supplies_lifecycle_evidence_before_excluding_new_events(self):
        scraper=USSSAFastpitchScraper();url='https://txfastpitch.usssa.com/event/cancelled-fixture/'
        with patch('scrapers.usssa_fastpitch_scraper.fetch_listing',return_value='<html></html>'),patch('scrapers.usssa_fastpitch_scraper.sports_events',return_value=[{'eventStatus':'https://schema.org/EventCancelled'}]):
            with self.assertRaises(SourceEventOutOfScope):scraper.extract_event_details(url)
        self.assertTrue(scraper.source_details[url]['cancelled'])
    def test_real_pg_header_city_and_formatted_description_recover_a_rejected_event(self):
        root=Path(__file__).parent/'fixtures/softball_quality'
        pages=[(root/f'pg_149643_{kind}.html').read_text(encoding='utf-8') for kind in ('default','locations')]
        scraper=PGSoftballScraper();url='https://www.perfectgame.org/Events/Default.aspx?event=149643'
        with patch('scrapers.pg_softball_scraper.fetch_listing',side_effect=pages):row=scraper.extract_event_details(url)
        self.assertEqual(row['City'],'Elizabeth City');self.assertEqual(row['State-FullName'],'North Carolina')
        details=scraper.source_details[url]
        self.assertEqual(details['locations'][0]['City'],'Elizabeth City')
        self.assertIn('<br>',details['description']['html']);self.assertNotIn('style=',details['description']['html'])
        self.assertIn('\n',details['description']['text']);self.assertEqual(details['divisions'][0]['age_groups'],[row['Age-groups.1']])
    def test_merge_only_newer_observations_preserves_operator_flags_and_absent_records(self):
        key='USASoftballScraper|softball|42'
        original=entry('softball',provider='USASoftballScraper',is_deleted=True,acquisition_status='ready',acquisition_refreshed_at='2026-10-20T00:00:00+00:00')
        original['url']='https://usa.registerplay.com/pages/event/detail/42'
        original['reviewed_notes']='retain';new=copy.deepcopy(original)
        new.update(processed=False,is_deleted=False,reviewed_notes='overwrite',acquisition_refreshed_at='2026-10-21T00:00:00+00:00')
        new['event_data']['Tournament-name']='New source title'
        base={'events':{key:original,'retained-history':{'sport':'softball'}}}
        data,report=merge(base,[{'events':{key:new}}])
        self.assertEqual(data['events'][key]['event_data']['Tournament-name'],'New source title')
        self.assertTrue(data['events'][key]['is_deleted']);self.assertTrue(data['events'][key]['processed'])
        self.assertEqual(data['events'][key]['reviewed_notes'],'retain');self.assertIn('retained-history',data['events'])
        replay,_=merge(data,[base]);self.assertEqual(replay,data)
        self.assertEqual(report['missing_records_withdrawn'],0)
        flagged=copy.deepcopy(new);flagged.update(source_review_required=True,source_review_reason='Source identifies a league')
        held,_=merge(base,[{'events':{key:flagged}}])
        self.assertTrue(held['events'][key]['source_review_required'])
        fresh=copy.deepcopy(new);fresh['acquisition_refreshed_at']='2026-10-22T00:00:00+00:00'
        restored,_=merge(held,[{'events':{key:fresh}}])
        self.assertNotIn('source_review_required',restored['events'][key])
        wrong=copy.deepcopy(new);wrong['url']='https://usa.registerplay.com/pages/event/detail/43'
        _,report=merge(base,[{'events':{key:wrong}}]);self.assertEqual(len(report['held']),1)

    def test_captured_registerplay_reprocess_preserves_ack_and_repeated_ingestion_is_stable(self):
        fixture=Path(__file__).parent/'fixtures/softball_quality/usa_pinkout_closed.json'
        raw=json.loads(fixture.read_text(encoding='utf-8'))
        identity=raw['header']['_key'];key='USASoftballScraper|softball|'+identity
        raw['info']['eventInfo'].update(starts='2099-10-24',ends='2099-10-25')
        with tempfile.TemporaryDirectory(dir=ROOT/'Tests') as temporary:
            root=Path(temporary);captures=root/'captures';captures.mkdir()
            (captures/'event.json').write_text(json.dumps(raw),encoding='utf-8')
            old=entry('softball',provider='USASoftballScraper',source_event_id=identity,acquisition_status='ready')
            old['url']='https://usa.registerplay.com/pages/event/detail/'+identity
            data={'events':{key:old}};(root/'events.json').write_text(json.dumps(data),encoding='utf-8')
            store=DeliveryStore(root/'outbox.sqlite3');store.ingest(data)
            session=Mock();session.post.side_effect=lambda *a,**kw:response(committed(**kw))
            store.send('softball_connected','https://review.example.test/api/acquisition/events','fixture',session=session)
            store.close()
            with patch('requests.sessions.Session.request',side_effect=AssertionError('Offline recovery attempted network')):
                result=reprocess(ledger=root/'events.json',outbox=root/'outbox.sqlite3',output=root/'result',usa_captures=[captures])
            self.assertEqual(result['historical_acknowledged'],1);self.assertEqual(result['repeat_ingestion']['staged'],0)
            self.assertEqual(result['network_requests'],0)
            after=json.loads((root/'result/events.json').read_text(encoding='utf-8'))['events'][key]
            self.assertTrue(after['processed']);self.assertEqual(after['export_status'],'exported')
            store=DeliveryStore(root/'result/website.sqlite3')
            try:
                row=store.db.execute('select desired_sequence,delivered_sequence,status from deliveries').fetchone()
                self.assertEqual(dict(row),{'desired_sequence':2,'delivered_sequence':1,'status':'pending'})
                self.assertEqual(store.db.execute('select count(*) from delivery_receipts').fetchone()[0],1)
            finally:store.close()

    def test_source_league_stature_is_not_an_ordinary_team_tournament(self):
        raw='<script type="application/ld+json">'+json.dumps({'@type':'SportsEvent','name':'Source league'})+'</script><div class="_item_info"><div class="_item_name">Stature</div><div class="_item_text">League</div></div>'
        with patch('scrapers.usssa_fastpitch_scraper.fetch_listing',return_value=raw):
            with self.assertRaisesRegex(SourceEventOutOfScope,'stature is League'):
                USSSAFastpitchScraper().extract_event_details('https://txfastpitch.usssa.com/event/league/')


if __name__=='__main__':unittest.main()
