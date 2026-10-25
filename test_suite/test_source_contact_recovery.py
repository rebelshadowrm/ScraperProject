import unittest
from bs4 import BeautifulSoup
from common.softball_contacts import cloudflare_email, pg_contact_facts, pg_division_facts


class SourceContactRecoveryTests(unittest.TestCase):
    def test_public_cloudflare_rendering_is_decoded_without_guessing(self):
        email = 'director@example.test'
        encoded = bytes([37] + [byte ^ 37 for byte in email.encode()]).hex()
        soup = BeautifulSoup(f'<span data-cfemail="{encoded}">[email protected]</span>', 'html.parser')
        self.assertEqual(cloudflare_email(soup.span), email)
        self.assertEqual(cloudflare_email(None), '')
        soup.span['data-cfemail'] = 'malformed'
        self.assertEqual(cloudflare_email(soup.span), '')

    def test_pg_role_and_destination_are_independent_from_organization_and_title(self):
        soup = BeautifulSoup('''<h1>Invitational HS mystery</h1>
            <span id="prefix_fvEventContact_lblDirectorName">Synthetic Director</span>
            <a id="prefix_EventHeader1_hlSideMenuContact" href="Contact.aspx?event=42">Contact us</a>
            <a id="prefix_rptDivisions_hlDivisions_0" href="/Events/Default.aspx?event=43">12U (A)</a>
            <a id="prefix_rptDivisions_hlDivisions_1" href="/Events/Default.aspx?event=42">18U (B)</a>''', 'html.parser')
        url = 'https://www.perfectgame.org/Events/Default.aspx?event=42'
        self.assertEqual(pg_contact_facts(soup, url), {
            'directorName': 'Synthetic Director', 'organizer_contact': {'form': {
                'page_url': 'https://www.perfectgame.org/Events/Contact.aspx?event=42',
                'action_label': 'Contact event director'}}})
        self.assertEqual(pg_division_facts(soup, url), {
            'age_groups': ['18u'], 'competition_class': 'B', 'classification_source_label': '18U (B)'})
        self.assertEqual(pg_division_facts(soup, url.replace('42', '44')), {})
        soup.select_one('[id$="hlSideMenuContact"]')['href'] = 'javascript:bad()'
        self.assertNotIn('organizer_contact', pg_contact_facts(soup, url))


if __name__ == '__main__':
    unittest.main()
