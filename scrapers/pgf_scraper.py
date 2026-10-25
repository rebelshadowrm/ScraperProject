"""Preliminary PGF adapter; see .agents/skills/scraper-daily-run/references/coverage.md for evidence."""
from common.softball_scaffold import SoftballScaffoldScraper
import base64
import binascii
import re
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from bs4 import BeautifulSoup
from common.softball_rows import make_row
from common.source_exclusions import SourceEventIncomplete
from common.softball_tournamentconnect import TC_HOST,TC_LISTING,TC_API,tc_identity,tc_read,collect_current,extract_current


class PGFScraper(SoftballScaffoldScraper):
    SCRAPER_NAME = "PGF"
    DISCOVERY_URL = "https://www.premiergirlsfastpitch.com/regional_championships"
    REQUEST_URL = TC_API
    BLOCKER = "Current public TournamentConnect API verified; broad coverage and source-supported modal deep links remain"
    EVENT_HOST = "www.premiergirlsfastpitch.com"
    EVENT_PATH = r'/sam/bundles/ltd/server/index\.php'
    EVENT_QUERY_ID = "regEventId"
    DISCOVERY_SELECTOR = 'table.ltd-content-table a[href*="regEventId="]'
    DETAIL_HTML_VERIFIED = True
    DETAIL_API_VERIFIED = True
    REUSE = ""

    def collect_event_links(self,driver,base_url,**kwargs):
        if base_url not in (self.DISCOVERY_URL,TC_LISTING):raise ValueError('Use a verified PGF listing')
        return collect_current(self,max_events=kwargs.get('max_events',5),max_pages=kwargs.get('max_pages',1))

    def inspect_event(self,url):
        if urlsplit(url).hostname!=TC_HOST:return super().inspect_event(url)
        identity=tc_identity(url)
        payload=tc_read({'eventid':int(identity[3:])})
        event=payload.get('event',{}) if isinstance(payload,dict) else {}
        if str(event.get('eventid'))!=identity[3:] or event.get('sportid')!=4:raise SourceEventIncomplete('PGF current inspection identity/sport mismatch')
        # Exclude access codes, files and any participant material from inspection.
        return dict(source_id=identity,source_url=url,sport='softball',ingestion_ready=False,
            event={key:event.get(key) for key in ('eventid','name','sportid','ngb_affiliation','startdate','enddate','city','stateabbr','divisionlabel','pricinglabel')},
            divisions=[{key:item.get(key) for key in ('eventdivisionid','division','sportskill','fromDivisionAge','toDivisionAge','price')} for item in payload.get('divisions',[])],
            facilities=[{key:item.get(key) for key in ('name','physical_address','physical_city','physical_state','physical_zip')} for item in payload.get('facilities',[])])

    def get_event_identity(self, url):
        if urlsplit(url).hostname==TC_HOST:return tc_identity(url)
        parsed = urlsplit(url)
        if (parsed.scheme != 'https' or parsed.hostname != self.EVENT_HOST or parsed.username
                or parsed.password or not re.fullmatch(self.EVENT_PATH,parsed.path)):
            raise ValueError('Unverified PGF registration host or route')
        values=parse_qs(parsed.query).get('regEventId',[])
        if len(values)!=1:
            raise ValueError('Missing or ambiguous PGF regEventId')
        try:
            decoded=base64.b64decode(values[0],validate=True).decode('ascii')
        except (ValueError,UnicodeDecodeError,binascii.Error) as exc:
            raise ValueError('Invalid PGF encoded registration ID') from exc
        if not re.fullmatch(r'[1-9]\d*',decoded):
            raise ValueError('PGF registration ID is not a published numeric token')
        return decoded

    def canonical_detail_url(self, url):
        if urlsplit(url).hostname==TC_HOST:
            tc_identity(url)
            return url
        self.get_event_identity(url)
        parsed=urlsplit(url)
        return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,
            urlencode({'regEventId':parse_qs(parsed.query)['regEventId'][0]}),''))

    def inspect_detail_html(self, url, html):
        identity=self.get_event_identity(url)
        soup=BeautifulSoup(html,'html.parser')
        headings=soup.select('h1')
        if len(headings)!=1 or not headings[0].get_text(' ',strip=True):
            raise ValueError('PGF registration detail has no unique title')
        sections={}
        for span in soup.select('div > span'):
            label=span.get_text(' ',strip=True)
            if label in {'Cost','Age & Gender','Dates'}:
                if label in sections:
                    raise ValueError('PGF detail has duplicate labelled sections')
                sections[label]=[p.get_text(' ',strip=True) for p in span.parent.find_all('p',recursive=False)]
        if set(sections)!={'Cost','Age & Gender','Dates'}:
            raise ValueError('PGF registration detail is missing labelled event sections')
        dates={}
        for text in sections['Dates']:
            match=re.fullmatch(r'(Start|End) Date:\s*(\d{2}/\d{2}/\d{4})',text)
            if match:
                if match[1] in dates:
                    raise ValueError('PGF detail repeats a date label')
                datetime.strptime(match[2],'%m/%d/%Y')
                dates[match[1]]=match[2]
        if set(dates)!={'Start','End'}:
            raise ValueError('PGF registration detail needs explicit-year start and end dates')
        if datetime.strptime(dates['End'],'%m/%d/%Y')<datetime.strptime(dates['Start'],'%m/%d/%Y'):
            raise ValueError('PGF detail end precedes start')
        locations=[]
        for name in soup.select('div.lt-tkt-column span.lt-name'):
            block=name.parent
            def text(selector):
                node=block.select_one(selector)
                return node.get_text(' ',strip=True) if node else ''
            locations.append(dict(venue=name.get_text(' ',strip=True),address=text('.lt-address'),
                                  city=text('.lt-city'),state=text('.lt-state'),zip=text('.lt-zip')))
        contacts=[]
        for block in soup.select('.section-contacts .contact'):
            values=[node.get_text(' ',strip=True) for node in block.find_all('div',recursive=False)]
            emails=re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',block.get_text(' ',strip=True))
            phone=re.search(r'\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}',block.get_text(' ',strip=True))
            contacts.append(dict(name=values[0] if values else '',email=emails[0] if emails else '',phone=phone[0] if phone else ''))
        return dict(source_id=identity,source_url=self.canonical_detail_url(url),sport='softball',
                    title=headings[0].get_text(' ',strip=True),start=dates['Start'],end=dates['End'],
                    fee_label=' | '.join(sections['Cost']),age_labels=' | '.join(sections['Age & Gender']),
                    locations=locations,contacts=contacts,ingestion_ready=False)

    def extract_event_details(self,url,driver=None,**kwargs):
        if urlsplit(url).hostname==TC_HOST:return extract_current(self,url)
        details=self.inspect_event(url)
        if not hasattr(self,'source_details'):self.source_details={}
        self.source_details[url]=details
        if not details['locations']:
            raise SourceEventIncomplete('PGF detail has no verified venue/state; listing state alone may conflict with physical venue')
        first=details['locations'][0]
        return make_row(title=details['title'],start=details['start'],end=details['end'],url=details['source_url'],
            state=first['state'],city=first['city'],locations=details['locations'],
            divisions=[dict(label=details['age_labels'],entry_fee=details['fee_label'],fee_label=details['fee_label'])],
            organizer=details['contacts'][0] if details['contacts'] else {},notes='Published eligibility: '+details['age_labels'])
