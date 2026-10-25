"""Preliminary AllianceFastpitch adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
from common.collection import CollectedLinks, validated_records
from common.identity import normalize_url
from common.sport import payload_revision
from common.softball_scaffold import fetch_listing,SoftballNotReady
from common.softball_rows import make_row,city_state,source_address
from common.source_exclusions import SourceEventIncomplete
from scrapers.triplecrown_scraper import _parse_month_day_range
from bs4 import BeautifulSoup
import json
import re
from urllib.parse import urlsplit, urlunsplit,urldefrag,parse_qs


class AllianceFastpitchScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "AllianceFastpitch"
    DISCOVERY_URL = "https://thealliancefastpitch.com/tournaments/"
    REQUEST_URL = ('https://thealliancefastpitch.com/wp-admin/admin-ajax.php?'
        'page=1&limit=25&season=2027&age_division=All&type=upcoming&league_division=Super&'
        'sort_by=dates&sort_direction=asc&env=prod&action=wsr_get_events_data')
    CONTENT_TYPE = 'json'
    BLOCKER = "Two external layouts verified; zero listing IDs, other hosts, fee packages and complete pagination remain"
    EVENT_HOST = ""
    EVENT_PATH = ""
    EVENT_QUERY_ID = ""
    DISCOVERY_SELECTOR = "api"
    REUSE = ""

    def get_event_identity(self, url):
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or not re.fullmatch(r'alliance-event=[1-9]\d*', parsed.fragment)):
            raise ValueError('Alliance event needs a published nonzero ID and HTTPS detail URL')
        return parsed.fragment.split('=', 1)[1]

    def parse_listing(self, html, base_url, *, max_events=5):
        if not 1 <= max_events <= 25:
            raise ValueError('Discovery sample limit must be 1..25')
        payload = json.loads(html)
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise ValueError('Alliance proxy did not confirm success')
        data = payload.get('data')
        if not isinstance(data, dict) or data.get('stale') is not False:
            raise ValueError('Alliance proxy is stale or has unknown freshness')
        total = data.get('totalRecords')
        if type(total) is not int or total < 0:
            raise ValueError('Alliance proxy has no verified totalRecords count')
        rows = validated_records(data, field='events', source='Alliance proxy')
        if total < len(rows) or len(rows) > 25 or (total and not rows):
            raise ValueError('Alliance pagination counts are inconsistent')
        links, ids, unresolved = [], {}, 0
        revisions = {}
        # Validate the entire bounded page, even when only returning a few IDs.
        for row in rows:
            identity = row.get('Event ID')
            if type(identity) is not int or identity < 0:
                raise ValueError('Alliance Event ID has an unverified type')
            if identity == 0:
                unresolved += 1
                continue
            anchors = BeautifulSoup(row.get('event_name', ''), 'html.parser').select('a[href]')
            if len(anchors) != 1:
                raise ValueError('Alliance event has no unique detail link')
            public_url = anchors[0]['href']
            parsed = urlsplit(public_url)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query,
                              f'alliance-event={identity}'))
            self.get_event_identity(url)
            if identity in ids and normalize_url(public_url) != ids[identity]:
                raise ValueError('Alliance repeated an ID with conflicting detail links')
            ids[identity] = normalize_url(public_url)
            if url not in links and len(links) < max_events:
                links.append(url)
                revisions[url] = payload_revision({field: row.get(field) for field in
                    ('dates', 'event_name', 'city', 'state', 'age_divisions', 'event_type', 'event_host')})
        self.discovery_revisions = revisions
        result = CollectedLinks(links, complete=False, pages=1,
            error=f'One season/league page only; {unresolved} rows have unresolved zero IDs; foreign details unsupported')
        result.unresolved_identities = unresolved
        result.source_total = total
        return result

    def extract_event_details(self,url,driver=None,**kwargs):
        self.get_event_identity(url)
        public,_=urldefrag(url)
        parsed=urlsplit(public)
        connect=parsed.hostname=='www.connectsportsevents.com' and re.fullmatch(r'/ray-seymour-invitational-20\d{2}/',parsed.path)
        tourney=parsed.hostname=='tourneymachine.com' and parsed.path=='/Public/Results/Tournament.aspx'
        if not connect and not tourney:
            listing=getattr(self,'_listing_rows',{}).get(url)
            if listing:
                # The national proxy supplies these required event facts itself.
                # Do not manufacture registration/contact/fee details or scrape
                # an unrelated external layout to make a valid source row exist.
                title=BeautifulSoup(listing.get('event_name',''),'html.parser').get_text(' ',strip=True)
                date_label=listing.get('dates','')
                if not re.search(r'\b20\d{2}\b',date_label):raise SourceEventIncomplete('Alliance listing requires an explicit year')
                start,end=_parse_month_day_range(date_label,None)
                division={'label':listing.get('age_divisions','')}
                self.source_details=getattr(self,'source_details',{})
                self.source_details[url]=dict(source_id=self.get_event_identity(url),source_url=self.DISCOVERY_URL,
                    public_url=public,source_dates=date_label,divisions=[division],source_organization=listing.get('event_host'),
                    collection_basis='Published Alliance national tournament proxy row',
                    data_gaps=['Fees, contact, registration status and venue addresses not supplied by the national listing; external layout unverified'])
                return make_row(title=title,start=start,end=end,url=public,state=listing.get('state',''),city=listing.get('city',''),
                    divisions=[division],notes='Source event host: '+str(listing.get('event_host',''))+'\nSource event type: '+str(listing.get('event_type','')))
            raise SoftballNotReady('Alliance external detail host/layout has not been verified: '+public)
        soup=BeautifulSoup(fetch_listing(public),'html.parser')
        locations=[];organizer={};notes='';hotel='NA';rich=None
        if connect:
            title=soup.select_one('.section-heading h1')
            def labelled(label):
                heading=soup.find('h5',string=lambda value:value and value.strip()==label)
                # The source has a malformed DATES h5 enclosing later headings.
                if not heading:
                    heading=next((node for node in soup.select('h5') if node.find(string=lambda v:v and v.strip()==label)),None)
                return (heading.find('p') or heading.find_next_sibling('p')) if heading else None
            dates=labelled('DATES');where=labelled('WHERE');who=labelled('WHO');pricing=labelled('Pricing')
            if not all([title,dates,where,who]):raise SourceEventIncomplete('Connect Sports labelled event layout changed')
            for sup in dates.select('sup'):sup.decompose()
            raw_date=dates.get_text(' ',strip=True)
            match=re.search(r'([A-Za-z]+)\s+(\d{1,2})\s+to\s+(?:[A-Za-z]+,?\s+)?([A-Za-z]+)\s+(\d{1,2})\s*,\s*(20\d{2})',raw_date)
            if not match or match[1]!=match[3]:raise SourceEventIncomplete('Connect Sports date range is unrecognized')
            date_label=f'{match[1]} {match[2]}-{match[4]}, {match[5]}'
            place=where.find('strong')
            city,state=city_state(place.get_text(' ',strip=True) if place else '')
            divisions=[dict(label=who.get_text(' ',strip=True))]
            directors=labelled('Tournament Director(s)')
            if directors:
                contacts=directors.select('a[href^="mailto:"]')
                if contacts:organizer=dict(name='; '.join(a.get_text(' ',strip=True) for a in contacts),email=contacts[0]['href'][7:])
            notes='Source pricing choices (game-guarantee packages, not age-specific fees): '+(pricing.get_text(' ',strip=True) if pricing else 'not published')
            if pricing:
                from common.softball_description import description
                rich=description(str(pricing),base_url=public)
                notes='Source pricing choices (game-guarantee packages, not age-specific fees):\n'+rich['text']
            for heading in soup.select('h3,h4,h5'):
                name=heading.get_text(' ',strip=True)
                if len(name)<100 and re.search(r'\b(?:Complex|Park)\b',name):locations.append(dict(venue=name))
            hotel='Yes' if 'Stay & Play' in soup.get_text(' ',strip=True) else 'NA'
            gaps=['Game-guarantee price choices require a package-aware mapping; entry fee left blank',
                  'Park names published without street addresses in this response']
        else:
            title=soup.select_one('#tournamentHeader1_ui_Tournament_HyperLink')
            dates=soup.select_one('.tournamentDates')
            where=soup.select_one('.tournamentLocation')
            if not all([title,dates,where]):raise SourceEventIncomplete('TourneyMachine public event header is missing')
            if parse_qs(urlsplit(title.get('href','')).query).get('IDTournament')!=parse_qs(parsed.query).get('IDTournament'):
                raise SourceEventIncomplete('TourneyMachine source tournament identity disagrees')
            date_label=dates.get_text(' ',strip=True)
            cities=re.findall(r'([^,\u2022]+),\s*([A-Z]{2})(?=\s|$)',where.get_text(' ',strip=True))
            if not cities:raise SourceEventIncomplete('TourneyMachine has no explicit US location')
            city,state=city_state(', '.join(cities[0]))
            divisions=[dict(label=node.div.get_text(' ',strip=True)) for node in soup.select('a.tournamentDivision') if node.div]
            for block in soup.select('.js-list-group-item-place'):
                detail=block.find_next_sibling('div',class_='list-group-item-dropdown')
                address=detail.find('address') if detail else None
                name=block.find('h4')
                if address and name:locations.append(source_address(name.get_text(' ',strip=True),address.get_text('\n',strip=True),state=state))
            notes='Source location label: '+where.get_text(' ',strip=True)
            gaps=['Entry fees and organizer contact are not published in the inspected schedule page; platform support contact excluded']
        if not re.search(r'\b20\d{2}\b',date_label):raise SourceEventIncomplete('Alliance detail requires an explicit year')
        start,end=_parse_month_day_range(date_label,None)
        self.source_details=getattr(self,'source_details',{})
        self.source_details[url]=dict(public_url=public,source_dates=date_label,divisions=divisions,locations=locations,data_gaps=gaps)
        if rich:self.source_details[url]['description']=rich
        return make_row(title=title.get_text(' ',strip=True),start=start,end=end,url=public,state=state,city=city,
            divisions=divisions,locations=locations,organizer=organizer,notes=notes,hotel=hotel)
