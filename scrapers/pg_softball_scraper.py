"""Preliminary PGSoftball adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper, SoftballNotReady, fetch_listing
from urllib.parse import parse_qs, urlsplit
from scrapers.pg_scraper import PGScraper
from bs4 import BeautifulSoup
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from common.softball_rows import source_amount,city_state,source_state
from common.softball_description import description,public_url
import re


class PGSoftballScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "PGSoftball"
    DISCOVERY_URL = "https://www.perfectgame.org/softball/schedule.aspx"
    BLOCKER = "Seeded HTTP schedule/detail rows verified; filter generation, pagination and division grouping remain"
    EVENT_HOST = "www.perfectgame.org"
    EVENT_PATH = "/Events/Default\\.aspx"
    EVENT_QUERY_ID = "event"
    DISCOVERY_SELECTOR = 'a[href*="Events/Default.aspx?event="]'
    REUSE = "scrapers.pg_scraper.PGScraper"

    def collect_event_links(self, driver, base_url, **kwargs):
        parsed = urlsplit(base_url)
        params = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.scheme != 'https' or parsed.hostname != 'www.perfectgame.org'
                or parsed.username or parsed.password or parsed.path != '/softball/schedule.aspx'
                or parsed.fragment or set(params) != {'ty','fg','dt','cl','dv','st','sp','ep','tm'}
                or any(len(values) != 1 or not values[0] for values in params.values())):
            raise SoftballNotReady('PG needs an exact browser-observed filtered schedule URL; '
                                   'use --listing-url with --live; filters are not generated or guessed')
        return self.parse_listing(fetch_listing(base_url),base_url,max_events=kwargs.get('max_events',5))

    def extract_event_details(self,url,driver=None,**kwargs):
        self.get_event_identity(url)
        soup=BeautifulSoup(fetch_listing(url),'html.parser')
        title=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventNameNew')
        raw_title=title.get_text(' ',strip=True) if title else ''
        if not raw_title:raise SourceEventIncomplete('PG softball detail title is missing')
        if re.search(r'\bleague\b',raw_title,re.I):raise SourceEventOutOfScope('PG source identifies a league rather than a tournament')
        date_node=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_lblEventDateNew, #ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblDatesNew')
        if not re.search(r'\b20\d{2}\b',raw_title+' '+(date_node.get_text(' ',strip=True) if date_node else '')):
            raise SourceEventIncomplete('PG softball has no explicit occurrence year in title/date context')
        helper=PGScraper()
        locations=[]
        parse_locations=helper._parse_locations_page
        def capture_locations(source):
            parsed=parse_locations(source)
            addresses=source.find_all('span',id=re.compile(r'rptLocations_lblBallParkAddresses_\d+'))
            for place,address in zip(parsed.get('locations',[]),addresses):
                link=source.find(id=address['id'].replace('lblBallParkAddresses','hlMaps'))
                if link and public_url(link.get('href'),url):place['directions_url']=public_url(link.get('href'),url)
            locations.extend(parsed.get('locations',[]))
            return parsed
        helper._parse_locations_page=capture_locations
        helper._get_soup=lambda _driver,source_url:BeautifulSoup(fetch_listing(source_url),'html.parser')
        try:
            row=helper._parse_single_event_page(None,url,soup=soup)
        finally:helper.close()
        row['Tournament-type']='Team'
        row['Are-there-multiple_locations']='Yes' if len(locations)>1 else 'No'
        location_label=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_EventHeader1_lblEventLocaGeneral')
        header_city=location_label.select_one('.text-nowrap') if location_label else None
        explicit_city,explicit_state=city_state(header_city.get_text(' ',strip=True)) if header_city else ('','')
        if not row.get('City') and explicit_city:
            if row.get('State-FullName') and source_state(row['State-FullName'])!=explicit_state:
                raise SourceEventIncomplete('PG event-header city/state conflicts with its venue state')
            row['City']=explicit_city;row['State-FullName']=explicit_state
            # Only the venue explicitly named beside this header city inherits
            # it. Never assign a central event city to every additional venue.
            named_venue=location_label.get_text(' ',strip=True).split('|',1)[0].strip()
            for place in locations:
                if not place.get('City') and place.get('Venue','').casefold()==named_venue.casefold():
                    place['City']=explicit_city
        best=next((place for place in locations if place.get('Address')==row.get('Address')),None)
        if best:row['Venue']=best.get('Venue','')
        fee=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_lblEntryFee')
        fee_note=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_lblFeeNote')
        label=fee.get_text(' ',strip=True) if fee else ''
        # A provider-labelled USD suffix is unit information, not a range.
        amount=source_amount(re.sub(r'\s+USD$','',label).strip())
        row['Entry-fee.1']=amount
        notes='Published division label: '+raw_title+' | Source fee: '+label
        if fee_note:notes+=' '+fee_note.get_text(' ',strip=True)
        if locations:notes+=' | Locations: '+' | '.join('; '.join(str(v) for v in place.values()) for place in locations)
        description_node=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_lblEventDescription')
        rich=description(description_node.decode_contents(),base_url=url) if description_node else None
        if rich and rich['text']:notes=rich['text']+'\n\n'+notes
        row['More-information-and-additional-tournament-locations']=notes
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(raw_division_label=raw_title,fee_label=label,locations=locations,
            facilities=[{'name':place.get('Venue',''),'directions_url':place['directions_url']} for place in locations if place.get('directions_url')],
            divisions=[{'label':raw_title,'age_groups':[row['Age-groups.1']] if row.get('Age-groups.1') else [],
                'competition_class':row.get('Competition-level.1'),'entry_fee':amount,'fee_label':label,
                'pricing':{'base_fee':amount,'currency':'USD' if label.endswith(' USD') else None}}],
            source_header_location=location_label.get_text(' ',strip=True) if location_label else None,
            schedule_group_id=getattr(self,'schedule_groups',{}).get(url),
            data_gaps=['Single-division parser reused; grouped division membership still needs verification before production'])
        if rich:self.source_details[url]['description']=rich
        return row
