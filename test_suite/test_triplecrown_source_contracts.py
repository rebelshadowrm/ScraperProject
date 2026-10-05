import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

import scrapers.triplecrown_scraper as module
from common.validation import validate_event_data


class TripleCrownSourceContracts(unittest.TestCase):
    """Reduced source contracts captured during the October 4 production pull."""

    def setUp(self):
        self.scraper = module.TripleCrownScraper()
        self.addCleanup(self.scraper.session.close)
        self.get = patch.object(self.scraper.session, "get", side_effect=AssertionError("unexpected network"))
        self.get.start()
        self.addCleanup(self.get.stop)

    def test_empty_marker_requires_schedule_component_and_search_form(self):
        html = '<div id="filtered-event-schedule-container"><form id="filtered-event-search-form"></form><div id="filtered-event-table-container"><div class="filtered-events-no-results font">No results found matching your search.</div></div></div>'
        for source, complete in [(html, True), (html.replace('id="filtered-event-search-form"', ''), False), ('<p>No results found matching your search.</p>', False), ('<h1>Access denied</h1>', False)]:
            with self.subTest(source=source), patch.object(module, "_soup", return_value=BeautifulSoup(source, "html.parser")):
                links = self.scraper.collect_event_links(None, module.TRIPLE_CROWN_FEED_BASE + "AL")
                self.assertEqual(list(links), [])
                self.assertEqual(links.complete, complete)

    def pathway(self, eligibility, *, dates="September 18-19, 2027", extra=""):
        html = f'<h1>Pathway Las Vegas</h1><p>Dates: {dates}</p><p>{eligibility}</p><p>{extra}</p><a href="mailto:brandon@triplecrownsports.com">Contact</a>'
        return self.scraper._handle_pathway("https://www.pathwaybaseball.com/events/sample", BeautifulSoup(html, "html.parser"), {"listing_state": "Nevada"})

    def test_upperclass_divisions_preserve_price_without_inventing_age_fee_pairs(self):
        row = self.pathway("Divisions: Upperclass &amp; Underclass", extra="Price: $1145")
        self.assertEqual(row["Tournament-type"], "Team")
        self.assertEqual(row["More-information-and-additional-tournament-locations"], "Divisions: Upperclass & Underclass | Price: $1145")
        self.assertTrue(validate_event_data(dict(row))[0])
        for idx in range(1, 14):
            self.assertEqual(row[f"Age-groups.{idx}"], "")
            self.assertEqual(row[f"Entry-fee.{idx}"], "")

    def test_player_eligibility_preserves_invitation_and_listing_date_priority(self):
        soup = BeautifulSoup('<h1>Pathway Prospect Games</h1><p>Dates: August 4-8, 2027</p><p>Player Eligibility: 2027-2030 Grads (Uncommitted)</p><p>Invite Only: Top 132 players</p><a href="mailto:brandon@triplecrownsports.com">Contact</a>', "html.parser")
        row = self.scraper._handle_pathway("https://www.pathwaybaseball.com/events/pathway-prospect-games", soup, {"start_date": "08/03/2027", "end_date": "08/08/2027", "listing_state": "California"})
        self.assertEqual(row["Start-date"], "08/03/2027")
        self.assertEqual(row["Tournament-type"], "Individual")
        notes = row["More-information-and-additional-tournament-locations"]
        self.assertIn("Player Eligibility: 2027-2030 Grads (Uncommitted)", notes)
        self.assertIn("Invite Only: Top 132 players", notes)
        self.assertIn("Detail page dates: 08/04/2027 - 08/08/2027; listing dates retained", notes)

    def test_camp_uses_published_page_email_and_keeps_waitlist_restriction(self):
        html = '<div class="et_pb_text_inner"><p>FALL CAMP 2026 SOLD OUT Waitlist ONLY</p><p>TEXT (916)798-7653</p><p>Rocklin, CA</p><p>HS GRAD YEARS: 2027 2028 2029 2030</p><a href="https://www.google.com/maps/place/Baseball+Field/@38.7899838,-121.2099762,17z">SIERRA COLLEGE BASEBALL FIELD- MAP</a></div><footer><a href="mailto:baseball@allstartournaments.com">Contact</a></footer>'
        soup = BeautifulSoup(html, "html.parser")
        meta = {"listing_name": "College Prep Baseball Clinic", "start_date": "10/11/2026", "end_date": "10/11/2026", "listing_city": "Rocklin", "listing_state": "California"}
        row = self.scraper._handle_allstar("https://allstartournaments.com/baseball/camp-summer/", soup, meta)
        self.assertEqual(row["Email"], "baseball@allstartournaments.com")
        self.assertEqual(row["Tournament-type"], "Individual")
        self.assertEqual(row["More-information-and-additional-tournament-locations"], "HS GRAD YEARS: 2027 2028 2029 2030 | SOLD OUT Waitlist ONLY")
        self.assertTrue(validate_event_data(dict(row))[0])
        soup.footer.decompose()
        with self.assertRaisesRegex(ValueError, "Missing required email"):
            self.scraper._handle_allstar("https://allstartournaments.com/baseball/camp-summer/", soup, meta)

    def test_unlabelled_or_unknown_eligibility_still_fails(self):
        for text in ["Upperclass &amp; Underclass", "Player Eligibility: TBD", "HS GRAD YEARS: TBD", "Grads 2027-2030"]:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "Missing required age group"):
                self.pathway(text)

    def test_graduation_years_alone_do_not_turn_team_events_into_individual_events(self):
        row = self.pathway("HS GRAD YEARS: 2027 2028 2029 2030")
        self.assertEqual(row["Tournament-type"], "Team")
        self.assertIn("HS GRAD YEARS: 2027 2028 2029 2030", row["More-information-and-additional-tournament-locations"])


if __name__ == "__main__":
    unittest.main()
