"""External cron trigger. No copied strategy, and no token in CLI arguments or logs."""
import os
import time
import httpx


def main():
    url = os.environ.get('AUTO_SCAN_BASE_URL', '').rstrip('/')
    token = os.environ.get('AUTO_SCAN_TOKEN', '')
    if not url or not token:
        raise SystemExit('AUTO_SCAN_BASE_URL and AUTO_SCAN_TOKEN must be configured as secrets')
    with httpx.Client(timeout=90, follow_redirects=False) as client:
        # Cold startup may return a loading response. Only health checks are retried.
        for attempt in range(3):
            try:
                response = client.get(url+'/health')
                if response.is_success and response.json().get('status') == 'ok':
                    break
            except (httpx.HTTPError, ValueError):
                pass
            if attempt == 2:
                raise SystemExit('Service health check failed')
            time.sleep(20)
        try:
            response = client.post(url+'/api/internal/auto-scan',headers={'Authorization':'Bearer '+token})
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError,ValueError):
            raise SystemExit('Protected automatic scan trigger failed; inspect server status') from None
        print('Trigger:',result.get('status'),result.get('reason',''),result.get('id',''))
        if result.get('status') != 'running':
            return
        # Keep an in-progress Free service alive, and report completion rather than just submission.
        for _ in range(100):
            time.sleep(20)
            try:
                job = client.get(url+'/api/task').json()
            except (httpx.HTTPError,ValueError):
                continue
            if job.get('id') != result['id']:
                raise SystemExit('Task changed unexpectedly')
            if job.get('status') != 'running':
                print('Result:',job['status'],job.get('automatic',{}),job.get('stats',{}))
                if job['status'] not in ('completed','completed_with_warnings'):
                    raise SystemExit('Automatic scan did not complete')
                return
        raise SystemExit('Automatic scan did not finish within the polling window')


if __name__ == '__main__':
    main()
