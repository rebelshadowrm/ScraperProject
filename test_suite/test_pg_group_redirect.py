import json
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from provider_replay import PG_DETAIL, PG_LOCATION
from scrapers.pg_scraper import PGScraper


class GroupRedirectFields(unittest.TestCase):
    def setUp(self):
        self.scraper = PGScraper()
        self.addCleanup(self.scraper.close)
        self.html = PG_DETAIL.replace('Summer Classic 10U (AA)', '2099 PG 14U Fall Classic PAP (Open)').replace('Jul 20-22', 'Oct 24-25, 2099') + '<div id="pnlTournaments">Team eligibility</div>'
        self.before = json.loads((Path(__file__).parent / 'fixtures/pg_single_before_redirect_fix.json').read_text())['row']

    def parse(self, context):
        with patch.object(self.scraper, '_get_soup', return_value=BeautifulSoup(PG_LOCATION, 'html.parser')):
            return self.scraper._parse_single_event_page(None, 'https://www.perfectgame.org/Events/Default.aspx?event=123', soup=BeautifulSoup(self.html, 'html.parser'), group_context=context)

    def test_direct_single_event_preserves_all_frozen_fields(self):
        self.assertEqual(self.parse(False), self.before)

    def test_group_redirect_recovers_only_published_fields_and_group_contract(self):
        row = self.parse(True)
        differences = {key: value for key, value in row.items() if value != self.before[key]}
        self.assertEqual(differences, {'Venue': 'Central Park', 'Competition-level.1': 'OPEN', 'Tournament-type': 'Team', 'Tournament-surface': 'NA'})
        from common.normalize import normalize_row
        self.assertEqual(normalize_row(row)['Competition-level.1'], 'Open')

    def test_direct_extraction_passes_group_context_after_real_redirect(self):
        driver = type('Driver', (), {'current_url': 'https://www.perfectgame.org/Events/Default.aspx?event=123'})()
        def soup(*args):
            self.scraper._last_soup_used_browser = True
            return BeautifulSoup(self.html, 'html.parser')
        with patch.object(self.scraper, '_get_soup', side_effect=soup), patch.object(self.scraper, '_parse_single_event_page', return_value=self.before) as parse:
            self.scraper.extract_event_details('https://www.perfectgame.org/Schedule/GroupedEvents.aspx?gid=1', driver)
        self.assertTrue(parse.call_args.kwargs['group_context'])


if __name__ == '__main__':
    unittest.main()
