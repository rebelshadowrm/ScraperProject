"""One provider, one listing, no details, runtime ledger, workbook or delivery."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.identity import event_identity
from common.softball_registry import softball_catalog, softball_registry
from common.softball_scaffold import SoftballNotReady, SoftballScaffoldScraper


def run(provider, *, fixture=None, live=False, max_events=5, listing_url=None):
    if (fixture is None) == (not live):
        raise ValueError('Choose exactly one of --fixture or --live')
    if not 1 <= max_events <= 25:
        raise ValueError('max_events must be 1..25')
    cls = softball_registry()[provider]
    scraper = cls()
    report = dict(provider=provider, scraper=cls.__name__, sport='softball',
                  status='blocked', complete=False, events=[], pages=0)
    try:
        if not isinstance(scraper, SoftballScaffoldScraper):
            # Existing full collectors are not bounded by this one-page contract.
            raise SoftballNotReady('Existing adapter needs a one-page smoke collector; '
                                   'use offline test_softball_acquisition; access gates remain in force')
        report['discovery_url'] = scraper.DISCOVERY_URL
        base_url = listing_url or scraper.DISCOVERY_URL
        links = (scraper.parse_listing(Path(fixture).read_text(encoding='utf-8'), base_url,
                                      max_events=max_events) if fixture is not None else
                 scraper.collect_event_links(None, base_url, max_events=max_events))
        report.update(status='partial', complete=links.complete, pages=links.pages,
                      error=links.error, events=[dict(event_id=event_identity(scraper, url), url=url) for url in links])
        for field in ('excluded_other_sport', 'unresolved_identities', 'source_total'):
            if hasattr(links, field):
                report[field] = getattr(links, field)
    except Exception as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        scraper.close()
    return report


def inspect_event(provider, event_url):
    """Inspect one verified detail API contract without ingestion or discovery."""
    scraper = softball_registry()[provider]()
    try:
        if not (getattr(scraper, 'DETAIL_API_VERIFIED', False) or getattr(scraper, 'DETAIL_HTML_VERIFIED', False)):
            raise SoftballNotReady('No independently verified detail inspection for this provider')
        return dict(provider=provider, status='inspection_only', complete=False,
                    requests=scraper.DETAIL_INSPECTION_REQUESTS, event=scraper.inspect_event(event_url))
    except Exception as exc:
        return dict(provider=provider, status='blocked', complete=False,
                    error=f'{type(exc).__name__}: {exc}')
    finally:
        scraper.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list', action='store_true', help='List registered providers without network or writes')
    parser.add_argument('--provider', choices=sorted(softball_registry()))
    parser.add_argument('--kind', choices=('api', 'html', 'browser', 'unresolved'), help='Filter --list by discovery strategy')
    parser.add_argument('--inspect-event-url', help='With --live, inspect one verified event (1 HTML or 2 JSON requests); no ingestion')
    parser.add_argument('--listing-url', help='Exact browser-observed listing URL, only where the provider validates it')
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--fixture', type=Path)
    source.add_argument('--live', action='store_true', help='Opt into one HTTP GET; no pagination or details')
    parser.add_argument('--max-events', type=int, choices=range(1, 26), default=5)
    args = parser.parse_args(argv)
    if args.list:
        if args.provider or args.fixture or args.live or args.inspect_event_url or args.listing_url:
            parser.error('--list cannot be combined with acquisition options')
        print(json.dumps(softball_catalog(kind=args.kind), indent=2))
        return 0
    if args.kind:
        parser.error('--kind requires --list')
    if not args.provider or (args.fixture is None and not args.live):
        parser.error('--provider and either --fixture or --live are required')
    if args.inspect_event_url and not args.live:
        parser.error('--inspect-event-url requires --live')
    if args.inspect_event_url and args.listing_url:
        parser.error('--inspect-event-url cannot be combined with --listing-url')
    report = (inspect_event(args.provider, args.inspect_event_url) if args.inspect_event_url else
              run(args.provider, fixture=args.fixture, live=args.live, max_events=args.max_events,
                  listing_url=args.listing_url))
    print(json.dumps(report, indent=2))
    # All initial samples are partial. Never signal production coverage success.
    return 2 if report['status'] == 'blocked' else 1


if __name__ == '__main__':
    raise SystemExit(main())
