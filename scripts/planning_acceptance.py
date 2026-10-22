"""One explicitly enabled live planning request, never imports or starts workers."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Approve exactly one metered planning request using backend configuration')
    parser.add_argument('--parent-id')
    parser.add_argument('--agent-id', choices=['mimo', 'zcode-gateway', 'deepseek-gateway'], default='mimo')
    args = parser.parse_args()
    if not args.live:
        print('No request submitted. --live and --parent-id are required; may consume API quota.')
        return 0
    if not args.parent_id or not re.fullmatch('[a-f0-9]{12}', args.parent_id):
        parser.error('--parent-id must be an existing goal ID')
    with httpx.Client(base_url='http://127.0.0.1:8765', trust_env=False, timeout=60) as client:
        def get(url):
            response = client.get('/api' + url)
            response.raise_for_status()
            return response.json()
        client.headers['X-Nexus-Token'] = get('/session')['token']
        def post(url, body):
            response = client.post('/api' + url, json=body)
            response.raise_for_status()
            return response.json()
        initial = get('/tasks')
        if any(t['status'] == 'running' for t in initial):
            raise RuntimeError('Refusing live acceptance while another task is running')
        owner = next(t for t in initial if t['id'] == args.parent_id)
        before = hashlib.sha256(json.dumps(owner, sort_keys=True).encode()).hexdigest()
        run = post(f'/tasks/{args.parent_id}/planning', {'agent_id': args.agent_id})
        task_id = run['task_id']
        preview = get('/execution/preview?task_id=' + task_id)
        if not preview['eligible'] or len(preview['scopes']) != 1 or preview['scopes'][0]['agent_id'] != args.agent_id:
            raise RuntimeError('Planning preview is blocked or changed; task retained, no request submitted')
        result = post('/execution/run', {'task_id': task_id, 'revision': preview['revision'], 'approve_usage': True})
        if not result.get('accepted'):
            raise RuntimeError('Planning dispatch refused; no fallback or retry')
        deadline = time.monotonic() + 360
        while True:
            task = next(t for t in get('/tasks') if t['id'] == task_id)
            if task['status'] in ('done', 'failed', 'blocked', 'cancelled'):
                break
            if time.monotonic() >= deadline:
                post('/tasks/' + task_id + '/cancel', {})
                raise TimeoutError('Acceptance timed out; cancellation requested, no retry')
            time.sleep(1)
        run = next(r for r in get(f'/tasks/{args.parent_id}/planning')['runs'] if r['task_id'] == task_id)
        after = next(t for t in get('/tasks') if t['id'] == args.parent_id)
        unchanged = before == hashlib.sha256(json.dumps(after, sort_keys=True).encode()).hexdigest()
        evidence = {'mode': 'real-api-planning-only', 'task_id': task_id, 'parent_id': args.parent_id,
                    'agent_id': args.agent_id, 'status': task['status'], 'result': task['result'],
                    'attempt': task['last_attempt'], 'specification': run['specification'],
                    'original_goal_unchanged': unchanged, 'imported': False, 'workers_started': 0,
                    'retry_submitted': False, 'model_requests_approved': 1}
        path = ROOT / 'outputs' / ('planning-live-' + task_id + '.json')
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'task_id': task_id, 'status': task['status'], 'report': str(path),
                          'nodes': len((run['specification'] or {}).get('nodes', [])), 'original_goal_unchanged': unchanged}, ensure_ascii=True))
        return 0 if task['status'] == 'done' and unchanged and run['specification'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
