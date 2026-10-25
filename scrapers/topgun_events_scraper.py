"""Preliminary TopGunEvents adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.softball_scaffold import fetch_listing
from common.softball_rows import make_row,city_state,source_address
from common.softball_rows import age_tokens
from common.softball_description import description
from common.collection import CollectedLinks
from common.source_exclusions import SourceEventIncomplete
from scrapers.triplecrown_scraper import _parse_month_day_range,_extract_emails
from bs4 import BeautifulSoup
import re


class TopGunEventsScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "TopGunEvents"
    DISCOVERY_URL = "https://topgunevents.com/tournaments/"
    REQUEST_URL = 'https://app.tagup.com/topgun/events/'
    BLOCKER = "Current Tagup first-page/detail rows verified; pagination and complete venue reconciliation remain"
    EVENT_HOST = "app.tagup.com"
    EVENT_PATH = "/topgun/event/tournament/(\\d+)/?"
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "a[href*=\"/topgun/event/tournament/\"]"
    REUSE = ""

    def parse_listing(self,html,base_url,*,max_events=5):
        if not 1<=max_events<=25:raise ValueError('Discovery sample limit must be 1..25')
        soup=BeautifulSoup(html,'html.parser')
        cards=soup.select('.tus_efinder_result[data-event_id][data-event_type="tournament"]')
        if not cards:return super().parse_listing(html,base_url,max_events=max_events)
        links=[]
        for card in cards:
            identity=card['data-event_id']
            if not re.fullmatch(r'\d+',identity):raise SourceEventIncomplete('tagup card has no verified tournament ID')
            url=f'https://app.tagup.com/topgun/event/tournament/{identity}/'
            if url not in links:links.append(url)
            if len(links)==max_events:break
        return CollectedLinks(links,complete=False,pages=1,error='Current tagup first-page sample; page continuation and all divisions remain unverified')

    def extract_event_details(self,url,driver=None,**kwargs):
        self.get_event_identity(url)
        soup=BeautifulSoup(fetch_listing(url),'html.parser')
        title=soup.select_one('.tus_se_head_bar_content_title')
        spans=soup.select('.tus_se_head_bar_content_subtitle .tus_efinder_result_span')
        body=soup.select_one('.tus_se_info_wrap')
        if not title or len(spans)<2 or not body:raise SourceEventIncomplete('Top Gun Events detail layout not recognized')
        dates=spans[0].get_text(' ',strip=True)
        if not re.search(r'\b20\d{2}\b',dates):raise SourceEventIncomplete('Top Gun Events needs explicit occurrence year')
        start,end=_parse_month_day_range(dates,None)
        city,state=city_state(spans[1].get_text(' ',strip=True))
        text=body.get_text(' ',strip=True)
        fields={}
        nodes={}
        for heading in body.select('.tus_se_detail_title'):
            block=heading.parent.parent
            value=block.select_one('.tus_se_detail_content')
            if value:
                key=heading.get_text(' ',strip=True)
                fields[key]=value.get_text(' ',strip=True);nodes[key]=value
        ages=re.search(r'Divisions:\s*((?:\d{1,2}u\s*,?\s*)+)',text,re.I)
        fee=re.search(r'Tournament Entry:\s*\$(\d+(?:\.\d{2})?)\s+per team',text,re.I)
        age_label=fields.get('Divisions') or (ages[1] if ages else '')
        if not age_tokens(age_label):raise SourceEventIncomplete('Top Gun Events has no verified team divisions')
        partitions=[dict(label=match[1],entry_fee=match[2],fee_label=match[0]) for match in
                    re.finditer(r'\b(\d{1,2}u)\s*:\s*\$([\d,]+(?:\.\d{2})?)',text,re.I)]
        event_fee=re.fullmatch(r'\$([\d,]+(?:\.\d{2})?)\s*/team',fields.get('Event Fee',''))
        if not partitions and event_fee:partitions=[dict(label=age_label,entry_fee=event_fee[1],fee_label=fields['Event Fee'])]
        if not partitions and fee:partitions=[dict(label=age_label,entry_fee=fee[1],fee_label=fee[0])]
        partitions=[dict(values) for values in {tuple(part.items()):part for part in partitions}.values()]
        if {age for part in partitions for age in age_tokens(part['label'])}!=set(age_tokens(age_label)):
            raise SourceEventIncomplete('Top Gun Events source fee partitions do not cover the published divisions')
        venues=[]
        for block in soup.select('.tus_venue_block'):
            name=block.select_one('.tus_venue_block_title');address=block.select_one('.tus_venue_block_address')
            if name and address:
                venues.append(source_address(name.get_text(' ',strip=True),address.get_text(' ',strip=True),state=state))
        if not state and venues:state=venues[0].get('state','')
        contact_node=body.find(string=lambda value:value and 'Contact Us:' in value)
        emails=_extract_emails(nodes['Contact Email']) if 'Contact Email' in nodes else []
        if not emails and contact_node:emails=_extract_emails(contact_node.parent.parent)
        games=re.search(r'\b(\d) game guarantee',text,re.I) or re.search(r'Game Guarantee:\s*(\d) games',text,re.I)
        rich=description(str(body),base_url=url)
        notes=rich['text']
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(divisions=partitions,locations=venues,description=rich,
            source_fields=fields,data_gaps=['Current tagup pagination and complete provider coverage remain unverified',
                'Venue names in prose may exceed the published address blocks; reconcile before delivery'])
        return make_row(title=title.get_text(' ',strip=True),start=start,end=end,url=url,state=state,city=city,
            divisions=partitions,locations=venues,
            organizer=dict(email=emails[0] if emails else '',phone=fields.get('Contact Phone','')),
            guaranteed_games=fields.get('Game Guarantee') or (games[1] if games else ''),notes=notes,
            hotel='Yes' if 'stay to play' in text.lower() else 'NA')
