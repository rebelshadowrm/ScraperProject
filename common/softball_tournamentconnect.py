"""Verified public PGF TournamentConnect read protocol; no registration writes."""
import base64
import json
import re
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from common.collection import CollectedLinks,validated_records
from common.softball_scaffold import fetch_listing
from common.softball_rows import make_row
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from common.sport import payload_revision
from common.softball_description import description


TC_HOST='softball.tournamentconnect.premiergirlsfastpitch.com'
TC_LISTING=f'https://{TC_HOST}/events/register/tournaments/provider/premiergirlsfastpitch'
TC_API=f'https://{TC_HOST}/callAPI.php?e=1'
TC_HEADERS={'Content-Type':'application/json','X-Requested-With':'XMLHttpRequest','Accept':'application/json','Referer':TC_LISTING}


def tc_identity(url):
    parts=urlsplit(url)
    if (parts.scheme!='https' or parts.netloc!=TC_HOST or parts.username or parts.password
            or parts.query or parts.path!=urlsplit(TC_LISTING).path or not re.fullmatch(r'tc-event=[1-9]\d*',parts.fragment)):
        raise ValueError('PGF current event needs the verified public listing and numeric TournamentConnect identity')
    return 'tc-'+parts.fragment.split('=',1)[1]


def tc_read(parameters,*,encoded=False):
    body={'pass':1,'controller':'Event','action':'read','parameters':dict(parameters)}
    if encoded:body['parameters']['encode']=1
    wire=base64.b64encode(json.dumps(body,separators=(',',':')).encode()).decode()
    raw=fetch_listing(TC_API,method='POST',data=wire,headers=TC_HEADERS)
    if encoded:
        raw=base64.b64decode(raw.strip().strip('"'),validate=True).decode('utf-8')
    result=json.loads(raw)
    if not isinstance(result,(list,dict)):raise SourceEventIncomplete('PGF current read returned no event object/list')
    return result


def collect_current(scraper,*,max_events=5,max_pages=1):
    if not 1<=max_events<=25 or not 1<=max_pages<=3:raise ValueError('PGF development bound is 25 events and 3 pages')
    links=[];seen=set();pages=0;exhausted=False
    scraper._tc_payloads={}
    for page in range(max_pages):
        data=tc_read(dict(providerid=76,eventtypeid=1,eventids=None,sportid=4,sort='startdate',limit=5,offset=page*5,
            entityid=None,entity='team',historylabel=False,futurelabel=True,fltr=False,filters={'visible':1}),encoded=True)
        rows=validated_records(list(data.values()) if isinstance(data,dict) else data,source='PGF current listing')
        pages+=1
        if len(rows)>5:raise SourceEventIncomplete('PGF page exceeded its verified limit')
        for payload in rows:
            event=payload.get('event',{})
            identity=event.get('eventid')
            if type(identity) is not int or identity<=0 or identity in seen:raise SourceEventIncomplete('PGF page contains missing/repeated event identities')
            seen.add(identity)
            url=TC_LISTING+f'#tc-event={identity}'
            scraper._tc_payloads[url]=payload
            scraper.discovery_revisions[url]=payload_revision({key:event.get(key) for key in
                ('eventid','modified','startdate','enddate','name','pricinglabel','divisionlabel','visible')})
            if len(links)<max_events:links.append(url)
        exhausted=len(rows)<5
        if exhausted or len(links)>=max_events:break
    result=CollectedLinks(links,complete=False,pages=pages,error='Bounded PGF TournamentConnect pages; #tc-event is an internal reference, not a source-supported modal deep link')
    result.source_page_exhausted=exhausted
    return result


def extract_current(scraper,url):
    key=int(tc_identity(url)[3:])
    payload=getattr(scraper,'_tc_payloads',{}).get(url) or tc_read({'eventid':key})
    if isinstance(payload,dict) and any(field not in payload for field in ('divisions','facilities')):
        payload=tc_read({'eventid':key})
    if not isinstance(payload,dict) or not isinstance(payload.get('event'),dict):raise SourceEventIncomplete('PGF single-event read has no event')
    event=payload['event']
    if event.get('eventid')!=key or event.get('sportid')!=4 or event.get('eventtypeid')!=1 or event.get('entitytype')!='team':
        raise SourceEventIncomplete('PGF event identity/sport/team type does not match')
    if event.get('ngb_affiliation')!=76 and event.get('providerid')!=76:raise SourceEventIncomplete('PGF affiliation is not verified')
    if event.get('visible')!=1:raise SourceEventOutOfScope('PGF source event is not publicly visible')
    raw_divisions=validated_records(payload,field='divisions',source='PGF current detail')
    divisions=[]
    for item in raw_divisions:
        if item.get('do_not_display')==1:continue
        if item.get('eventid')!=key or item.get('gender')!='Girls':raise SourceEventIncomplete('PGF division event/gender differs from the verified source')
        minimum,maximum=item.get('fromDivisionAge'),item.get('toDivisionAge')
        age=minimum
        if minimum!=maximum:
            # Eligibility bounds are not a list of offered divisions. A source
            # row explicitly named 10U with bounds 8..10 is one 10U division.
            labelled=re.fullmatch(r'(\d{1,2})U',str(item.get('division') or '').strip(),re.I)
            labelled_age=int(labelled[1]) if labelled else None
            bounded=(type(minimum) is int and type(maximum) is int
                     and 0<=minimum<maximum<=18 and labelled_age==maximum)
            single_bound=(maximum is None and type(minimum) is int and minimum==labelled_age)
            if not labelled or not 6<=labelled_age<=18 or not (bounded or single_bound):
                raise SourceEventIncomplete('PGF source division spans an unresolved age range; no matching explicit U division')
            age=labelled_age
        divisions.append(dict(label=f"{item.get('division','')} {item.get('sportskill','')}",age=age,
            entry_fee=item.get('price'),fee_label=str(item.get('price')),
            source_division_id=item.get('eventdivisionid'),minimum_age=minimum,maximum_age=maximum,
            competition_class=item.get('sportskill')))
    locations=[]
    facilities=payload.get('facilities') or []
    for item in validated_records(facilities,source='PGF current detail facilities'):
        locations.append(dict(venue=item.get('name',''),address=item.get('physical_address',''),city=item.get('physical_city',''),
            state=item.get('physical_state',''),zip=item.get('physical_zip','')))
    rich=description(event.get('description'),base_url=url)
    description_text=rich['text']
    emails=re.findall(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',description_text)
    provider=payload.get('provider') or {}
    scraper.source_details=getattr(scraper,'source_details',{})
    scraper.source_details[url]=dict(backend='tournamentconnect',source_event_id=key,
        event={key:event.get(key) for key in ('eventid','providerid','sportid','eventtypeid','ngb_affiliation','name','startdate','enddate','modified','pricinglabel','divisionlabel')},
        divisions=divisions,locations=locations,description=rich,
        data_gaps=['Public View Details opens a modal; #tc-event is the scraper reference and API eventid retrieves it independently']
            +([] if locations else ['Facilities not published in the independent source event response']))
    return make_row(title=event.get('name'),start=event.get('startdate'),end=event.get('enddate'),url=url,
        state=event.get('stateabbr'),city=event.get('city',''),divisions=divisions,locations=locations,
        organizer=dict(name=provider.get('name',''),email=emails[0] if emails else provider.get('email',''),phone=provider.get('phonenumber','')),
        guaranteed_games=event.get('gameguarantee',''),notes=description_text,hotel='Yes' if 'stay to play' in description_text.lower() else 'NA')
