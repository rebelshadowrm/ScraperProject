"""Reprocess frozen source facts into a cloned ledger/outbox, never the live store."""
import argparse
import re
from copy import deepcopy
from contextlib import closing
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common.delivery import DeliveryStore
from common.identity import event_identity
from common.normalize import normalize_row
from common.softball_api import RegisterPlaySoftballScraper
from common.softball_registry import softball_registry
from common.softball_description import description
from common.softball_registerplay_metadata import division_metadata
from common.storage import save_json_atomic
from common.validation import is_future_event,validate_event_data
from scripts.softball_site_run import site_locations
from common.softball_transport import source_transport
from common.source_exclusions import SourceEventOutOfScope
from common.softball_lifecycle import retain_cancellation


@source_transport(offline=True)
def reprocess(*,ledger,outbox,output,reports=(),pgf_captures=(),usa_capture=None,usa_captures=(),provider_captures=(),recover_discovered=False,normalize_existing=True):
    output=Path(output).resolve()
    if not output.is_relative_to((ROOT/'Tests').resolve()) or output.exists():
        raise ValueError('Use a new isolated output directory under Tests/')
    output.mkdir(parents=True)
    source_bytes=Path(ledger).read_bytes();data=json.loads(source_bytes)
    source=sqlite3.connect(Path(outbox).resolve().as_uri()+'?mode=ro',uri=True)
    target=sqlite3.connect(str(output/'website.sqlite3'))
    try:source.backup(target)
    finally:source.close();target.close()
    registry=softball_registry();by_name={cls.__name__:cls for cls in registry.values()}
    before=deepcopy(data);changed=[];held=[];live_refresh=[];recovered=[]
    for key,entry in (data['events'].items() if normalize_existing else []):
        if entry.get('sport')!='softball':continue
        cls=by_name.get(entry.get('scraper'))
        if not cls:held.append({'event_id':key,'reason':'Unknown provider'});continue
        scraper=cls()
        try:
            details=deepcopy(entry.get('source_details') or {})
            if isinstance(scraper,RegisterPlaySoftballScraper):
                if not all(field in details for field in ('title','start','end','divisions')):
                    live_refresh.append(key);continue
                if not all('registration' in item for item in details['divisions']):live_refresh.append(key)
                header={'_key':scraper.get_event_identity(entry['url']),'sport':'FP','name':details['title'],
                    **{raw:details.get(name) for raw,name in [('isYouth','is_youth'),('isLeague','is_league'),
                        ('hasTeamDivisions','has_team_divisions'),('hasIndividualDivisions','has_individual_divisions'),
                        ('cancelled','cancelled'),('location','location'),('dirName','organizer_name'),
                        ('site','source_organization'),('season','season')]}}
                info={'eventInfo':{'starts':details['start'],'ends':details['end'],'state':details.get('state'),
                    'info':details.get('additional_information_html',''),'ccFeeExtra':details.get('credit_card_fee_extra')},
                    'divisions':details['divisions'],'facilities':details.get('facilities',[])}
                observed=scraper.inspect_detail_payloads(entry['url'],header=header,info=info)
                # Previously retained structured facts win over a reconstructed
                # observation that no longer has their raw provider fields.
                old={str(item.get('_key')):item for item in details['divisions']}
                for item in observed['divisions']:
                    prior=old.get(str(item.get('_key')), {})
                    if prior.get('registration'):item['registration']=prior['registration']
                row=scraper.extract_observation(entry['url'],observed)
                entry['event_data']=normalize_row(row)
                entry['source_details']={**details,**scraper.source_details[entry['url']]}
            elif details.get('description'):
                rich=description(details['description'].get('html',''),base_url=entry['url'])
                details['description']=rich;entry['source_details']=details
            elif details.get('event_schema',{}).get('description'):
                raw=details['event_schema']['description'];rich=description(raw,base_url=entry['url'])
                legacy=entry['event_data'].get('More-information-and-additional-tournament-locations','')
                if legacy.startswith(raw):legacy=legacy[len(raw):].strip(' |')
                entry['event_data']['More-information-and-additional-tournament-locations']=rich['text']+'\n\n'+legacy
                details['description']=rich;entry['source_details']=details
            else:live_refresh.append(key)
            locations=site_locations(entry.get('source_details',{}))
            if locations:entry['source_details']['site_locations']=locations
            raw_locations=entry.get('source_details',{}).get('locations') or []
            if raw_locations and isinstance(raw_locations[0],dict):
                first=raw_locations[0];raw_zip=first.get('zip') or first.get('Zip')
                if raw_zip and not re.fullmatch(r'\d{5}(?:-\d{4})?',str(raw_zip).strip()):entry['event_data']['Zip']=''
            if entry.get('event_data')!=before['events'][key].get('event_data') or entry.get('source_details')!=before['events'][key].get('source_details'):
                changed.append(key)
        except Exception as exc:
            data['events'][key]=before['events'][key]
            held.append({'event_id':key,'reason':type(exc).__name__+': '+str(exc)})
        finally:scraper.close()

    # Explicit captured reports replay real provider parsing. Their source
    # acquisition timestamps are not refreshed by an offline reprocess.
    for path in reports:
        report=json.loads(Path(path).read_text(encoding='utf-8'))
        for item in report.get('events',[]):
            key=item.get('event_id')
            if key not in data['events'] or item.get('status')!='validated':continue
            current=data['events'][key]
            current['event_data']=normalize_row(item['row']);current['source_details']=item.get('source_details',{})
            locations=site_locations(current['source_details'])
            if locations:current['source_details']['site_locations']=locations
            changed.append(key)
    if usa_capture:
        payload=json.loads(Path(usa_capture).read_text(encoding='utf-8'))
        scraper=registry['usa_softball']();url=f"https://{scraper.EVENT_HOST}/pages/event/detail/{payload['header']['_key']}"
        key=event_identity(scraper,url)
        if key in data['events']:
            details=scraper.inspect_detail_payloads(url,header=payload['header'],info=payload['info'])
            data['events'][key].update(event_data=normalize_row(scraper.extract_observation(url,details)),source_details=scraper.source_details[url])
            data['events'][key]['source_details']['source_observed_on']=payload.get('observed_on')
            locations=site_locations(data['events'][key]['source_details'])
            if locations:data['events'][key]['source_details']['site_locations']=locations
            changed.append(key);live_refresh=[identity for identity in live_refresh if identity!=key]
    for directory in pgf_captures:
        for capture in Path(directory).glob('*.txt'):
            try:payload=json.loads(capture.read_text(encoding='utf-8'))
            except (ValueError,UnicodeError):continue
            if not isinstance(payload,dict) or not isinstance(payload.get('event'),dict):continue
            scraper=registry['pgf']()
            from common.softball_tournamentconnect import TC_LISTING
            url=TC_LISTING+'#tc-event='+str(payload['event'].get('eventid'))
            try:
                key=event_identity(scraper,url);scraper._tc_payloads={url:payload}
                row=normalize_row(scraper.extract_event_details(url))
                valid,error=validate_event_data(row)
                if not valid or not is_future_event(row):raise ValueError(error or 'Not future')
                details=scraper.source_details[url];locations=site_locations(details)
                if locations:details['site_locations']=locations
                entry=data['events'].setdefault(key,{})
                existed=key in before['events']
                entry.update(scraper=type(scraper).__name__,sport='softball',sport_verified=True,
                    source_event_id=scraper.get_event_identity(url),url=url,event_data=row,source_details=details,
                    acquisition_status='ready')
                if not existed:
                    entry.update(acquisition_refreshed_at=datetime.fromtimestamp(capture.stat().st_mtime,timezone.utc).isoformat(),
                        processed=False,export_status='not_applicable')
                    recovered.append(key)
                changed.append(key)
            except Exception as exc:held.append({'capture':str(capture),'reason':str(exc)})
            finally:scraper.close()
    for directory in usa_captures:
        for capture in Path(directory).glob('*.json'):
            scraper=registry['usa_softball']()
            try:
                payload=json.loads(capture.read_text(encoding='utf-8'))
                identity=payload['header']['_key'];url=f'https://{scraper.EVENT_HOST}/pages/event/detail/{identity}'
                details=scraper.inspect_detail_payloads(url,header=payload['header'],info=payload['info'])
                row=normalize_row(scraper.extract_observation(url,details));valid,error=validate_event_data(row)
                if not valid or not is_future_event(row) or not row.get('City'):raise ValueError(error or 'No future event with usable city')
                key=event_identity(scraper,url);existed=key in data['events']
                locations=site_locations(scraper.source_details[url])
                if locations:scraper.source_details[url]['site_locations']=locations
                entry=data['events'].setdefault(key,{})
                entry.update(scraper=type(scraper).__name__,sport='softball',sport_verified=True,
                    source_event_id=str(identity),url=url,event_data=row,source_details=scraper.source_details[url],
                    acquisition_status='ready',last_attempt_status='success',source_transport='browser_capture',
                    acquisition_refreshed_at=datetime.fromtimestamp(capture.stat().st_mtime,timezone.utc).isoformat())
                entry.setdefault('processed',False);entry.setdefault('export_status','not_applicable')
                changed.append(key)
                if not existed:recovered.append(key)
                live_refresh=[value for value in live_refresh if value!=key]
            except Exception as exc:
                if isinstance(exc,SourceEventOutOfScope):
                    key=event_identity(scraper,url)
                    if retain_cancellation(data['events'].get(key),scraper.source_details.get(url,{}),observed_at=datetime.fromtimestamp(capture.stat().st_mtime,timezone.utc).isoformat()):changed.append(key)
                held.append({'capture':str(capture),'reason':type(exc).__name__+': '+str(exc)})
            finally:scraper.close()
    for provider,directory in provider_captures:
        from scripts.softball_provider_cycle import CaptureFetcher
        cls=registry[provider]
        fetcher=CaptureFetcher(directory,replay=True)
        with source_transport(fetcher),closing(cls()) as scraper:
            urls=[entry['url'] for entry in data['events'].values() if entry.get('scraper')==cls.__name__ and entry.get('sport')=='softball']
            if recover_discovered:
                from common.softball_discovery import discover_national
                urls=list(dict.fromkeys(urls+list(discover_national(provider,scraper))))
            for url in urls:
                key=event_identity(scraper,url);entry=data['events'].get(key)
                try:
                    row=normalize_row(scraper.extract_event_details(url))
                    valid,error=validate_event_data(row)
                    if not valid or not row.get('City'):raise ValueError(error or 'No usable source city')
                    if not is_future_event(row):raise SourceEventOutOfScope('Source event is no longer future')
                    details=scraper.source_details[url];locations=site_locations(details)
                    if locations:details['site_locations']=locations
                    if entry is None:
                        entry=data['events'].setdefault(key,dict(scraper=cls.__name__,sport='softball',sport_verified=True,
                            source_event_id=scraper.get_event_identity(url),url=url,acquisition_status='ready',last_attempt_status='success',
                            processed=False,export_status='not_applicable',source_transport='capture_replay',
                            acquisition_refreshed_at=datetime.fromtimestamp(fetcher.last_observed_at,timezone.utc).isoformat()))
                        recovered.append(key)
                    entry.update(event_data=row,source_details=details)
                    entry.pop('source_review_required',None);entry.pop('source_review_reason',None)
                    changed.append(key)
                    live_refresh=[value for value in live_refresh if value!=key]
                except SourceEventOutOfScope as exc:
                    # Explicit source classification warrants review. Keep the
                    # historical row and receipts; do not publish it as a team
                    # tournament or silently withdraw a reviewed hosted record.
                    if entry is not None:entry.update(source_review_required=True,source_review_reason=str(exc))
                    held.append({'event_id':key,'reason':str(exc),'source_review_required':True})
                except Exception as exc:held.append({'event_id':key,'reason':type(exc).__name__+': '+str(exc)})
    save_json_atomic(data,output/'events.json')
    store=DeliveryStore(output/'website.sqlite3')
    try:
        result=store.ingest(data)
        repeat=store.ingest(data)
        assert repeat['staged']==0,'Repeated reprocessing cannot create another delivery revision'
        store.export('softball_connected',output/'envelopes.json')
        summary={'status':'reprocessed_isolated','source_ledger_sha256':hashlib.sha256(source_bytes).hexdigest(),
            'inventory_before':len(before['events']),'inventory_after':len(data['events']),
            'changed':len(set(changed)),'recovered':sorted(set(recovered)),'held':held,
            'requires_live_metadata_refresh':sorted(set(live_refresh)),
            'ingestion':result,'repeat_ingestion':repeat,'delivery':store.summary(),
            'historical_acknowledged':store.db.execute('SELECT count(*) FROM deliveries WHERE delivered_sequence IS NOT NULL').fetchone()[0],
            'network_requests':0,'production_state_modified':False}
        summary['existing_normalization_performed']=normalize_existing
        save_json_atomic(summary,output/'result.json');return summary
    finally:store.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',required=True,type=Path);parser.add_argument('--outbox',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path);parser.add_argument('--report',action='append',default=[],type=Path)
    parser.add_argument('--pgf-captures',action='append',default=[],type=Path);parser.add_argument('--usa-capture',type=Path)
    parser.add_argument('--usa-captures',action='append',default=[],type=Path)
    parser.add_argument('--provider-captures',action='append',nargs=2,default=[],metavar=('PROVIDER','DIRECTORY'))
    parser.add_argument('--recover-discovered',action='store_true',help='Replay all captured national listing identities, including previously rejected details')
    parser.add_argument('--captures-only',action='store_true',help='Preserve unrelated records and replay only explicitly supplied captures')
    args=parser.parse_args(argv)
    result=reprocess(ledger=args.ledger,outbox=args.outbox,output=args.output,reports=args.report,pgf_captures=args.pgf_captures,usa_capture=args.usa_capture,usa_captures=args.usa_captures,provider_captures=args.provider_captures,recover_discovered=args.recover_discovered,normalize_existing=not args.captures_only)
    print(json.dumps({**{key:result[key] for key in ['status','inventory_before','inventory_after','changed','historical_acknowledged','network_requests']},'recovered_count':len(result['recovered']),'held_count':len(result['held'])}))


if __name__=='__main__':main()
