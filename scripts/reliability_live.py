"""Explicit, bounded acceptance using the running Nexus backend's own keys.

No credential files, key export, retries, fallback, or automatic merge. --live
requires the operator's approval for one small request per selected runtime.
"""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

import httpx
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[1]


def check_arithmetic_fixture(content: bytes, function: str) -> dict:
    """Execute only an AST-verified two-argument arithmetic function, no imports."""
    if not __debug__:
        raise RuntimeError('The acceptance harness refuses Python -O; validation checks must remain active')
    assert function in ('add', 'multiply')
    tree = ast.parse(content.decode('utf-8'))
    assert len(tree.body) == 1 and type(tree.body[0]) is ast.FunctionDef
    node = tree.body[0]
    assert node.name == function and not node.decorator_list and node.returns is None and not node.type_params
    assert not node.args.posonlyargs and not node.args.kwonlyargs and not node.args.defaults
    assert node.args.vararg is None and node.args.kwarg is None
    assert [a.arg for a in node.args.args] == ['a', 'b'] and all(a.annotation is None for a in node.args.args)
    assert len(node.body) == 1 and type(node.body[0]) is ast.Return
    expression = node.body[0].value
    assert type(expression) is ast.BinOp
    assert type(expression.left) is ast.Name and expression.left.id == 'a'
    assert type(expression.right) is ast.Name and expression.right.id == 'b'
    assert type(expression.op) is (ast.Add if function == 'add' else ast.Mult)
    namespace = {}
    exec(compile(tree, '<Nexus AST-whitelisted arithmetic fixture>', 'exec'), {'__builtins__': {}}, namespace)
    cases = [(0, 0), (1, 2), (-3, 4), (7, -9), (100, 200)]
    results = []
    for a, b in cases:
        actual = namespace[function](a, b)
        expected = a+b if function == 'add' else a*b
        assert actual == expected
        results.append({'input': [a, b], 'expected': expected, 'actual': actual, 'passed': True})
    return {'passed': True, 'cases': len(cases), 'results': results, 'source': 'nexus-fixed-ast-harness', 'function': function}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--agents', nargs='*')
    parser.add_argument('--label', default='p1')
    parser.add_argument('--resume', help='Existing outputs JSON; collect evidence only, never resubmit requests')
    parser.add_argument('--development', action='store_true', help='Independent arithmetic modules, AST-validated before fixed tests')
    parser.add_argument('--start-pending', action='store_true', help='Explicitly start blocked tasks which have no execution attempts')
    args = parser.parse_args()
    run_id = args.label + '-' + uuid.uuid4().hex[:10]
    evidence_path = ROOT / 'outputs' / (run_id + '.json')
    resumed = None
    if args.resume:
        evidence_path = (ROOT / 'outputs' / args.resume).resolve()
        if evidence_path.parent != (ROOT / 'outputs').resolve():
            raise ValueError('Resume path must be a report filename')
        resumed = json.loads(evidence_path.read_text(encoding='utf-8'))
        run_id = resumed['run_id']
    async with httpx.AsyncClient(base_url='http://127.0.0.1:8765', trust_env=False, timeout=45) as client:
        async def api(path, payload=None):
            response = await (client.get('/api' + path) if payload is None else client.post('/api' + path, json=payload))
            response.raise_for_status()
            return response.json()
        snapshot = await api('/agents')
        selected = [a for a in snapshot if a.get('ready') and (not args.agents or a['id'] in args.agents)]
        evidence = resumed or {'run_id': run_id, 'live': args.live, 'agents': [
            {k: a.get(k) for k in ('id', 'installed', 'authenticated', 'credential_configured', 'status', 'ready', 'binding', 'blockers')}
            for a in snapshot], 'tasks': [], 'websocket': [], 'merged': False}
        def save():
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        save()
        if not args.live:
            print(json.dumps({'candidates': [a['id'] for a in selected], 'live': False, 'evidence': evidence_path.name}))
            return
        if args.agents and set(args.agents) - {a['id'] for a in selected}:
            raise RuntimeError('Requested runtime not ready; no substitution or retry')
        session = (await api('/session'))['token']
        client.headers['X-Nexus-Token'] = session
        cursor = (await api('/state'))['event_cursor']
        stop = asyncio.Event()
        async def observe():
            async with connect(f'ws://127.0.0.1:8765/ws?after={cursor}', origin='http://127.0.0.1:8765', subprotocols=['nexus', session]) as ws:
                while not stop.is_set():
                    try:
                        event = json.loads(await asyncio.wait_for(ws.recv(), 0.5))
                    except asyncio.TimeoutError:
                        continue
                    task_id = (event.get('payload') or {}).get('task_id')
                    if task_id in {t['id'] for t in evidence['tasks']}:
                        evidence['websocket'].append({'event_id': event['id'], 'type': event['type'], 'task_id': task_id, 'received_at': time.time()})
        watcher = asyncio.create_task(observe())
        try:
            if args.start_pending:
                if not resumed:
                    raise ValueError('--start-pending requires --resume')
                current_tasks = {t['id']: t for t in await api('/tasks')}
                for entry in evidence['tasks']:
                    task = current_tasks[entry['id']]
                    attempts = await api('/tasks/' + entry['id'] + '/attempts')
                    if task['status'] == 'done':
                        continue
                    if task['status'] != 'blocked' or attempts:
                        raise RuntimeError('Refusing to retry a model request or interrupt another execution')
                    entry['redispatch'] = await api('/tasks/' + entry['id'] + '/run', {'cost_approved': True})
                    save()
            # Explicitly selected independent tasks may run concurrently, one per runtime.
            for index, agent in enumerate([] if resumed else selected):
                filename = f'nexus-{run_id}-{agent["id"]}.json'
                expected = {'message': 'Hello Agent Nexus', 'agent': agent['id'], 'run': run_id}
                function = ('add' if index % 2 == 0 else 'multiply') if args.development else None
                if function:
                    filename = f'nexus_{run_id}_{agent["id"]}.py'.replace('-', '_')
                objective = ('Create exactly one JSON text fixture ' + filename + '. Its parsed content must equal ' + json.dumps(expected)) if not function else (
                    f'Create exactly one Python module {filename}. It must contain exactly: def {function}(a, b): return a {"+" if function == "add" else "*"} b. '
                    'No imports, decorators, annotations, defaults, docstrings or other statements. Nexus will independently validate the AST and run five fixed integer test cases. Do not claim tests were executed by you.')
                task = await api('/tasks', {'title': f'[{run_id}] {agent["id"]} {function or "JSON fixture"}',
                    'description': objective + '. No other files, no tools, no shell commands, no dependencies. This is a low-risk independently reviewable development fixture.',
                    'agent_id': agent['id'], 'auto_run': False, 'cost_approved': True})
                entry = {'id': task['id'], 'agent_id': agent['id'], 'filename': filename, 'expected': expected, 'function': function}
                evidence['tasks'].append(entry)
                entry['dispatch'] = await api('/tasks/' + task['id'] + '/run', {})
                save()
            deadline = time.monotonic() + 330
            pending = {t['id'] for t in evidence['tasks']}
            while pending and time.monotonic() < deadline:
                tasks = {t['id']: t for t in await api('/tasks')}
                for entry in evidence['tasks']:
                    task = tasks[entry['id']]
                    if entry['id'] in pending and task['status'] in ('done', 'failed', 'blocked', 'cancelled'):
                        pending.remove(entry['id'])
                        entry['task'] = task
                        entry['logs'] = await api('/logs?task_id=' + entry['id'])
                        entry['attempts'] = await api('/tasks/' + entry['id'] + '/attempts')
                        if task['status'] == 'done':
                            tree = Path(task['worktree']).resolve()
                            expected_tree = ROOT / 'work' / 'worktrees' / entry['id']
                            if tree != expected_tree.resolve():
                                raise RuntimeError('Unexpected Worktree; refused file access')
                            path = tree / entry['filename']
                            content = path.read_bytes()
                            valid = check_arithmetic_fixture(content, entry['function']) if entry.get('function') else {'valid_json': json.loads(content) == entry['expected']}
                            entry['physical_check'] = {**valid,
                                'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                            assert valid.get('passed', valid.get('valid_json')), 'Physical artifact did not meet acceptance criteria'
                            names = [entry['filename']]
                            if entry.get('function'):
                                report_name = 'nexus-test-report-' + entry['id'] + '.json'
                                report_content = json.dumps({'task_id': entry['id'], 'code_sha256': hashlib.sha256(content).hexdigest(), **valid}, ensure_ascii=False, indent=2).encode('utf-8')
                                report_path = tree / report_name
                                if report_path.exists():
                                    assert report_path.read_bytes() == report_content, 'Refusing to overwrite another report'
                                else:
                                    with report_path.open('xb') as stream:
                                        stream.write(report_content)
                                names.append(report_name)
                                entry['test_report_path'] = report_name
                            # Only the generated task artifact is committed, never the user's files.
                            env_args = ['git', '-c', 'core.hooksPath=NUL', '-c', 'commit.gpgsign=false',
                                        '-c', 'user.name=Nexus Acceptance', '-c', 'user.email=acceptance@localhost']
                            dirty = subprocess.check_output(['git', 'status', '--porcelain', '--', *names], cwd=tree)
                            for command in ((['add', '--', *names], ['commit', '--quiet', '--no-gpg-sign', '-m', 'Nexus acceptance fixture and fixed test report; pending human review']) if dirty.strip() else []):
                                result = subprocess.run([*env_args, *command], cwd=tree, capture_output=True, timeout=30)
                                if result.returncode:
                                    raise RuntimeError('Task-only checkpoint failed: ' + result.stderr.decode('utf-8', 'replace')[:500])
                            entry['review'] = await api('/tasks/' + entry['id'] + '/review')
                            entry['pending_human_review'] = True
                        print(json.dumps({'agent': entry['agent_id'], 'task': entry['id'], 'status': task['status'], 'physical': entry.get('physical_check')}, ensure_ascii=False), flush=True)
                        save()
                await asyncio.sleep(0.5)
            for task_id in pending:
                await api('/tasks/' + task_id + '/cancel', {})
            evidence['timeout_cancelled'] = sorted(pending)
            evidence['usage'] = [r for r in (await api('/usage?span=all'))['records'] if r['task_id'] in {t['id'] for t in evidence['tasks']}]
        finally:
            stop.set()
            await watcher
            save()
        print('Evidence: ' + str(evidence_path), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
