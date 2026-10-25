"""National discovery from published directories, forms and continuation controls.

Listing completeness and extractable details are separate. Unsupported external
layouts and failed scopes are retained as holds, never certified empty.
"""
import json
import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit, parse_qs, urlencode, urldefrag

from bs4 import BeautifulSoup
from common.collection import CollectedLinks
from common.softball_scaffold import fetch_listing
from common.sport import payload_revision


def result(links, *, scopes, pages, held=(), error='', source_total=None):
    value=CollectedLinks(list(dict.fromkeys(links)),complete=not error and not any(not item.get('out_of_scope') for item in held),error=error,pages=pages)
    value.scopes=scopes
    value.held=list(held)
    value.source_total=source_total
    return value


def discover_fasa(scraper):
    soup=BeautifulSoup(fetch_listing(scraper.DISCOVERY_URL),'html.parser')
    # An unfiltered source table, rather than the bounded exact-date sample.
    for field in ('STARTDATE','STATE'):
        node=soup.select_one(f'select[name="{field}"]')
        if node is None or not node.select_one('option[value=""]'):
            raise ValueError('FASA all-date/all-state controls changed')
        selected=node.select_one('option[selected]')
        if selected is not None and selected.get('value'):
            raise ValueError('FASA default search is unexpectedly filtered')
    links=[]
    for anchor in soup.select(scraper.DISCOVERY_SELECTOR):
        url=scraper.canonical_detail_url(urljoin(scraper.DISCOVERY_URL,anchor['href']))
        links.append(url)
        row=anchor.find_parent('tr')
        if row:scraper.discovery_revisions[url]=payload_revision([cell.get_text(' ',strip=True) for cell in row.find_all('td',recursive=False)][1:15])
    if not links:raise ValueError('FASA source table is missing; inventory unknown')
    continuation=soup.select('a[rel="next"],input[name*="Next"],a.page-numbers')
    return result(links,scopes=['ALL DATES / ALL STATES source table'],pages=1,source_total=len(set(links)),
                  error='Unrecognized FASA continuation requires inspection' if continuation else '')


def discover_topgun(scraper, *, max_pages=100):
    raw=fetch_listing(scraper.REQUEST_URL)
    soup=BeautifulSoup(raw,'html.parser')
    wrapper=soup.select_one('.tus_efinder_wrap')
    if wrapper is None:raise ValueError('Tagup event finder is absent')
    args={}
    for field in wrapper.select('.tus_square_filter_input,.tus_field_range_input.on,.tus_efinder_filter_input'):
        key=field.get('name') or field.get('data-key')
        if field.name=='select':
            option=field.select_one('option[selected]') or field.select_one('option')
            value=option.get('value','') if option else ''
        else:value=field.get('value','')
        if key and value:args[key]=value
    if args.get('event_status')!='upcoming':raise ValueError('Tagup source upcoming scope changed')
    total_node=wrapper.select_one('.tus_sui_pagination_count')
    match=re.fullmatch(r'(\d+) Events?',total_node.get_text(' ',strip=True) if total_node else '')
    if not match:raise ValueError('Tagup source total is absent')
    total=int(match[1]);links=[];seen=set();pages=0;error='Tagup page budget exhausted';held=[]
    for page in range(1,max_pages+1):
        if page>1:
            data={'action':'tus_efinder_query_events',**{f'args[{key}]':value for key,value in dict(args,page=str(page)).items()}}
            try:
                raw=fetch_listing('https://app.tagup.com/topgun/wp-admin/admin-ajax.php',method='POST',data=data,content_type='html')
                marker='##__wpvr__##'
                if not raw.startswith(marker) or not raw.endswith(marker):raise ValueError('Tagup continuation envelope changed')
                response=json.loads(raw[len(marker):-len(marker)])
                if response.get('status')!=1:raise ValueError('Tagup continuation was rejected')
                data=response.get('data',{})
                soup=BeautifulSoup(data.get('list_html','')+data.get('pagination_html',''),'html.parser')
            except Exception as exc:
                return result(links,scopes=['upcoming / all states'],pages=pages,source_total=total,held=held,
                    error=type(exc).__name__+': Tagup continuation incomplete')
        cards=soup.select('.tus_efinder_result[data-event_id][data-event_type]')
        if not cards and total:raise ValueError('Tagup continuation returned no event cards')
        for card in cards:
            identity=card['data-event_id']
            if not re.fullmatch(r'[1-9]\d*',identity) or identity in seen:
                error='Tagup repeated or invalid identity during pagination'
                return result(links,scopes=['upcoming / all states'],pages=pages,source_total=total,error=error)
            seen.add(identity)
            if card['data-event_type']!='tournament':
                held.append({'source_id':identity,'reason':'Source marks a camp rather than a team tournament' if card['data-event_type']=='hcamp' else 'Unsupported source event type',
                    'out_of_scope':card['data-event_type']=='hcamp'})
                continue
            url=f'https://app.tagup.com/topgun/event/tournament/{identity}/'
            scraper.get_event_identity(url);links.append(url)
            scraper.discovery_revisions[url]=payload_revision(card.get_text(' ',strip=True))
        pages+=1
        options=soup.select('#tus_sui_pagination_dropdown option')
        last=max((int(o['value']) for o in options if o.get('value','').isdigit()),default=page)
        if page>=last:
            error='' if len(seen)==total else 'Tagup unique event count differs from source total'
            break
    value=result(links,scopes=['upcoming / all states'],pages=pages,error=error,source_total=total,held=held)
    value.filter_counts={'source_records':total,'non_tournament':sum(bool(item.get('out_of_scope')) for item in held),'discovered_identities':len(value)}
    return value


def discover_triplecrown(scraper):
    landing=BeautifulSoup(fetch_listing(urljoin(scraper.DISCOVERY_URL,'find-an-event')),'html.parser')
    expected='https://components.triplecrownsports.com/Components/GetFilteredEventSchedule/?sportID=3&state='
    if not landing.select_one(f'iframe[src="{expected}"]'):raise ValueError('Triple Crown published fastpitch search changed')
    soup=BeautifulSoup(fetch_listing(expected),'html.parser')
    rows=soup.select('.event-row');links=[];held=[]
    if not rows:raise ValueError('Triple Crown national schedule is missing')
    for row in rows:
        anchor=row.select_one('a.event-name[href]')
        stamp=row.select_one('.event-date-container')
        if not anchor or not stamp:
            held.append({'reason':'Schedule row lacks source URL or occurrence date'});continue
        url=anchor['href']
        try:
            super(type(scraper),scraper).get_event_identity(url)
            date=datetime.strptime(stamp.get_text(' ',strip=True),'%d %b %Y').strftime('%Y-%m-%d')
            url=urldefrag(url)[0]+'#tc-start='+date
            scraper.get_event_identity(url)
            links.append(url);scraper.discovery_revisions[url]=payload_revision(row.get_text(' ',strip=True))
        except ValueError:
            held.append({'source_url':url,'reason':'External Triple Crown schedule layout/identity unsupported'})
    return result(links,scopes=['published sportID=3 / all states schedule'],pages=1,held=held,source_total=len(rows))


def discover_usssa(scraper, *, max_pages=2000):
    directory=BeautifulSoup(fetch_listing(scraper.DISCOVERY_URL),'html.parser')
    scopes=sorted(set(a['href'].rstrip('/') for a in directory.select('a[href]')
        if re.fullmatch(r'https://[a-z]{2}fastpitch\.usssa\.com/?',a['href'])))
    if len(scopes)<50:raise ValueError('USSSA state directory is missing scopes')
    scraper.verified_hosts={urlsplit(url).hostname for url in scopes}
    links=[];held=[];examined=[];pages=0
    for root in scopes:
        url=root+'/events/';visited=set();page_signatures=set();scope={'url':url,'pages':0,'status':'running'};examined.append(scope)
        try:
            while url:
                if pages>=max_pages:raise ValueError('USSSA national listing page budget exhausted')
                if url in visited:raise ValueError('USSSA pagination cycle')
                visited.add(url)
                requested_page=parse_qs(urlsplit(url).query).get('paged',[''])[0]
                redirects=('/events/page/'+requested_page+'/',) if re.fullmatch(r'[1-9]\d*',requested_page) else ()
                soup=BeautifulSoup(fetch_listing(url,**({'redirect_paths':redirects} if redirects else {})),'html.parser');pages+=1;scope['pages']+=1
                anchors=soup.select('.events-item-wrapper a[href*="/event/"]')
                page_links=[]
                for anchor in anchors:
                    detail=urljoin(url,anchor['href'])
                    if urlsplit(detail).hostname not in scraper.verified_hosts:continue
                    try:scraper.get_event_identity(detail)
                    except ValueError:
                        held.append({'scope':root,'source_url':detail,'reason':'Source event link has an unsupported identity'});continue
                    page_links.append(urldefrag(detail)[0])
                if not page_links:
                    # No-events must be source-confirmed, not a redirect/empty shell.
                    if not re.search(r'No (?:upcoming )?events (?:found|scheduled)',soup.get_text(' ',strip=True),re.I):
                        raise ValueError('No verified event cards or explicit empty-state message')
                signature=tuple(sorted(set(page_links)))
                if page_links and signature in page_signatures:raise ValueError('USSSA repeated event page')
                page_signatures.add(signature)
                links.extend(page_links)
                next_node=soup.select_one('.pagination a.next[href]')
                next_url=urljoin(url,next_node['href']) if next_node else ''
                if next_url and (urlsplit(next_url).hostname!=urlsplit(root).hostname or not re.fullmatch(r'/events/(?:page/[1-9]\d*/)?',urlsplit(next_url).path)):
                    raise ValueError('USSSA continuation left the verified scope')
                # WordPress can combine /page/N/ with ?paged=N+1, then serve
                # page N again. The source query supplies the next page number;
                # the observed same-origin canonical route resolves it reliably.
                next_page=parse_qs(urlsplit(next_url).query).get('paged',[''])[0] if next_url else ''
                if re.fullmatch(r'[1-9]\d*',next_page):next_url=root+'/events/page/'+next_page+'/'
                url=next_url
            scope['status']='complete'
        except Exception as exc:
            scope.update(status='failed',reason=type(exc).__name__+': '+str(exc)[:180])
            held.append({'scope':root,'reason':scope['reason']})
    return result(links,scopes=examined,pages=pages,held=held)


def discover_usfa(scraper):
    directory=BeautifulSoup(fetch_listing(scraper.DISCOVERY_URL),'html.parser')
    urls=sorted(set(urldefrag(a['href'])[0] for a in directory.select('a[href]')
        if urlsplit(a['href']).hostname in ('usfaevents.com','softball.sincsports.com')
        and urlsplit(a['href']).path.lower()=='/eventlist.aspx'
        and re.fullmatch(r'USFA[A-Z]{2}',parse_qs(urlsplit(a['href']).query).get('tid',[''])[0])))
    if not urls:raise ValueError('USFA published state directory is missing')
    links=[];held=[];scopes=[];pages=0
    for url in urls:
        state=parse_qs(urlsplit(url).query)['tid'][0];scope={'source_url':url,'scope':state};scopes.append(scope)
        try:
            soup=BeautifulSoup(fetch_listing(url),'html.parser');pages+=1
            cards=soup.select('.ev-card[data-tid][data-type="event"]')
            if not cards:raise ValueError('USFA state event-card layout is absent')
            for card in cards:
                identity=card['data-tid'];anchor=card.select_one('a.ev-name[href]')
                if not anchor or parse_qs(urlsplit(anchor['href']).query).get('tid')!=[identity]:raise ValueError('USFA card identity conflict')
                # This canonical host/route is the existing verified redirect target.
                detail=f'https://usfaevents.com/details.aspx?tid={identity}&tab=1'
                scraper.get_event_identity(detail);links.append(detail)
                scraper.discovery_revisions[detail]=payload_revision(card.get_text(' ',strip=True))
            continuation=any('showMore(' in node.get('onclick','') for node in soup.select('[onclick]'))
            scope['status']='partial' if continuation else 'complete'
            if continuation:held.append({'scope':state,'reason':'EO callback continuation requires verification'})
        except Exception as exc:
            scope.update(status='failed',reason=type(exc).__name__+': '+str(exc)[:120]);held.append({'scope':state,'reason':scope['reason']})
    return result(links,scopes=scopes,pages=pages,held=held)


def discover_alliance(scraper, *, max_pages=200):
    landing=BeautifulSoup(fetch_listing(scraper.DISCOVERY_URL),'html.parser')
    node=landing.select_one('.wsr-season-context[data-season]')
    if not node or not re.fullmatch(r'20\d{2}',node['data-season']):raise ValueError('Alliance source season is missing')
    # All is an actual published tournament league-division control.
    season=node['data-season'];links=[];held=[];pages=0;total=None;raw_rows=0;seen=set();seen_pages=set();revisions={};error='Alliance page budget exhausted';duplicates=0;identities={}
    scraper._listing_rows={}
    for page in range(1,max_pages+1):
        params=dict(page=page,limit=25,season=season,age_division='All',type='upcoming',league_division='All',sort_by='dates',sort_direction='asc',env='prod',action='wsr_get_events_data')
        try:
            raw=fetch_listing('https://thealliancefastpitch.com/wp-admin/admin-ajax.php?'+urlencode(params),content_type='json')
            payload=json.loads(raw);data=payload.get('data',{})
        except Exception as exc:
            error=type(exc).__name__+': Alliance continuation incomplete';break
        current=data.get('totalRecords');rows=data.get('events')
        if payload.get('success') is not True or data.get('stale') is not False or type(current) is not int or not isinstance(rows,list):
            error='Alliance live inventory is stale or invalid';break
        if total is not None and current!=total:error='Alliance total changed during pagination';break
        total=current
        if len(rows)>25 or (not rows and raw_rows<total):error='Alliance pagination ended before total';break
        signature=payload_revision(rows)
        if signature in seen_pages:error='Alliance repeated a whole page during pagination';break
        seen_pages.add(signature)
        for row in rows:
            fingerprint=payload_revision(row)
            if fingerprint in seen:duplicates+=1
            seen.add(fingerprint);raw_rows+=1
        else:
            for row in rows:
                if row.get('Event ID')==0:
                    anchors=BeautifulSoup(row.get('event_name',''),'html.parser').select('a[href]')
                    held.append({'reason':'Source Event ID is zero; stable occurrence identity unresolved',
                        'source_url':anchors[0]['href'] if len(anchors)==1 else None,'source_dates':row.get('dates')})
                    continue
                identity=row.get('Event ID')
                if type(identity) is not int or identity<1:
                    held.append({'reason':'Source Event ID has an unsupported type'});continue
                anchors=BeautifulSoup(row.get('event_name',''),'html.parser').select('a[href]')
                public=anchors[0]['href'] if len(anchors)==1 else scraper.DISCOVERY_URL
                detail=urldefrag(public)[0]+f'#alliance-event={identity}'
                try:scraper.get_event_identity(detail)
                except ValueError:
                    held.append({'source_id':identity,'reason':'Source detail URL is invalid'});continue
                if identity in identities and identities[identity]!=(detail,row.get('dates')):
                    held.append({'source_id':identity,'reason':'Repeated source ID has conflicting URL or occurrence dates'});continue
                identities[identity]=(detail,row.get('dates'))
                if detail in scraper._listing_rows and scraper._listing_rows[detail]!=row:
                    held.append({'source_id':identity,'reason':'Repeated source ID has conflicting rows'});continue
                links.append(detail);revisions[detail]=payload_revision(row);scraper._listing_rows[detail]=row
            pages+=1
            if raw_rows==total:error='';break
            if raw_rows>total:error='Alliance row count exceeds total';break
            continue
        break
    scraper.discovery_revisions=revisions
    value=result(links,scopes=[f'season={season} / upcoming / league_division=All'],pages=pages,held=held,error=error,source_total=total)
    value.filter_counts={'source_records':raw_rows,'duplicate_rows':duplicates,'unique_rows':len(seen),'discovered_identities':len(value)}
    return value


def discover_pg(scraper, *, max_pages=100):
    url=scraper.DISCOVERY_URL
    soup=BeautifulSoup(fetch_listing(url),'html.parser')
    form=soup.select_one('form[method="post"]')
    load=soup.select_one('#ContentTopLevel_ContentPlaceHolder1_lbFilter')
    if form is None or load is None:raise ValueError('PG published schedule form changed')
    action=re.fullmatch(r"javascript:__doPostBack\('([^']+)',''\)",load.get('href',''))
    if not action:raise ValueError('PG load-events action is unrecognized')
    data={node['name']:node.get('value','') for node in form.select('input[type="hidden"][name]')}
    data.update(__EVENTTARGET=action[1],__EVENTARGUMENT='')
    raw=fetch_listing(url,method='POST',data=data,redirect_paths=('/softball/schedule.aspx',))
    links=[];held=[];pages=0;seen=set();scope=[];error='PG page budget exhausted';total=None
    scraper.schedule_groups={}
    for page in range(max_pages):
        soup=BeautifulSoup(raw,'html.parser')
        anchors=soup.select(scraper.DISCOVERY_SELECTOR)
        if not anchors:
            if not links:raise ValueError('PG loaded schedule has no verified division links')
            error='PG continuation returned no verified division links';break
        current=[]
        for anchor in anchors:
            detail=scraper.canonical_detail_url(urljoin(url,anchor['href']))
            if detail not in current:current.append(detail)
            # Preserve the source's explicit schedule grouping without merging
            # distinct published division IDs or renaming historical identities.
            row=anchor.find_parent('tr')
            group=row.select_one('input[name$="$hfEventScheduleGroupID"]') if row else None
            if group and group.get('value'):scraper.schedule_groups[detail]=group['value']
        if not set(current)-seen:
            error='PG repeated a schedule page';break
        links.extend(current);seen.update(current);pages+=1
        next_node=soup.select_one('.rgPager [title="Next Page"]')
        pager=soup.select_one('.rgPager')
        if pager and not scope:scope.append(pager.get_text(' ',strip=True))
        count=re.search(r'(\d+)\s+items in\s+(\d+)\s+pages',pager.get_text(' ',strip=True) if pager else '')
        if count:total=int(count[1])
        action=(next_node.get('href') or next_node.get('onclick') or '') if next_node else ''
        if not next_node or action=='return false;':
            error='' if total is None or len(seen)==total else 'PG unique division count differs from source total'
            break
        postback=re.fullmatch(r"javascript:__doPostBack\('([^']+)',''\)",action)
        if postback:
            form_node=soup.select_one('form')
            form_url=urljoin(url,form_node.get('action','')) if form_node else ''
            parsed_form=urlsplit(form_url)
            if parsed_form.scheme!='https' or parsed_form.hostname!='www.perfectgame.org' or parsed_form.path!='/softball/schedule.aspx':
                error='PG continuation form left the verified schedule';break
            data={node['name']:node.get('value','') for node in soup.select('form input[type="hidden"][name]')}
            data.update(__EVENTTARGET=postback[1],__EVENTARGUMENT='')
            try:raw=fetch_listing(form_url,method='POST',data=data,redirect_paths=('/softball/schedule.aspx',))
            except Exception as exc:error=type(exc).__name__+': PG form continuation failed';break
            continue
        continuation=urljoin(url,action);parsed=urlsplit(continuation);params=parse_qs(parsed.query)
        if parsed.hostname!='www.perfectgame.org' or parsed.scheme!='https' or parsed.path!='/softball/schedule.aspx':
            error='PG continuation left its published schedule';break
        # These are server-generated filters, not synthesized base64 values.
        if params.get('ty')!=['MjA='] or params.get('st')!=['Wlo=']:
            error='PG continuation changed softball/all-state scope';break
        if not scope or len(scope)==1:scope.append({key:value for key,value in params.items() if 'ChangePage' not in key})
        try:raw=fetch_listing(continuation)
        except Exception as exc:error=type(exc).__name__+': PG continuation failed';break
    return result(links,scopes=scope,pages=pages,held=held,error=error,source_total=total)


def discover_national(provider,scraper):
    return {'fasa':discover_fasa,'topgun_events':discover_topgun,'triplecrown_fastpitch':discover_triplecrown,
            'usssa_fastpitch':discover_usssa,'usfa':discover_usfa,'alliance_fastpitch':discover_alliance,
            'pg_softball':discover_pg}[provider](scraper)
