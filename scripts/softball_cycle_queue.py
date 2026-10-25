"""Finite isolated provider checks. Completion never triggers another source pull."""
import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from common.process_budget import run_bounded
from common.softball_registry import softball_catalog
from common.storage import save_json_atomic


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--providers',nargs='+',choices=sorted(softball_catalog()),required=True)
    parser.add_argument('--run-dir',required=True)
    parser.add_argument('--workers',type=int,choices=(1,2),default=1)
    parser.add_argument('--timeout',type=int,default=90)
    parser.add_argument('--replay',action='store_true')
    args=parser.parse_args(argv)
    root=Path(args.run_dir).resolve()
    if not root.is_relative_to((ROOT/'Tests').resolve()) or not 5<=args.timeout<=180:
        parser.error('Use an isolated Tests/ run directory and a 5..180 second provider deadline')
    root.mkdir(parents=True,exist_ok=True)
    providers=list(dict.fromkeys(args.providers))
    def execute(provider):
        command=[sys.executable,str(ROOT/'scripts/softball_provider_cycle.py'),'--provider',provider,
                 '--output',str(root/(provider+'.json')),'--capture-dir',str(root/'captures'/provider)]
        if args.replay:command.append('--replay')
        report_path=root/(provider+'.json')
        try:
            result=run_bounded(command,cwd=ROOT,stdout=root/(provider+'.stdout.log'),
                               stderr=root/(provider+'.stderr.log'),timeout=args.timeout)
        except Exception as exc:
            result=dict(exit_code=2,timed_out=False,error=f'{type(exc).__name__}: {exc}')
            save_json_atomic(dict(provider=provider,status='failed',error=result['error']),report_path)
        if result['timed_out']:
            report=json.loads(report_path.read_text()) if report_path.exists() else {'provider':provider}
            report.update(status='timed_out',error='Provider process deadline exceeded; no automatic retry')
            save_json_atomic(report,report_path)
        print(json.dumps(dict(provider=provider,**result)),flush=True)
        return dict(provider=provider,**result)
    results=[]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(execute,provider) for provider in providers]
        for future in as_completed(futures):
            results.append(future.result())
            save_json_atomic({'providers':results,'remaining':len(providers)-len(results)},root/'queue.json')
    return 2 if any(item['exit_code']>1 for item in results) else 1 if any(item['exit_code']==1 for item in results) else 0


if __name__=='__main__':
    raise SystemExit(main())
