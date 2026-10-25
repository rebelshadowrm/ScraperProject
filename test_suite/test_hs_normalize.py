import unittest
from bs4 import BeautifulSoup
from common.normalize import normalize_age_group, normalize_row
from common.age_groups import age_tokens
from scrapers.ncs_scraper import NCSScraper


class HighSchoolNormalizationTests(unittest.TestCase):
    def test_explicit_school_labels(self):
        for label in ['HS', 'hs', 'High School', 'High School B']:
            self.assertEqual(normalize_age_group(label), 'HS')
        self.assertEqual(normalize_age_group('18U'), '18u')
        self.assertEqual(normalize_age_group('13uB'), '13u')
        for label in ['13uAA', '13uBB']:
            self.assertEqual(age_tokens(label), ['13u'])
            self.assertEqual(normalize_age_group(label), '13u')
        self.assertEqual(normalize_age_group('Graduation 2028'), '')
        self.assertEqual(age_tokens('11/13U A / HS'), ['11u', '13u', 'HS'])

    def test_wide_normalization_preserves_combined_division_fees(self):
        row=normalize_row({'Age-groups.1':'11/13U','Entry-fee.1':'300','Age-groups.2':'HS','Entry-fee.2':'425'})
        self.assertEqual(row['Age-groups.2'],'HS')
        self.assertEqual(row['source_details']['divisions'][0],{'label':'11/13U','age_groups':['11u','13u'],'entry_fee':'300'})
        self.assertEqual(row['source_details']['divisions'][1]['entry_fee'],'425')
        self.assertEqual(normalize_row(row),row)

    def test_ncs_numeric_and_hs_divisions(self):
        soup=BeautifulSoup('<div class="division">High School</div><div class="division">13U AA</div>','html.parser')
        ages,levels=NCSScraper()._parse_divisions(soup,['AA'])
        self.assertEqual(ages,['HS','13U'])
        self.assertEqual([v.upper() for v in levels],['AA'])


if __name__=='__main__':unittest.main()
