"""Observed RegisterPlay JSON contracts, scoped to one tenant per adapter.

Discovery identities and raw detail inspection are separate from ingestion rows.
Unknown tournament eligibility never enables detail extraction automatically.
"""
import json
import re

from common.collection import CollectedLinks, validated_records
from common.softball_scaffold import SoftballScaffoldScraper, fetch_listing
from common.sport import payload_revision
from common.softball_rows import make_row,city_state,source_state
from common.softball_rows import source_date
from datetime import datetime
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from bs4 import BeautifulSoup
from urllib.parse import quote
from common.softball_description import description, public_url
from common.softball_registerplay_metadata import division_metadata, division_label


class RegisterPlaySoftballScraper(SoftballScaffoldScraper):
    CONTENT_TYPE = 'json'
    DISCOVERY_SELECTOR = 'api'  # Existing discovery capability flag; not a CSS selector.
    EVENT_PATH = r'/pages/event/detail/(\d+)/?'
    OTHER_SPORTS = ()
    DETAIL_API_VERIFIED = False
    DETAIL_INSPECTION_REQUESTS = 2
    SEARCH_API_VERIFIED = False
    DIRECTOR_FORM_VERIFIED = False
    DIRECTIONS_TEMPLATE_VERIFIED = False
    SEARCH_STATE_ALIASES = {}
    SEARCH_MULTI_STATE_SCOPES = {}

    def scope_states(self,scope):
        from us import states
        if scope in self.SEARCH_MULTI_STATE_SCOPES:
            return self.SEARCH_MULTI_STATE_SCOPES[scope] or tuple([s.abbr for s in states.STATES]+['DC'])
        return (self.SEARCH_STATE_ALIASES.get(scope,scope),)

    def configure_search(self,*,state,period):
        from us import states
        if not self.SEARCH_API_VERIFIED:raise NotImplementedError('Search POST not verified on this tenant')
        physical=self.scope_states(state)[0]
        selected=states.DC if physical=='DC' else states.lookup(physical or '')
        if not selected or selected.abbr!=physical or not re.fullmatch(r'd\|(?:[1-9]|1[012])\|20\d{2}',period or ''):
            raise ValueError('Search requires a US state abbreviation and observed begin-period d|month|year')
        self.discovery_filters=dict(stateID=state,sportID='FP',classID='',subDirID='',keyword='',period=period,defaultSearch=False)

    def collect_event_links(self,driver,base_url,**kwargs):
        filters=getattr(self,'discovery_filters',None)
        if not filters:return super().collect_event_links(driver,base_url,**kwargs)
        if base_url!=self.DISCOVERY_URL:raise ValueError('Search uses the configured tenant listing')
        endpoint=f'https://{self.EVENT_HOST}/api/v1/Event/search'
        raw=fetch_listing(endpoint,content_type='json',method='POST',json_body=filters)
        source=validated_records(json.loads(raw),source='RegisterPlay search')
        selected=[];unresolved=[]
        _,month,year=filters['period'].split('|')
        begin=datetime(int(year),int(month),1)
        for record in source:
            if record.get('sport') not in ('FP',*self.OTHER_SPORTS):raise ValueError('Search contains an unverified sport')
            if record.get('sport')!='FP':continue
            city,state=self.location_parts(record.get('location'))
            if not city or city.lower() in {'tbd','unknown','various','varies','multiple locations'} or not state:
                unresolved.append(dict(source_id=record.get('_key'),reason='No usable source city/state in discovery'))
                continue
            if state not in {source_state(code) for code in self.scope_states(filters['stateID'])}:
                unresolved.append(dict(source_id=record.get('_key'),reason='Source location differs from the selected state'))
                continue
            if datetime.strptime(source_date(record.get('starts')),'%m/%d/%Y')<begin:continue
            selected.append(record)
        links=self.parse_listing(json.dumps(selected),base_url,max_events=kwargs.get('max_events',5))
        links.search_filters=dict(filters)
        links.source_total=len(source)
        links.unresolved_locations=unresolved
        links.error='Bounded state/begin-period search; UI pagination is client-side; no nationwide coverage claim'
        return links

    def location_parts(self,label):
        raw=str(label or '').strip()
        for scope,physical in self.SEARCH_STATE_ALIASES.items():
            if raw.endswith(', '+scope):raw=raw[:-len(scope)]+physical
        return city_state(raw)

    def parse_listing(self, html, base_url, *, max_events=5):
        if not 1 <= max_events <= 25:
            raise ValueError('Discovery sample limit must be 1..25')
        records = validated_records(json.loads(html), source=f'{self.SCRAPER_NAME} events')
        self.discovery_revisions.clear()
        links, excluded = [], 0
        for record in records:
            if record.get('sport') in self.OTHER_SPORTS:
                excluded += 1
                continue
            if record.get('sport') != 'FP':
                raise ValueError('Event feed contains unverified sport')
            key = record.get('_key')
            if not isinstance(key, str) or not re.fullmatch(r'\d+', key):
                raise ValueError('Event has no verified _key')
            url = f'https://{self.EVENT_HOST}/pages/event/detail/{key}'
            if url not in links:
                links.append(url)
                self.discovery_revisions[url] = payload_revision({field: record.get(field) for field in
                    ('name', 'starts', 'ends', 'location', 'entryFee', 'competitionClassesList', 'cancelled', 'season')})
            if len(links) == max_events:
                break
        result = CollectedLinks(links, complete=False, pages=1,
            error='Active feed sample only; season coverage, pagination and details are not certified')
        result.excluded_other_sport = excluded
        return result

    def detail_request_urls(self, url):
        key = self.get_event_identity(url)
        if not self.DETAIL_API_VERIFIED:
            raise NotImplementedError(f'{self.SCRAPER_NAME}: detail API has not been observed for this tenant')
        return {name: f'https://{self.EVENT_HOST}/api/v1/EP/{route}/{key}' for name, route in
                (('header', 'header4EP'), ('info', 'info-v3.0/desktop'))}

    def inspect_event(self, url):
        """Two independent GETs for one observed event; never returns an import row."""
        urls = self.detail_request_urls(url)
        payloads = {name: json.loads(fetch_listing(endpoint, content_type='json'))
                    for name, endpoint in urls.items()}
        return self.inspect_detail_payloads(url, **payloads)

    def inspect_detail_payloads(self, url, *, header, info):
        key = self.get_event_identity(url)
        if not isinstance(header, dict) or not isinstance(info, dict):
            raise ValueError('RegisterPlay detail responses must be objects')
        if str(header.get('_key')) != key or header.get('sport') != 'FP':
            raise ValueError('RegisterPlay detail identity/sport mismatch')
        event_info = info.get('eventInfo')
        if not isinstance(event_info, dict):
            raise ValueError('RegisterPlay detail has no eventInfo object')
        divisions = validated_records(info, field='divisions', source='RegisterPlay details')
        facilities = validated_records(info, field='facilities', source='RegisterPlay details')
        if not header.get('name') or not event_info.get('starts') or not event_info.get('ends'):
            raise ValueError('RegisterPlay detail lacks source title or explicit dates')
        division_fields = ('_key', 'name', 'age', 'starts', 'ends', 'fee', 'divisionFee',
                           'minGames', 'individual', 'divisionType', 'enterByCutDate',
                           'showEnter')
        observed_divisions = []
        for item in divisions:
            observed = {field: item.get(field) for field in division_fields}
            observed.update(division_metadata(item, cc_fee_extra=event_info.get('ccFeeExtra')))
            # Keep the legacy key, with the same restricted public values.
            observed['waitlist'] = observed['registration']['waitlist']
            observed_divisions.append(observed)
        rich = description(event_info.get('info'), base_url=url)
        public_facilities=[]
        for facility in facilities:
            # Retain the public venue and its division association, excluding
            # scheduler grids and other unrelated nested source material.
            place={field:facility.get(field) for field in
                ('_key','name','address','location','city','state','zip','coordinate') if field in facility}
            place['divisions']=[{field:item.get(field) for field in ('_key','name')}
                for item in (facility.get('divisions') or []) if isinstance(item,dict)]
            if self.DIRECTIONS_TEMPLATE_VERIFIED and all(place.get(field) for field in ('name','address','location')):
                query=' '.join(str(place.get(field) or '') for field in ('name','address','location','zip')).strip()
                place['directions_url']='https://www.google.com/maps/search/'+quote(query,safe=',')
                place['directions_url_origin']='verified_provider_ui_template'
            public_facilities.append(place)
        return dict(source_id=key, source_url=url, sport='softball',
                    title=header['name'], start=event_info['starts'], end=event_info['ends'],
                    state=event_info.get('state'), location=header.get('location'),
                    is_youth=header.get('isYouth'), is_league=header.get('isLeague'),
                    has_team_divisions=header.get('hasTeamDivisions'),
                    has_individual_divisions=header.get('hasIndividualDivisions'),
                    cancelled=header.get('cancelled'),
                    divisions=observed_divisions,
                    facilities=public_facilities,
                    additional_information_html=rich['html'], description=rich,
                    description_media_omitted=bool(BeautifulSoup(event_info.get('info') or '', 'html.parser').select('img,iframe,object,embed,video')),
                    credit_card_fee_extra=event_info.get('ccFeeExtra'),
                    organizer_name=header.get('dirName',''),
                    source_organization=header.get('site'),
                    season=header.get('season'),
                    program='HPP' if any('HPP' in str(item.get('divisionType') or '')
                                        or item.get('name') == 'HPP' for item in divisions) else None,
                    ingestion_ready=False)

    def extract_event_details(self,url,driver=None,**kwargs):
        return self.extract_observation(url,self.inspect_event(url))

    def extract_observation(self,url,details):
        """Normalize an independently verified observation, including offline replay."""
        if not hasattr(self,'source_details'):self.source_details={}
        self.source_details[url]=details
        if details.get('cancelled') is True:
            raise SourceEventOutOfScope('Cancelled by source')
        if (details.get('is_league') is True or details.get('has_team_divisions') is False
                or details.get('has_individual_divisions') is True):
            raise SourceEventOutOfScope('Outside team tournament scope: source marks league/individual/non-team eligibility')
        if details.get('is_youth') is False:
            raise SourceEventOutOfScope('Source does not verify youth eligibility')
        if any(details.get(key) is not expected for key,expected in
               [('is_league',False),('has_team_divisions',True),('has_individual_divisions',False),('is_youth',True)]):
            raise SourceEventIncomplete('Source eligibility flags are missing or unknown')
        city,location_state=self.location_parts(details.get('location'))
        raw_state=details.get('state') or location_state
        state=source_state(location_state if raw_state in self.SEARCH_MULTI_STATE_SCOPES else self.SEARCH_STATE_ALIASES.get(raw_state,raw_state))
        locations=[]
        for facility in details['facilities']:
            facility_city,facility_state=self.location_parts(facility.get('location'))
            locations.append(dict(venue=facility.get('name',''),address=facility.get('address',''),
                city=facility_city,state=facility_state or state,zip=facility.get('zip','')))
        divisions=[dict(label=item.get('name',''),age=item.get('age'),
            age_labels=division_label(item.get('name'))[0],
            entry_fee=item.get('divisionFee'),
            fee_label=f"base={item.get('divisionFee')}; advertised/card-inclusive={item.get('fee')}; type={item.get('divisionType')}")
                   for item in details['divisions'] if item.get('individual') is not True]
        rich=details.get('description') or description(details.get('additional_information_html',''),base_url=url)
        details['description']=rich
        notes=rich['text']
        notes+='\n\nPublished credit-card-fee-extra flag: '+str(details.get('credit_card_fee_extra'))
        games={item.get('minGames') for item in details['divisions']}
        guaranteed=next(iter(games)) if len(games)==1 else ''
        soup=BeautifulSoup(details.get('additional_information_html',''),'html.parser')
        emails=[anchor['href'].removeprefix('mailto:') for anchor in soup.select('a[href^="mailto:"]')]
        details['organizer_contact']={'email_published':bool(emails), 'source_url':url,
            'urls':list(dict.fromkeys(public_url(anchor.get('href'),url) for anchor in soup.select('a[href]')
                if public_url(anchor.get('href'),url) and re.search(r'contact|register|sign.?up',anchor.get_text(' ',strip=True),re.I)))}
        if self.DIRECTOR_FORM_VERIFIED and details.get('organizer_name'):
            details['organizer_contact']['form']={'page_url':url,'action_label':'Reach out to Event Director',
                'method':'source_page_dialog','direct_url':None}
        registration_notes=[]
        for item in details['divisions']:
            registration=item.get('registration',{})
            facts=[]
            if registration.get('deadline_label'):
                facts.append('deadline='+str(registration['deadline_label']))
            if registration.get('message'):
                facts.append('source action='+registration['message'])
            if registration.get('status') not in (None,'unknown'):
                facts.append('status='+registration['status'])
            if registration.get('spots_remaining') is not None:
                facts.append('remaining spots='+str(registration['spots_remaining']))
            if facts:registration_notes.append(str(item.get('name',''))+': '+'; '.join(facts))
        if registration_notes:notes+='\n\nSource registration:\n'+'\n'.join(registration_notes)
        gaps=[]
        if details.get('description_media_omitted'):gaps.append('Source description contains media; flyer/image-only details require inspection on the original event page')
        if not locations:gaps.append('venue not published in inspected info response')
        if not emails:gaps.append('organizer email not published in inspected info response'
            + ('; contact form available on official event page' if details['organizer_contact'].get('form') else ''))
        details['data_gaps']=gaps
        return make_row(title=details['title'],start=details['start'],end=details['end'],url=url,
            state=state,city=city,locations=locations,divisions=divisions,
            organizer={'name':details.get('organizer_name',''),'email':emails[0] if emails else ''},
            notes=notes,guaranteed_games=guaranteed)
