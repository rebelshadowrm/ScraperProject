"""Prepare bounded softball data and deliver it only to a verified test receiver.

Collect uses finite owned provider subprocesses. Prepare and send never scrape;
website receipts remain in their own SQLite outbox, separate from acquisition.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from urllib.parse import urlsplit,urlunsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import requests
from us import states
from common.delivery import DeliveryStore
from common.normalize import normalize_row
from common.process_budget import run_bounded
from common.softball_registry import PROVIDERS,softball_registry
from common.softball_rows import city_state
from common.storage import ledger_lock,save_json_atomic
from common.validation import is_future_event,validate_event_data


def isolated(path):
    path=Path(path).resolve()
    if not path.is_relative_to((ROOT/'Tests').resolve()):raise ValueError('Test-run state and artifacts must stay under Tests/')
    return path


def read_plan(path):
    plan=json.loads(Path(path).read_text(encoding='utf-8'))
    if plan.get('version')!=1 or not isinstance(plan.get('providers'),dict) or set(plan['providers'])!=set(PROVIDERS):
        raise ValueError('A version 1 plan must account for every registered softball provider')
    allowed={'max_events','listing_url','event_urls','search_state','search_period','filters','browser'}
    for provider,options in plan['providers'].items():
        if not isinstance(options,dict) or set(options)-allowed:raise ValueError('Unknown provider plan options: '+provider)
        if not 1<=options.get('max_events',2)<=3:raise ValueError('Keep each provider sample within 1..3 events')
    return plan


def collect(plan_path,run_dir,*,replay=False,workers=2,timeout=90):
    if workers not in (1,2) or not 5<=timeout<=180:raise ValueError('Use one/two isolated workers and 5..180 second deadlines')
    root=isolated(run_dir);root.mkdir(parents=True,exist_ok=True)
    plan=read_plan(plan_path)
    save_json_atomic(plan,root/'plan.json')
    def execute(provider,options):
        command=[sys.executable,str(ROOT/'scripts/softball_provider_cycle.py'),'--provider',provider,
                 '--output',str(root/(provider+'.json')),'--capture-dir',str(root/'captures'/provider),
                 '--max-events',str(options.get('max_events',2))]
        for key,flag in [('listing_url','--listing-url'),('search_state','--search-state'),('search_period','--search-period')]:
            if options.get(key):command.extend([flag,options[key]])
        for url in options.get('event_urls',[]):command.extend(['--event-url',url])
        if options.get('filters'):
            filters=root/(provider+'-filters.json');save_json_atomic(options['filters'],filters)
            command.extend(['--filters-file',str(filters)])
        if replay:command.append('--replay')
        if options.get('browser'):command.append('--browser')
        try:
            result=run_bounded(command,cwd=ROOT,stdout=root/(provider+'.stdout.log'),stderr=root/(provider+'.stderr.log'),timeout=timeout)
        except Exception as exc:result=dict(exit_code=2,timed_out=False,error=type(exc).__name__)
        if result.get('timed_out') or result.get('error'):
            report_path=root/(provider+'.json')
            partial=json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else dict(provider=provider,events=[])
            partial.update(status='blocked',coverage_complete=False,error='Owned provider process deadline/launch failed; completed event checkpoints retained; no retry')
            save_json_atomic(partial,report_path)
        print(json.dumps(dict(provider=provider,**result)),flush=True)
        return dict(provider=provider,**result)
    results=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Keep the three RegisterPlay tenants on one serial worker. Other source
        # families use separately owned processes and may proceed independently.
        api=[(key,value) for key,value in plan['providers'].items() if key in {'usa_fastpitch','usa_softball','nsa_fastpitch'}]
        remaining=[(key,value) for key,value in plan['providers'].items() if key not in {p for p,_ in api}]
        def tenant_sequence():return [execute(key,value) for key,value in api]
        futures=[pool.submit(tenant_sequence)]+[pool.submit(execute,key,value) for key,value in remaining]
        for future in as_completed(futures):
            result=future.result();results.extend(result if isinstance(result,list) else [result])
            save_json_atomic(dict(completed=len(results),total=len(PROVIDERS),results=results),root/'collection.json')
    return results


def site_locations(details):
    rows=details.get('locations') or details.get('facilities') or []
    if not isinstance(rows,list):return []
    result=[]
    for place in rows:
        if not isinstance(place,dict):continue
        inferred_city,inferred_state=city_state(place.get('location'))
        city=place.get('city') or place.get('City') or inferred_city
        raw_state=place.get('state') or place.get('State-FullName') or inferred_state
        state=states.DC if str(raw_state or '').strip().casefold() in {'dc','district of columbia'} else states.lookup(raw_state or '')
        if not city or not state:continue # Raw incomplete venue remains in source_details.
        value=dict(venue=place.get('venue') or place.get('Venue') or place.get('name') or '',
                   addressLine1=place.get('address') or place.get('Address') or '',city=city,state=state.abbr,
                   postalCode=place.get('zip') or place.get('Zip') or '')
        # Optional malformed source ZIPs remain in raw locations; never truncate
        # them into a plausible but invented postal code in the website contract.
        if not re.fullmatch(r'\d{5}(?:-\d{4})?',str(value['postalCode']).strip()):value['postalCode']=''
        if value not in result:result.append(value)
    if len(result)>10:raise ValueError('Website supports 10 structured venues; retain source for a complete mapping')
    return result


def prepare(run_dir):
    root=isolated(run_dir);read_plan(root/'plan.json')
    ledger_path=root/'acquisition.json'
    data=json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {'events':{}}
    accepted=[];held=[];providers=[]
    for provider,cls in softball_registry().items():
        path=root/(provider+'.json')
        report=json.loads(path.read_text(encoding='utf-8')) if path.exists() else dict(status='missing',events=[])
        if path.exists() and report.get('provider')!=provider:raise ValueError('Report provider does not match its plan entry')
        providers.append(dict(provider=provider,status=report.get('status'),error=report.get('error'),coverage_complete=report.get('coverage_complete',False)))
        scraper=cls()
        try:
            for item in report.get('events',[]):
                identity=item.get('event_id','')
                try:
                    if item.get('status')!='validated':raise ValueError(item.get('error') or item.get('status','No validated row'))
                    from common.identity import event_identity
                    if event_identity(scraper,item['url'])!=identity:raise ValueError('Identity does not match registered sport/provider/source')
                    row=normalize_row(item['row'])
                    valid,error=validate_event_data(row)
                    if not valid:raise ValueError(error)
                    if not is_future_event(row):raise ValueError('Event is no longer strictly future')
                    if not row.get('City') or row['City'].strip().lower() in {'tbd','unknown','various','varies','multiple locations'}:raise ValueError('Website needs a source-backed usable city')
                    if identity in accepted:raise ValueError('Duplicate identity in test bundle')
                    details=dict(item.get('source_details') or {})
                    locations=site_locations(details)
                    if locations:details['site_locations']=locations
                    payload=dict(row,source_details=details,eventStatus='active',**({'locations':locations} if locations else {}))
                    if len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))>256*1024:
                        raise ValueError('Source payload exceeds website 256 KiB limit; retain capture for targeted field/media repair')
                    previous=data['events'].get(identity,{})
                    data['events'][identity]=dict(previous,scraper=cls.__name__,sport='softball',sport_verified=True,
                        source_event_id=scraper.get_event_identity(item['url']),url=item['url'],event_data=row,source_details=details,
                        acquisition_status='ready',acquisition_refreshed_at=datetime.now(timezone.utc).isoformat(),
                        processed=previous.get('processed',False),export_status=previous.get('export_status','not_applicable'))
                    accepted.append(identity)
                except (ValueError,TypeError,KeyError) as exc:held.append(dict(provider=provider,event_id=identity,reason=str(exc)))
        finally:scraper.close()
    with ledger_lock(ledger_path):save_json_atomic(data,ledger_path)
    state=root/'website.sqlite3'
    with ledger_lock(state):
        store=DeliveryStore(state)
        try:
            staged=store.ingest(data)
            store.export('softball_connected',root/'envelopes.json',event_ids=set(accepted))
            delivery=store.summary()
        finally:store.close()
    unfinished=any(item['status'] in ('running','missing') for item in providers)
    result=dict(status='prepared' if accepted and not staged['quarantined'] and not unfinished else 'blocked',accepted=len(accepted),
        event_ids=accepted,held=held,providers=providers,ingestion=staged,delivery=delivery,publication='review-only receiver; no production send',
        acquisition_sha256=hashlib.sha256(ledger_path.read_bytes()).hexdigest())
    save_json_atomic(result,root/'prepared.json')
    return result


def send(run_dir,endpoint,*,token_env='SOFTBALL_CONNECTED_TEST_TOKEN',allow_loopback=False,replay=False,expected_commit=None,preflight=False):
    root=isolated(run_dir);prepared=json.loads((root/'prepared.json').read_text(encoding='utf-8'))
    if prepared.get('status')!='prepared':raise ValueError('Prepare a valid isolated bundle first')
    ledger=json.loads((root/'acquisition.json').read_text(encoding='utf-8'))
    if hashlib.sha256((root/'acquisition.json').read_bytes()).hexdigest()!=prepared['acquisition_sha256']:
        raise ValueError('Acquisition changed since preparation; prepare the reviewed bundle again')
    if any(not is_future_event(ledger['events'][key]['event_data']) for key in prepared['event_ids']):
        raise ValueError('Prepared source events have expired; collect and review a current bounded sample')
    parts=urlsplit(endpoint)
    local=allow_loopback and parts.hostname in ('127.0.0.1','localhost','::1') and parts.scheme in ('http','https')
    if ((parts.scheme!='https' and not local) or parts.username or parts.password or parts.query or parts.fragment
            or parts.path!='/api/acquisition/events'):raise ValueError('Use a verified HTTPS acquisition endpoint; local HTTP requires explicit loopback mode')
    health=urlunsplit((parts.scheme,parts.netloc,'/api/health/environment','',''))
    response=requests.get(health,timeout=(5,10),allow_redirects=False)
    if response.status_code!=200 or len(response.content)>65536:raise ValueError('Test site environment could not be verified')
    environment=response.json()
    if environment.get('sport')!='softball' or environment.get('site')!='softball-connected':raise ValueError('Test target is not the softball deployment')
    if not local and (environment.get('environment')!='staging' or environment.get('indexingAllowed') is not False
                      or environment.get('scheduledDeliveryAllowed') is not False or not expected_commit):
        raise ValueError('Remote test delivery needs an isolated staging site and exact expected deployment commit')
    if expected_commit and environment.get('commit')!=expected_commit:raise ValueError('Test target deployment commit differs')
    receiver=environment.get('acquisition') or {}
    if (receiver.get('contractVersion')!=1 or receiver.get('enabled') is not True
            or not (receiver.get('reviewOnly') is True or receiver.get('automaticPublication') is True)):
        raise ValueError('Supported acquisition receiver is not enabled/deployed; no event was sent')
    token=os.environ.get(token_env,'')
    if not token:raise ValueError('Set the test receiver credential in '+token_env)
    target_path=root/'site-target.json'
    target=dict(endpoint=endpoint,destination='softball_connected')
    known_target=target_path.exists()
    if target_path.exists() and json.loads(target_path.read_text(encoding='utf-8'))!=target:
        raise ValueError('This outbox belongs to another target; local receipts cannot acknowledge staging delivery')
    if preflight:return dict(status='verified',accepted=prepared['accepted'],post_requests=0,environment=environment)
    state=root/'website.sqlite3'
    with ledger_lock(state):
        save_json_atomic(target,target_path)
        store=DeliveryStore(state)
        try:
            store.bind_target('softball_connected',endpoint,adopt_existing_receipts=known_target)
            if replay:store.requeue('softball_connected')
            pending=store.pending('softball_connected',limit=1000)
            if {item['event_id'] for item in pending}-set(prepared['event_ids']):raise ValueError('Unexpected events in test outbox; review the selected bundle')
            result=store.send('softball_connected',endpoint,token,limit=1000,allow_loopback=allow_loopback)
            report=dict(status='delivered' if result['delivered']==prepared['accepted'] else 'partial',destinations=store.summary(),
                        attempted=len(pending),environment=environment,
                        acquisition_unchanged=hashlib.sha256((root/'acquisition.json').read_bytes()).hexdigest()==prepared['acquisition_sha256'])
        finally:store.close()
    save_json_atomic(report,root/'delivery-report.json')
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('collect','prepare','preflight','send'))
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--plan',type=Path)
    parser.add_argument('--workers',type=int,choices=(1,2),default=2)
    parser.add_argument('--timeout',type=int,choices=range(5,181),default=90)
    parser.add_argument('--replay',action='store_true')
    parser.add_argument('--endpoint')
    parser.add_argument('--token-env',default='SOFTBALL_CONNECTED_TEST_TOKEN')
    parser.add_argument('--allow-loopback',action='store_true')
    parser.add_argument('--expected-commit')
    args=parser.parse_args(argv)
    try:
        if args.action=='collect':
            if not args.plan:parser.error('collect requires --plan')
            result=collect(args.plan,args.run_dir,replay=args.replay,workers=args.workers,timeout=args.timeout)
            return 1 if any(item['exit_code'] for item in result) else 0
        if args.action=='prepare':result=prepare(args.run_dir)
        else:
            if not args.endpoint:parser.error('send requires --endpoint')
            result=send(args.run_dir,args.endpoint,token_env=args.token_env,allow_loopback=args.allow_loopback,
                        replay=args.replay,expected_commit=args.expected_commit,preflight=args.action=='preflight')
        print(json.dumps({key:result[key] for key in ('status','accepted','attempted','destinations','acquisition_unchanged') if key in result},indent=2))
        return 0 if result['status'] in ('prepared','delivered','verified') else 1
    except Exception as exc:
        # Error categories/messages contain configuration only, never transport
        # exception bodies or authentication headers.
        print(json.dumps(dict(status='failed',error=type(exc).__name__,message=str(exc) if isinstance(exc,ValueError) else 'See isolated source/receiver diagnostics')))
        return 2


if __name__=='__main__':raise SystemExit(main())
