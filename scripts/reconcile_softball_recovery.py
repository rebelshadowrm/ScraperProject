"""Merge isolated acquisition forks without replacing durable review/export flags.

Only strictly newer successful source observations replace existing event facts.
Missing events never mean deletion. The result remains isolated and undelivered.
"""
import argparse
from copy import deepcopy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common.identity import event_identity
from common.softball_registry import softball_registry
from common.storage import save_json_atomic

SOURCE_FIELDS=('scraper','sport','sport_verified','source_event_id','url','event_data','source_details',
    'acquisition_status','acquisition_refreshed_at','acquired_discovery_revision','observed_discovery_revision',
    'last_attempt_status','source_transport')


def stamp(entry):
    try:
        value=datetime.fromisoformat(entry.get('acquisition_refreshed_at',''))
        return value if value.tzinfo else datetime.min.replace(tzinfo=timezone.utc)
    except (TypeError,ValueError):return datetime.min.replace(tzinfo=timezone.utc)


def merge(base,inputs):
    data=deepcopy(base);providers={cls.__name__:cls for cls in softball_registry().values()}
    report={'added':0,'refreshed':0,'older_or_equal_preserved':0,'held':[]}
    for incoming in inputs:
        for key,entry in incoming.get('events',{}).items():
            cls=providers.get(entry.get('scraper'))
            if not cls or entry.get('sport')!='softball' or entry.get('acquisition_status')!='ready':
                report['held'].append({'event_id':key,'reason':'Unknown provider/sport or non-ready acquisition'});continue
            scraper=cls()
            try:
                if event_identity(scraper,entry['url'])!=key:raise ValueError('Identity disagrees with provider URL')
            except Exception as exc:
                report['held'].append({'event_id':key,'reason':str(exc)});continue
            finally:scraper.close()
            current=data['events'].get(key)
            if current is None:
                data['events'][key]=deepcopy(entry);report['added']+=1
            elif stamp(entry)>stamp(current):
                for field in SOURCE_FIELDS:
                    if field in entry:current[field]=deepcopy(entry[field])
                # These are acquisition eligibility holds, not human-reviewed
                # listing corrections. Carry the newest parser decision only.
                for field in ('source_review_required','source_review_reason'):
                    if field in entry:current[field]=deepcopy(entry[field])
                    else:current.pop(field,None)
                report['refreshed']+=1
            else:report['older_or_equal_preserved']+=1
    report.update(stored=len(data['events']),missing_records_withdrawn=0)
    return data,report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base',type=Path,required=True);parser.add_argument('--input',type=Path,action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv);root=args.output.resolve()
    if not root.is_relative_to((ROOT/'Tests').resolve()) or root.exists():raise ValueError('Use a new isolated Tests directory')
    paths=[args.base,*args.input];raw=[path.read_bytes() for path in paths]
    data,report=merge(json.loads(raw[0]),[json.loads(value) for value in raw[1:]])
    root.mkdir(parents=True)
    report.update(inputs=[{'path':str(path),'sha256':hashlib.sha256(value).hexdigest()} for path,value in zip(paths,raw)],production_state_modified=False)
    save_json_atomic(data,root/'events.json');save_json_atomic(report,root/'result.json');print(json.dumps(report))


if __name__=='__main__':main()
