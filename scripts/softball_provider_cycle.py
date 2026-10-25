"""Bounded provider discovery/detail validation with reusable local captures.

This command never opens the production ledger, a workbook or a website outbox.
Run one provider per subprocess; see softball_cycle_queue.py for finite scheduling.
"""
import argparse
from contextlib import ExitStack
from datetime import date
import hashlib
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from common.collection import CollectedLinks
from common.config import OUTPUT_COLUMNS
from common.identity import event_identity
from common.normalize import normalize_row
from common.softball_registry import PROVIDERS, softball_registry
from common.softball_scaffold import SoftballNotReady, SoftballScaffoldScraper, fetch_listing
from common.source_exclusions import SourceEventIncomplete,SourceEventOutOfScope
from common.storage import save_json_atomic,atomic_output
from common.validation import is_future_event, validate_event_data


class CaptureFetcher:
    def __init__(self,root,*,replay=False,max_requests=10):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.replay=replay
        self.max_requests=max_requests
        self.requests=0
        self.cache_hits=0
        self.last_observed_at=None

    def __call__(self,url,*,session=None,content_type='html',method='GET',data=None,json_body=None,headers=None,redirect_paths=()):
        signature=[url,content_type] if method=='GET' and data is None else [url,content_type,method,data]
        if json_body is not None:signature=[url,content_type,method,{'json':json_body}]
        if headers:signature.append({'headers':headers})
        if redirect_paths:signature.append({'redirect_paths':redirect_paths})
        key=hashlib.sha256(json.dumps(signature,sort_keys=True).encode()).hexdigest()
        body=self.root/(key+'.txt')
        if body.exists():
            self.cache_hits+=1
            self.last_observed_at=body.stat().st_mtime
            return body.read_text(encoding='utf-8')
        if self.replay:
            raise RuntimeError('Replay capture is missing; no network fallback')
        if self.requests>=self.max_requests:
            raise RuntimeError('Provider request budget exhausted')
        self.requests+=1
        options=dict(session=session,content_type=content_type,method=method,data=data)
        if json_body is not None:options['json_body']=json_body
        if headers:options['headers']=headers
        if redirect_paths:options['redirect_paths']=redirect_paths
        text=fetch_listing(url,**options)
        with atomic_output(body,backup=False) as staged:
            Path(staged).write_text(text,encoding='utf-8')
        self.last_observed_at=body.stat().st_mtime
        save_json_atomic(dict(url=url,content_type=content_type,method=method,data=data,json_body=json_body,headers=headers,redirect_paths=redirect_paths),self.root/(key+'.json'))
        return text


def run_cycle(provider,*,output,capture_dir,max_events=2,listing_url=None,replay=False,event_urls=None,search_state=None,search_period=None,filters=None,browser=False):
    if not 1<=max_events<=3:
        raise ValueError('Provider cycle supports 1..3 sampled events')
    output=Path(output).resolve()
    captures=Path(capture_dir).resolve()
    # Development artifacts have an explicit isolated destination.
    allowed=(ROOT/'Tests').resolve()
    if not output.is_relative_to(allowed) or not captures.is_relative_to(allowed):
        raise ValueError('Cycle reports and captures must stay under Tests/')
    report=dict(provider=provider,status='running',coverage_complete=False,events=[],
                observed_on=date.today().isoformat(),requests=0,cache_hits=0)
    save_json_atomic(report,output)
    scraper=softball_registry()[provider]()
    fetcher=CaptureFetcher(captures,replay=replay)
    driver=None
    existing=not isinstance(scraper,SoftballScaffoldScraper)
    source_operations=0
    try:
        if filters:
            if not hasattr(scraper,'configure_filters'):raise ValueError('Provider has no verified filter configuration')
            scraper.configure_filters(**filters)
        if search_state or search_period:
            if not hasattr(scraper,'configure_search'):raise ValueError('Provider does not support verified JSON search')
            scraper.configure_search(state=search_state,period=search_period)
        if existing:
            scraper.require_permission()
            if replay:raise SoftballNotReady('Existing gated adapters use their offline fixture tests; no uncaptured replay acquisition')
            if not listing_url and not event_urls:raise ValueError('Existing sample adapter needs a state-scoped listing URL or independent event URLs')
            if scraper.REQUIRES_DRIVER:
                if not browser:raise SoftballNotReady('Bounded NCS acquisition needs --browser and an owned driver; access record remains separate')
                from common.driver_manager import initialize_driver
                driver=initialize_driver(download_dir=str(captures/'downloads'),headless=True,page_load_timeout_sec=20)
        module=importlib.import_module(PROVIDERS[provider][0])
        modules=[importlib.import_module(name) for name in ('common.softball_scaffold','common.softball_api','common.softball_tournamentconnect')]+[module]
        with ExitStack() as stack:
            if existing and hasattr(scraper,'_get'):
                original=scraper._get
                def counted(*args,**kwargs):
                    nonlocal source_operations
                    source_operations+=1
                    if source_operations>10:raise ValueError('Existing source-operation budget exceeded')
                    return original(*args,**kwargs)
                stack.enter_context(patch.object(scraper,'_get',counted))
            for target in modules:
                if hasattr(target,'fetch_listing'):
                    stack.enter_context(patch.object(target,'fetch_listing',fetcher))
            if event_urls:
                if len(event_urls)>max_events:raise ValueError('Explicit events exceed sample bound')
                for url in event_urls:scraper.get_event_identity(url)
                links=CollectedLinks(event_urls,complete=False,error='Explicit independent detail sample; no listing coverage claim')
            else:
                links=(scraper.collect_discovery_sample(driver,listing_url,max_events=max_events) if existing else
                       scraper.collect_event_links(None,listing_url or scraper.DISCOVERY_URL,max_events=max_events))
            report.update(discovered=len(links),collection_error=getattr(links,'error',''),
                coverage_complete=getattr(links,'complete',False))
            for key in ('source_total','search_filters','unresolved_identities','excluded_other_sport','unresolved_locations'):
                if hasattr(links,key):report[key]=getattr(links,key)
            for url in links[:max_events]:
                item=dict(event_id=event_identity(scraper,url),url=url,status='running')
                report['events'].append(item)
                try:
                    row=scraper.extract_event_details(url,driver=driver)
                    if not isinstance(row,dict):
                        raise ValueError('Extractor returned no event row')
                    valid,error=validate_event_data(row)
                    if not valid:
                        raise ValueError(error)
                    row=normalize_row(row)
                    item.update(status='validated' if is_future_event(row) else 'expired',
                                row=row,source_details=getattr(scraper,'source_details',{}).get(url,{}))
                except SourceEventOutOfScope as exc:
                    item.update(status='out_of_scope',error=str(exc))
                except SourceEventIncomplete as exc:
                    item.update(status='source_incomplete',error=str(exc))
                except (NotImplementedError,SoftballNotReady) as exc:
                    item.update(status='needs_implementation',error=str(exc))
                    if getattr(scraper,'DETAIL_API_VERIFIED',False) or getattr(scraper,'DETAIL_HTML_VERIFIED',False):
                        try:
                            item['inspection']=scraper.inspect_event(url)
                        except Exception as detail_error:
                            item['inspection_error']=f'{type(detail_error).__name__}: {detail_error}'
                except Exception as exc:
                    item.update(status='failed',error=f'{type(exc).__name__}: {exc}')
                if 'source_details' not in item and url in getattr(scraper,'source_details',{}):
                    item['source_details']=scraper.source_details[url]
                report.update(requests=fetcher.requests,cache_hits=fetcher.cache_hits)
                save_json_atomic(report,output)
        counts={status:sum(item['status']==status for item in report['events']) for status in
                ('validated','expired','out_of_scope','source_incomplete','needs_implementation','failed')}
        report.update(counts=counts,status='needs_implementation' if counts['needs_implementation'] else
                      'failed' if counts['failed'] else 'partial' if not report['coverage_complete'] else 'sample_complete')
    except Exception as exc:
        report.update(status='blocked',error=f'{type(exc).__name__}: {exc}')
    finally:
        if driver is not None:
            from common.driver_manager import safe_quit_driver
            safe_quit_driver(driver)
        scraper.close()
        report.update(requests=fetcher.requests,cache_hits=fetcher.cache_hits)
        if existing:
            report.update(requests=None if source_operations or driver is not None else 0,source_operations=source_operations,
                          transport='existing provider/browser path; source operations are not individual retry counts')
        save_json_atomic(report,output)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider',required=True,choices=sorted(PROVIDERS))
    parser.add_argument('--output',required=True)
    parser.add_argument('--capture-dir',required=True)
    parser.add_argument('--max-events',type=int,choices=range(1,4),default=2)
    parser.add_argument('--listing-url')
    parser.add_argument('--event-url',action='append')
    parser.add_argument('--replay',action='store_true')
    parser.add_argument('--search-state',help='Verified RegisterPlay state filter, e.g. IN')
    parser.add_argument('--search-period',help='Observed begin-period token, e.g. d|11|2026')
    parser.add_argument('--filters-file',type=Path,help='Provider-specific verified filter JSON')
    parser.add_argument('--browser',action='store_true',help='Permit an owned NCS development browser after its existing access gate')
    args=parser.parse_args(argv)
    report=run_cycle(args.provider,output=args.output,capture_dir=args.capture_dir,
                     max_events=args.max_events,listing_url=args.listing_url,replay=args.replay,event_urls=args.event_url,
                     search_state=args.search_state,search_period=args.search_period,
                     filters=json.loads(args.filters_file.read_text(encoding='utf-8')) if args.filters_file else None,browser=args.browser)
    print(json.dumps({key:report.get(key) for key in ('provider','status','counts','requests','cache_hits','error')}))
    return 2 if report['status'] in {'blocked','failed','needs_implementation'} else 1 if report['status']=='partial' else 0


if __name__=='__main__':
    raise SystemExit(main())
