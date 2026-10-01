"""Explicit live acceptance tool; never run by the mock-only CI tests."""
import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description='Run one controlled real-market scan and save its actual report.')
    parser.add_argument('--url', required=True)
    parser.add_argument('--exchanges', nargs='+', choices=('binance','okx','gate'), required=True)
    parser.add_argument('--markets', nargs='+', choices=('spot','perpetual'), required=True)
    scope=parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--limit', type=int, choices=range(1,101), metavar='1-100')
    scope.add_argument('--full', action='store_true')
    parser.add_argument('--turnover', type=float, default=10_000_000)
    parser.add_argument('--timeout', type=int, default=3600)
    parser.add_argument('--output', type=Path, required=True)
    args=parser.parse_args()
    base=args.url.rstrip('/')
    options={'exchanges':args.exchanges,'markets':args.markets,'periods':['4h'],
             'min_previous_day_turnover':args.turnover,'max_symbols':args.limit}
    with httpx.Client(timeout=90) as client:
        client.get(base+'/health').raise_for_status()
        current=client.get(base+'/api/task')
        current.raise_for_status()
        state=current.json()
        if state['status']=='running':
            raise SystemExit('An existing scan is running; do not replace or stop it.')
        time.sleep(state.get('scan_cooldown_seconds',0))
        start=client.post(base+'/api/task',json=options)
        start.raise_for_status()
        task_id=start.json()['id']
        print('TASK',task_id,options,flush=True)
        began=time.monotonic()
        previous=None
        while True:
            try:
                response=client.get(base+'/api/task')
                response.raise_for_status()
                job=response.json()
            except httpx.HTTPError:
                # A brief Render connection failure does not create a second scan.
                if time.monotonic()-began<args.timeout:
                    time.sleep(5)
                    continue
                raise
            if job.get('id')!=task_id:
                raise SystemExit('Task changed or server restarted; acceptance is incomplete.')
            progress=(job['status'],job.get('phase'),job.get('stats'),job.get('filter_done'),job.get('done'))
            if progress!=previous:
                print(json.dumps({'status':job['status'],'current':job.get('current'),'stats':job.get('stats')},ensure_ascii=True),flush=True)
                previous=progress
            if job['status']!='running':
                break
            if time.monotonic()-began>=args.timeout:
                client.post(base+'/api/task/stop').raise_for_status()
                job=client.get(base+'/api/task').json()
                break
            time.sleep(5)
        report={'base_url':base,'options':options,'job':job,
                'host_status':client.get(base+'/api/exchanges/status').json()}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print('REPORT',args.output,'STATUS',job['status'],'DURATION',job.get('duration'),'STATS',job.get('stats'),flush=True)


if __name__=='__main__':
    main()
