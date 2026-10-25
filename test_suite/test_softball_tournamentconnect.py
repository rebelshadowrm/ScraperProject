import base64
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from common.softball_tournamentconnect import TC_LISTING,collect_current,tc_identity,tc_read
from common.source_exclusions import SourceEventIncomplete
from scrapers.pgf_scraper import PGFScraper

SAMPLES=json.loads((Path(__file__).parent/'fixtures/softball_tournamentconnect.json').read_text(encoding='utf-8'))


class TournamentConnectTests(unittest.TestCase):
    def setUp(self):
        guard=patch('requests.sessions.Session.request',side_effect=AssertionError('Offline fixture attempted network'))
        guard.start();self.addCleanup(guard.stop)

    def test_public_read_protocol_and_independent_numeric_identity(self):
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(SAMPLES['first'])) as fetch:
            payload=tc_read({'eventid':5203})
        self.assertEqual(payload['event']['eventid'],5203)
        self.assertTrue(fetch.call_args.args[0].endswith('?e=1'))
        wire=json.loads(base64.b64decode(fetch.call_args.kwargs['data']))
        self.assertEqual(wire,{'pass':1,'controller':'Event','action':'read','parameters':{'eventid':5203}})
        self.assertEqual(tc_identity(TC_LISTING+'#tc-event=5203'),'tc-5203')
        with self.assertRaises(ValueError):tc_identity(TC_LISTING.replace('premiergirlsfastpitch.com','attacker.test')+'#tc-event=5203')

    def test_current_listing_decodes_only_bounded_page_and_preserves_revision(self):
        rows=[SAMPLES['first'],SAMPLES['second']]
        encoded=base64.b64encode(json.dumps(rows).encode()).decode()
        scraper=PGFScraper()
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=encoded) as fetch:
            links=collect_current(scraper,max_events=2,max_pages=1)
        self.assertEqual(len(links),2);self.assertEqual(fetch.call_count,1)
        self.assertFalse(links.complete)
        self.assertEqual([scraper.get_event_identity(url) for url in links],['tc-5203','tc-3390'])
        self.assertTrue(scraper.get_discovery_revision(links[0]))
        self.assertEqual(json.loads(base64.b64decode(fetch.call_args.kwargs['data']))['parameters']['limit'],5)

    def test_real_fields_keep_class_fee_pairs_venues_and_contact(self):
        scraper=PGFScraper();url=TC_LISTING+'#tc-event=5203'
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(SAMPLES['first'])):
            row=scraper.extract_event_details(url)
        self.assertEqual(row['Start-date'],'10/23/2026')
        self.assertEqual([row[f'Age-groups.{i}'] for i in [1,2,3]],['14u','12u','10u'])
        self.assertEqual([row[f'Entry-fee.{i}'] for i in [1,2,3]],['850','850','850'])
        self.assertEqual(row['City'],'Evansville');self.assertEqual(row['State-FullName'],'Indiana')
        self.assertEqual(row['Venue'],'Deaconess Sports Park')
        self.assertEqual(row['Email'],'pgfsuperselectevents@gmail.com')
        self.assertTrue(any(' B' in item['label'] for item in scraper.source_details[url]['divisions']))

    def test_missing_optional_facilities_does_not_invent_a_venue(self):
        scraper=PGFScraper();url=TC_LISTING+'#tc-event=3390'
        with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(SAMPLES['second'])):
            row=scraper.extract_event_details(url)
        self.assertEqual(row['City'],'Crown Point');self.assertEqual(row['Venue'],'')
        self.assertIn('Facilities not published',' '.join(scraper.source_details[url]['data_gaps']))

    def test_cross_sport_or_unresolved_range_cannot_be_fabricated(self):
        for mutation in ['sport','age_range']:
            payload=copy.deepcopy(SAMPLES['first'])
            if mutation=='sport':payload['event']['sportid']=1
            else:payload['divisions'][0]['toDivisionAge']=18
            with patch('common.softball_tournamentconnect.fetch_listing',return_value=json.dumps(payload)):
                with self.assertRaises(SourceEventIncomplete):PGFScraper().extract_event_details(TC_LISTING+'#tc-event=5203')


if __name__=='__main__':unittest.main()
