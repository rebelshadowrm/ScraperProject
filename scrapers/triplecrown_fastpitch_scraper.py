"""Preliminary TripleCrownFastpitch adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.softball_scaffold import fetch_listing
from common.collection import CollectedLinks
from common.source_exclusions import SourceEventIncomplete
from common.softball_rows import age_tokens,source_amount
from common.identity import normalize_url
from common.softball_description import description
from scrapers.triplecrown_scraper import TripleCrownScraper,_parse_month_day_range
from bs4 import BeautifulSoup
from urllib.parse import urldefrag
from datetime import datetime
import re


class TripleCrownFastpitchScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "TripleCrownFastpitch"
    DISCOVERY_URL = "https://www.triplecrownfastpitch.com/"
    BLOCKER = "Featured occurrence rows verified; stable registration IDs, search coverage and division/fee ranges remain"
    EVENT_HOST = "www.triplecrownfastpitch.com"
    EVENT_PATH = "/events/[^/]+/?"
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "a[href*=\"/events/\"]"
    REUSE = "scrapers.triplecrown_scraper.TripleCrownScraper"

    @staticmethod
    def _details(soup):
        values={}
        for block in soup.select('div.event-details-info div.text-row'):
            label=block.select_one('.item-title');value=block.select_one('.info-item-title')
            if label and value:values[label.get_text(' ',strip=True).rstrip(':')]=value.get_text(' ',strip=True)
        return values

    def get_event_identity(self,url):
        public,fragment=urldefrag(url)
        super().get_event_identity(public)
        if not fragment:return normalize_url(public) # Historical preliminary fixture contract only.
        if not re.fullmatch(r'tc-start=\d{4}-\d{2}-\d{2}',fragment):raise ValueError('Invalid Triple Crown occurrence date')
        datetime.strptime(fragment.split('=',1)[1],'%Y-%m-%d')
        return normalize_url(public)+'|'+fragment

    def collect_event_links(self,driver,base_url,**kwargs):
        links=super().collect_event_links(driver,base_url,**kwargs)
        self._detail_html={}
        result=[]
        for public in links:
            html=fetch_listing(public)
            details=self._details(BeautifulSoup(html,'html.parser'))
            raw=details.get('Dates','')
            if not re.search(r'\b20\d{2}\b',raw):raise SourceEventIncomplete('Triple Crown family needs explicit occurrence year before identity')
            start,end=_parse_month_day_range(raw,None)
            if not start or not end:raise SourceEventIncomplete('Triple Crown family date range is unrecognized')
            value=datetime.strptime(start,'%m/%d/%Y').strftime('%Y-%m-%d')
            url=public+'#tc-start='+value
            self._detail_html[url]=html
            result.append(url)
        return CollectedLinks(result,complete=False,pages=1,error='Featured occurrence sample only; schedule coverage and stable registration IDs need verification')

    def extract_event_details(self,url,driver=None,**kwargs):
        self.get_event_identity(url)
        public,fragment=urldefrag(url)
        if not fragment:raise SourceEventIncomplete('Triple Crown family URL lacks a source-backed occurrence identity')
        html=getattr(self,'_detail_html',{}).get(url) or fetch_listing(public)
        soup=BeautifulSoup(html,'html.parser')
        details=self._details(soup)
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(source_fields=details)
        eligibility=details.get('Division') or details.get('Divisions','')
        if re.search(r'\d{1,2}u\s*[-\u2013]\s*\d{1,2}u',eligibility,re.I):
            raise SourceEventIncomplete('Triple Crown publishes an age range; individual divisions/fee partitions need verification')
        helper=TripleCrownScraper()
        # Reuse the pure source parser, keeping external enrichment explicit.
        helper._merge_exposure_fallback=lambda row,_soup:row
        try:row=helper._handle_webflow_family(public,soup,{})
        finally:helper.close()
        if not row:raise SourceEventIncomplete('Triple Crown fastpitch fields did not satisfy the shared parser')
        occurrence=datetime.strptime(row['Start-date'],'%m/%d/%Y').strftime('%Y-%m-%d')
        if fragment!='tc-start='+occurrence:raise SourceEventIncomplete('Triple Crown occurrence changed after discovery')
        price=details.get('Price','')
        ages=age_tokens(eligibility)
        if not ages:raise SourceEventIncomplete('No explicit Triple Crown U divisions')
        for index in range(1,14):
            row[f'Age-groups.{index}']=ages[index-1] if index<=len(ages) else ''
            row[f'Entry-fee.{index}']=source_amount(price) if index<=len(ages) else ''
        games=re.search(r'\b(\d)[- ]Game\b',details.get('Format',''),re.I)
        if games:row['Guaranteed-games']=games[1]
        if re.search(r'\d\s*[-–]\s*\$?\d',price):
            for index in range(1,14):row[f'Entry-fee.{index}']=''
            row['More-information-and-additional-tournament-locations']+=' | Source price range: '+price
        row['More-information-and-additional-tournament-locations']+=' | Source eligibility: '+details.get('Division','')
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(occurrence_start=occurrence,source_fields=details,
            identity_basis='family URL plus explicit source occurrence start; date correction may require reconciliation',
            data_gaps=['Per-division fees not mapped from a price range','External venue/registration enrichment not yet verified'])
        sections=[]
        for block in soup.select('.event-info-accordion'):
            heading=block.select_one('.dropdown-toggle-event-info')
            content=block.select_one('.event-info-details')
            if heading and content and heading.get_text(' ',strip=True)=='Event Details':
                sections.append(str(content))
        if sections:
            rich=description(''.join(sections),base_url=public)
            self.source_details[url]['description']=rich
            row['More-information-and-additional-tournament-locations']=rich['text']+'\n\n'+row['More-information-and-additional-tournament-locations']
        return row
