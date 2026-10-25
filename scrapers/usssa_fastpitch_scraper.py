"""Preliminary USSSAFastpitch adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.softball_scaffold import fetch_listing
from common.softball_structured import sports_events
from common.softball_rows import make_row
from common.softball_rows import source_amount
from common.softball_description import description
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from scrapers.usssa_scraper import USSSAScraper
from bs4 import BeautifulSoup
from urllib.parse import urlsplit
import json
import re


class USSSAFastpitchScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "USSSAFastpitch"
    DISCOVERY_URL = "https://www.usssa.com/fastpitch_events"
    REQUEST_URL = 'https://txfastpitch.usssa.com/events/'
    BLOCKER = "TX detail/division POST verified; registration/schema conflicts, stable occurrence identity and state coverage remain"
    EVENT_HOST = "txfastpitch.usssa.com"
    EVENT_PATH = "/event/([^/]+)/?"
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = 'a[href*="/event/"]'
    REUSE = "scrapers.usssa_scraper.USSSAScraper"

    def get_event_identity(self,url):
        from us import states
        parsed=urlsplit(url)
        host=re.fullmatch(r'([a-z]{2})fastpitch\.usssa\.com',parsed.hostname or '')
        path=re.fullmatch(self.EVENT_PATH,parsed.path)
        if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port
                or not host or not states.lookup(host[1].upper()) or not path or parsed.query):
            raise ValueError('Unverified USSSA fastpitch state event URL')
        # Preserve historical Texas identities; namespace newly supported states.
        return path[1] if host[1]=='tx' else host[1]+'|'+path[1]

    def extract_event_details(self,url,driver=None,**kwargs):
        self.get_event_identity(url)
        soup=BeautifulSoup(fetch_listing(url),'html.parser')
        events=sports_events(soup)
        if len(events)!=1:raise SourceEventIncomplete('USSSA page needs one explicit event date schema')
        event=events[0]
        if 'Cancelled' in str(event.get('eventStatus','')):
            self.source_details=getattr(self,'source_details',{})
            self.source_details[url]={'cancelled':True,'event_schema':event}
            raise SourceEventOutOfScope('USSSA source marks event cancelled')
        fields={}
        for block in soup.select('._item_info'):
            label=block.select_one('._item_name');value=block.select_one('._item_text')
            if label and value:fields[label.get_text(' ',strip=True)]=value.get_text(' ',strip=True)
        if fields.get('Stature') in ('League','Individual Event'):
            raise SourceEventOutOfScope('USSSA source stature is '+fields['Stature'])
        name=soup.select_one('h3._heading')
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(schema_title=event.get('name'),
            visible_title=name.get_text(' ',strip=True) if name else '',
            schema_registration_url=event.get('offers',{}).get('url'))
        if name is None or name.get_text(' ',strip=True)!=event.get('name'):
            raise SourceEventIncomplete('USSSA visible title and event schema conflict')
        # The event schema's registration URL can incorrectly say baseball; use
        # the page's actual division request fields, never that cross-sport URL.
        params=None
        for script in soup.select('script'):
            match=re.search(r'var script_parameter\s*=\s*(\{.*?\});',script.get_text(),re.S)
            if match:params=json.loads(match[1]);break
        if not params or params.get('ajax_url')!=f'https://{urlsplit(url).hostname}/wp-admin/admin-ajax.php':
            raise SourceEventIncomplete('USSSA division endpoint/nonce is not the observed site contract')
        data={'action':'fetch_event_divisions_ajax','nonce':params.get('nonce','')}
        for field,selector in [('event_id','#event_current_id'),('lwc_display','#event_lwc_display'),
                               ('lwc_option','#event_lwc_option'),('wp_event_id','#wp_event_pid'),('wp_siteid','#event_siteid')]:
            element=soup.select_one(selector)
            if element is None:raise SourceEventIncomplete('USSSA division request fields are missing')
            data[field]=element.get('value','')
        response=json.loads(fetch_listing(params['ajax_url'],content_type='json',method='POST',data=data))
        if not isinstance(response,dict) or response.get('status') is not True or not isinstance(response.get('result'),str):
            raise SourceEventIncomplete('USSSA division read did not confirm success')
        divisions=[];games=set()
        rows=BeautifulSoup('<table>'+response['result']+'</table>','html.parser').select('tr')
        for row in rows:
            cells=row.find_all('td',recursive=False)
            if len(cells)<2:continue
            label=cells[0].get_text(' ',strip=True)
            age=re.match(r'(\d{1,2})\s*&\s*Under\b',label,re.I)
            high_school=re.match(r'(?:High School|HS)\b',label,re.I)
            if not age and not high_school:raise SourceEventIncomplete('USSSA division has unsupported labelled eligibility')
            fee=cells[1].get_text(' ',strip=True)
            competition=re.search(r'\b(Open|A|B|C)\b',label,re.I)
            divisions.append(dict(label=label,**({'age':age[1]} if age else {}),entry_fee=fee,fee_label=fee,
                competition_class=competition[1] if competition else None,
                pricing={'base_fee':source_amount(fee),'currency':'USD' if event.get('offers',{}).get('priceCurrency')=='USD' else None}))
            gg=row.select_one('.game-guarantee-td')
            if gg:games.add(gg.get_text(' ',strip=True))
        if not divisions:raise SourceEventIncomplete('USSSA published no readable division rows')
        mapped_divisions=divisions
        helper=USSSAScraper()
        venues=[]
        for tr in soup.select('#list_view_wrapper tbody tr'):
            cells=tr.find_all('td')
            if len(cells)>=2:
                place=helper._parse_venue_address(cells[0].get_text(' ',strip=True),cells[1].get_text(' ',strip=True))
                venues.append(dict(venue=place.get('Venue',''),address=place.get('Address',''),city=place.get('City',''),
                                   state=place.get('State',''),zip=place.get('Zip Code','')))
        if not venues:raise SourceEventIncomplete('USSSA venue/state table is absent')
        contact=soup.select_one('div._winfo')
        def text(selector):
            node=contact.select_one(selector) if contact else None
            return node.get_text(' ',strip=True) if node else ''
        email=contact.select_one('a[href^="mailto:"]') if contact else None
        phone=contact.select_one('a[href^="tel:"]') if contact else None
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(divisions=divisions,locations=venues,
            source_fields=fields,
            source_event_id=data['event_id'],schema_registration_url=event.get('offers',{}).get('url'),
            data_gaps=['Date/schema and visible registration routes need cross-sport consistency review before production'])
        if event.get('description'):
            self.source_details[url]['description']=description(event['description'],base_url=url)
        return make_row(title=event['name'],start=event.get('startDate'),end=event.get('endDate'),url=url,
            state=venues[0]['state'],city=venues[0]['city'],locations=venues,divisions=mapped_divisions,
            organizer=dict(name=text('h3'),email=email['href'][7:] if email else '',phone=phone['href'][4:] if phone else ''),
            guaranteed_games=next(iter(games)) if len(games)==1 else '',
            notes='Division labels and source registration inconsistency retained in source details'
                + ('\n\nSchool divisions retained without invented U ages: '+ '; '.join(division['label']+': '+division['fee_label'] for division in divisions if not division.get('age')) if len(mapped_divisions)!=len(divisions) else ''))
