"""Preliminary USFA adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.softball_scaffold import fetch_listing
from common.softball_structured import sports_events
from common.softball_rows import make_row,age_tokens,source_address
from common.softball_description import description
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from common.collection import CollectedLinks
from scrapers.triplecrown_scraper import _extract_emails
from bs4 import BeautifulSoup
from urllib.parse import parse_qs,urlsplit
import re


class USFAScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "USFA"
    DISCOVERY_URL = "https://usfastpitch.com/tournaments"
    BLOCKER = "State map discovery and pagination need verification; landing page lists only featured championships"
    EVENT_HOST = "usfaevents.com"
    EVENT_PATH = "/(?:TTContent|details)\\.aspx"
    EVENT_QUERY_ID = "tid"
    DISCOVERY_SELECTOR = "a[href*=\"TTContent.aspx?tid=USFA\"]"
    REUSE = ""

    STATE_LISTINGS={'OK':'https://usfaevents.com/eventlist.aspx?tid=USFAOK&tab=3&sub=1&stid=USFAOK&show=F'}

    def collect_event_links(self,driver,base_url,**kwargs):
        if base_url==self.DISCOVERY_URL:return super().collect_event_links(driver,base_url,**kwargs)
        if base_url not in self.STATE_LISTINGS.values():raise ValueError('Unverified USFA state listing')
        soup=BeautifulSoup(fetch_listing(base_url),'html.parser')
        cards=soup.select('.ev-card[data-tid][data-type="event"]')
        links=[]
        for card in cards:
            source=card.select_one('a.ev-name[href]')
            identity=card['data-tid']
            if not source or parse_qs(urlsplit(source['href']).query).get('tid')!=[identity]:
                raise SourceEventIncomplete('USFA state card and published detail ID disagree')
            # The published TTIntro link redirects to this verified details route.
            url=f'https://usfaevents.com/details.aspx?tid={identity}&tab=1'
            self.get_event_identity(url)
            links.append(url)
            if len(links)>=kwargs.get('max_events',5):break
        if not links:raise SourceEventIncomplete('USFA state list has no verified event cards')
        return CollectedLinks(links,complete=False,pages=1,error='One observed state page; season callbacks/continuation and other states remain bounded out')

    def extract_event_details(self,url,driver=None,**kwargs):
        identity=self.get_event_identity(url)
        soup=BeautifulSoup(fetch_listing(url),'html.parser')
        candidates=[event for event in sports_events(soup)
                    if parse_qs(urlsplit(event.get('url','')).query).get('tid')==[identity]]
        if len(candidates)!=1:raise SourceEventIncomplete('USFA event schema is missing or does not match the published tid')
        event=candidates[0]
        if 'Cancelled' in str(event.get('eventStatus','')):raise SourceEventOutOfScope('USFA event cancelled by source')
        text=soup.get_text(' ',strip=True).replace('\xa0',' ')
        divisions=[]
        for match in re.finditer(r'((?:\b\d{1,2}U\s*(?:,|&|and)?\s*)+)\s+ENTRY\s*[-:]\s*\$(\d[\d,]*(?:\.\d{2})?)',text,re.I):
            divisions.append(dict(label=match[1],entry_fee=match[2],fee_label=match[0]))
        if not divisions:
            fee=re.search(r'ENTRY(?: FEE)?\s*[-:]\s*\$(\d[\d,]*(?:\.\d{2})?)',text,re.I)
            ages=re.search(r'DIVISIONS\s*[-:]\s*((?:\d{1,2}U\s*(?:,|&|and)?\s*)+)',text,re.I)
            if fee and ages:divisions=[dict(label=ages[1],entry_fee=fee[1],fee_label=fee[0])]
        if not divisions:
            raise SourceEventIncomplete('USFA fee/age partitions are not recognized; do not assign one fee across divisions')
        covered={age for division in divisions for age in age_tokens(division['label'])}
        published=set(age_tokens(event.get('name','')))
        if not published and divisions:published=set(age_tokens(divisions[0]['label']))
        if not published or covered!=published:
            raise SourceEventIncomplete('USFA source fee partitions do not cover the named ages exactly')
        place=event.get('location',{})
        address=place.get('address',{})
        state=address.get('addressRegion','')
        city=address.get('addressLocality','')
        rich=description(event.get('description'),base_url=url)
        notes=rich['text']+'\n\n'+ '\n'.join(division['fee_label'] for division in divisions)
        games=re.search(r'\b(\d)\s+game\s+guarantee\b',text,re.I)
        venues=[]
        for paragraph in soup.select('p'):
            paragraph_text=paragraph.get_text(' ',strip=True).replace('\xa0',' ').replace('\ufffd',' ')
            if not re.search(r'EVENT LOCATIONS',paragraph_text,re.I):continue
            names=paragraph.find_all('strong')
            for index,node in enumerate(names):
                full_name=node.get_text(' ',strip=True)
                tail=paragraph_text.split(full_name,1)[-1]
                if index+1<len(names):tail=tail.split(names[index+1].get_text(' ',strip=True),1)[0]
                postal=re.search(r'(\d{1,6}[^,]+,\s*[^,]+,\s*[A-Za-z ]+?\s+\d{5}(?:-\d{4})?)',tail)
                if postal:venues.append(source_address(re.sub(r'\s*\(COMPLEX MAP\)$','',full_name,flags=re.I).strip(),postal[1]))
        organizer={}
        director_name=''
        contact_name=''
        for paragraph in soup.select('p'):
            label=paragraph.get_text(' ',strip=True).replace('\xa0',' ').replace('\ufffd',' ')
            match=re.search(r'Tourn\w* Contact:\s*(.+?)\s+(\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})',label,re.I)
            if match:
                contact_name=match[1].strip()
                organizer=dict(name=contact_name,phone=match[2])
            director=re.search(r'Tourn\w* Director:\s*(.+?)\s*-\s*(\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})',label,re.I)
            if director:
                director_name=director[1].strip()
                emails=_extract_emails(paragraph)
                organizer=dict(name=director[1].strip(),phone=director[2],email=emails[0] if emails else '')
        if not hasattr(self,'source_details'):self.source_details={}
        self.source_details[url]=dict(source_id=identity,divisions=divisions,event_schema=event,locations=venues,organizer=organizer,description=rich,
            directorName=director_name,tournamentContactName=contact_name or organizer.get('name',''),
            data_gaps=['Additional Fields/Contact tabs remain unverified'] if not venues else ['Email not published in inspected event description'])
        return make_row(title=event['name'],start=event.get('startDate'),end=event.get('endDate'),url=url,
            state=state,city=city,divisions=divisions,locations=venues,organizer=organizer,notes=notes,guaranteed_games=games[1] if games else '')
