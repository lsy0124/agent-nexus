"""Durability and side-effect boundaries. All model replies are LOCAL FIXTURES."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import pytest

from nexus import main, usage, worktrees
from nexus.adapters import AgentEvent, make_adapters
from nexus.bridge_protocol import BridgeError
from nexus.reliability import Checkpoints, STEPS
from nexus.storage import Store, now
from test_reliability_audit import repository, PlanFixture


class ExecutionFixture(PlanFixture):
    def __init__(self):
        self.calls = 0
        self.mode = 'success'

    async def execute(self, context):
        self.calls += 1
        if self.mode == 'slow':
            await asyncio.sleep(60)
        if self.mode == 'network':
            raise BridgeError('network', 'LOCAL UNKNOWN NETWORK RESULT FIXTURE')
        await asyncio.sleep(.1)
        yield AgentEvent('completion', 'LOCAL FILE FIXTURE', {'plan': {
            'summary': 'LOCAL FILE FIXTURE', 'files': [{'path': 'fixture.txt', 'content': 'LOCAL FIXTURE'}],
            'approval_requests': []}, 'usage': {'input_tokens': 4, 'output_tokens': 2}, 'exit_code': 0})


def app_for(root, monkeypatch, adapter):
    adapters = make_adapters(root, False)
    adapters[adapter.id] = adapter
    monkeypatch.setattr(main, 'make_adapters', lambda *a: adapters)
    return main.create_app(root, enable_codex=False)


def authorized(app):
    client = TestClient(app)
    client.__enter__()
    client.headers['X-Nexus-Token'] = app.state.token
    return client


def wait(client, task_id):
    for _ in range(150):
        task = next(t for t in client.get('/api/tasks').json() if t['id'] == task_id)
        if task['status'] != 'running':
            return task
        time.sleep(.03)
    raise AssertionError('Local fixture did not settle')


def seed_running(root, stage):
    store = Store(root)
    task_id, attempt_id = 'c0123456789a', 'd0123456789a'
    tree = root / 'work/worktrees' / task_id
    stamp = now()
    with store.connect() as db:
        db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,input_files,worktree,created_at,updated_at) "
                   "VALUES(?,?,?,'deepseek-gateway','agent','running','normal','[]','[]',?,?,?)",
                   (task_id, 'LOCAL RECOVERY FIXTURE', 'LOCAL FIXTURE', str(tree), stamp, stamp))
        db.execute("INSERT INTO attempts(id,task_id,purpose,status,agent_id,created_at) VALUES(?,?,'task','running','deepseek-gateway',?)", (attempt_id, task_id, stamp))
        task = store.tasks(db)[0]
        manager = Checkpoints(root, store)
        manager.reserve(db, attempt_id, task, 'deepseek-gateway', {'provider': {'id': 'gonode-gateway'},
            'model': {'key': 'gonode-gateway/deepseek-v4.1-flash'}, 'model_id': 'deepseek-v4.1-flash'})
        for step in STEPS[1:STEPS.index(stage) + 1]:
            manager.advance(db, attempt_id, step, known_result='REMOTE_RESULT_UNKNOWN' if step == 'REQUEST_STARTED' else 'NOT_STARTED')
    return store, manager, task_id, attempt_id


def test_ledger_survives_new_store_and_never_accepts_repeated_write_boundary(tmp_path):
    store, manager, task_id, attempt_id = seed_running(tmp_path, 'WRITING_FILES')
    with store.connect() as db:
        with pytest.raises(ValueError):
            manager.advance(db, attempt_id, 'WRITING_FILES')
        with pytest.raises(ValueError):
            manager.advance(db, attempt_id, 'REQUEST_STARTED')
    restored = Store(tmp_path).tasks()[0]
    assert restored['id'] == task_id and restored['checkpoint']['stage'] == 'WRITING_FILES'
    assert restored['checkpoint']['payload']['authorized_directory'].endswith(task_id)
    assert 'INPUTS_VALIDATED' in restored['checkpoint']['payload']['completed_steps']
    assert restored['checkpoint']['payload']['retry_count'] == 0


@pytest.mark.parametrize('stage', ['RESERVED', 'REQUEST_STARTED', 'WRITING_FILES', 'SNAPSHOTTING'])
def test_restart_restores_paused_evidence_without_replaying(tmp_path, monkeypatch, stage):
    store, manager, task_id, attempt_id = seed_running(tmp_path, stage)
    adapter = ExecutionFixture()
    app = app_for(tmp_path, monkeypatch, adapter)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        task = next(t for t in client.get('/api/tasks').json() if t['id'] == task_id)
        assert task['execution_state'] == 'PAUSED' and task['recovery_hold']
        checkpoint = task['checkpoint']
        assert checkpoint['stage'] == stage and checkpoint['state'] == 'PAUSED'
        assert client.post(f'/api/tasks/{task_id}/run', json={'cost_approved': True}).status_code == 409
        preview = client.get(f'/api/tasks/{task_id}/recovery').json()
        assert preview['can_release'] == (stage not in ('WRITING_FILES', 'SNAPSHOTTING'))
        assert client.get('/api/execution/preview', params={'task_id': task_id}).json()['eligible'] is False
        assert adapter.calls == 0
        rows = client.get('/api/usage', params={'range': 'all'}).json()['records']
        assert len(rows) == (0 if stage == 'RESERVED' else 1)
        if rows:
            assert rows[0]['total_tokens'] is None and rows[0]['usage_source'] == 'unavailable'


def test_duplicate_execution_key_survives_concurrency_completion_and_restart(tmp_path, monkeypatch):
    repository(tmp_path)
    adapter = ExecutionFixture()
    app = app_for(tmp_path, monkeypatch, adapter)
    body = {'cost_approved': True, 'execution_key': 'LOCAL_EXECUTION_INTENT_0001'}
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        task = client.post('/api/tasks', json={'title': 'LOCAL IDEMPOTENCY FIXTURE', 'agent_id': adapter.id}).json()
        endpoint = f"/api/tasks/{task['id']}/run"
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: client.post(endpoint, json=body), range(2)))
        assert all(r.status_code == 200 and r.json()['accepted'] for r in responses)
        assert sum(bool(r.json().get('replayed')) for r in responses) == 1
        done = wait(client, task['id'])
        assert done['status'] == 'done' and done['checkpoint']['state'] == 'COMPLETED'
        artifact = Path(done['worktree']) / 'fixture.txt'
        before = artifact.read_bytes(), artifact.stat().st_mtime_ns, worktrees.command(Path(done['worktree']), ['rev-parse', 'HEAD'])
        assert client.post(endpoint, json=body).json()['replayed'] is True
        assert client.post(endpoint, json={**body, 'cost_approved': False}).status_code == 409
        assert len(client.get(f"/api/tasks/{task['id']}/attempts").json()) == 1 and adapter.calls == 1
    next_adapter = ExecutionFixture()
    with TestClient(app_for(tmp_path, monkeypatch, next_adapter)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        replay = client.post(endpoint, json=body).json()
        assert replay['replayed'] and next_adapter.calls == 0
        assert before == (artifact.read_bytes(), artifact.stat().st_mtime_ns, worktrees.command(Path(done['worktree']), ['rev-parse', 'HEAD']))
        assert client.get('/api/usage', params={'range': 'all'}).json()['totals']['requests'] == 1


def test_unknown_request_requires_explicit_review_then_separate_new_attempt(tmp_path, monkeypatch):
    repository(tmp_path)
    adapter = ExecutionFixture(); adapter.mode = 'network'
    app = app_for(tmp_path, monkeypatch, adapter)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        task = client.post('/api/tasks', json={'title': 'LOCAL UNKNOWN FIXTURE', 'agent_id': adapter.id}).json()
        endpoint = f"/api/tasks/{task['id']}"
        key = {'cost_approved': True, 'execution_key': 'LOCAL_UNKNOWN_INTENT_001'}
        assert client.post(endpoint + '/run', json=key).json()['accepted']
        task = wait(client, task['id'])
        assert task['execution_state'] == 'PAUSED' and adapter.calls == 1
        assert client.post(endpoint + '/run', json=key).json()['replayed']
        assert client.post(endpoint + '/run', json={'cost_approved': True}).status_code == 409
        checkpoint = client.get(endpoint + '/recovery').json()['checkpoint']
        review = {'checkpoint_id': checkpoint['id'], 'version': checkpoint['version'], 'reviewer': 'LOCAL FIXTURE', 'note': 'LOCAL MANUAL ACKNOWLEDGEMENT'}
        assert client.post(endpoint + '/recovery/review', json=review).status_code == 403
        assert client.post(endpoint + '/recovery/review', json={**review, 'version': 1, 'acknowledge_unknown_result': True}).status_code == 409
        released = client.post(endpoint + '/recovery/review', json={**review, 'acknowledge_unknown_result': True}).json()
        assert released == {'released': True, 'executed': False} and adapter.calls == 1
        assert client.post(endpoint + '/recovery/review', json={**review, 'acknowledge_unknown_result': True}).status_code == 409
        adapter.mode = 'success'
        assert client.post(endpoint + '/run', json={'cost_approved': True, 'execution_key': 'LOCAL_UNKNOWN_NEW_INTENT_02'}).json()['accepted']
        assert wait(client, task['id'])['status'] == 'done' and adapter.calls == 2


def test_pause_persists_without_blocking_websocket_or_other_work(tmp_path, monkeypatch):
    repository(tmp_path)
    adapter = ExecutionFixture(); adapter.mode = 'slow'
    app = app_for(tmp_path, monkeypatch, adapter)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        task = client.post('/api/tasks', json={'title': 'LOCAL PAUSE FIXTURE', 'agent_id': adapter.id}).json()
        assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()['accepted']
        for _ in range(100):
            if adapter.calls:
                break
            time.sleep(.02)
        with client.websocket_connect('/ws', headers={'origin': 'http://127.0.0.1:8765'}, subprotocols=['nexus', app.state.token]) as ws:
            ws.send_text('ping')
            for _ in range(300):
                if ws.receive_json().get('type') == 'pong':
                    break
            else:
                raise AssertionError('Heartbeat blocked by a long task')
        paused = client.post(f"/api/tasks/{task['id']}/pause").json()
        assert paused['execution_state'] == 'PAUSED' and paused['recovery_hold']
        assert paused['checkpoint']['payload']['known_result'] == 'REMOTE_RESULT_UNKNOWN'
        assert client.get('/api/health').json()['status'] == 'ok'
        assert adapter.calls == 1


def test_completed_files_are_not_replayed_after_bookkeeping_failure(tmp_path, monkeypatch):
    repository(tmp_path)
    adapter = ExecutionFixture()
    app = app_for(tmp_path, monkeypatch, adapter)
    original = usage.UsageCollector.record
    with monkeypatch.context() as patch:
        def unavailable(*args, **kwargs):
            raise RuntimeError('LOCAL BOOKKEEPING CRASH FIXTURE')
        patch.setattr(usage.UsageCollector, 'record', unavailable)
        with TestClient(app) as client:
            client.headers['X-Nexus-Token'] = app.state.token
            task = client.post('/api/tasks', json={'title': 'LOCAL BOOKKEEPING FIXTURE', 'agent_id': adapter.id}).json()
            assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()['accepted']
            done = wait(client, task['id'])
            assert done['status'] == 'done' and done['checkpoint']['state'] == 'COMPLETED'
    assert usage.UsageCollector.record == original
    next_adapter = ExecutionFixture()
    for _ in range(2):
        with TestClient(app_for(tmp_path, monkeypatch, next_adapter)) as client:
            client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
            assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).status_code == 409
            rows = client.get('/api/usage', params={'range': 'all'}).json()['records']
            assert len(rows) == 1 and rows[0]['total_tokens'] == 6 and rows[0]['total_source'] == 'derived'
            assert next_adapter.calls == 0


def test_real_interpreter_crash_after_file_write_is_never_automatically_replayed(tmp_path, monkeypatch):
    repository(tmp_path)
    root = Path(__file__).resolve().parents[2]
    code = '''
import os,sys,time
from pathlib import Path
from fastapi.testclient import TestClient
from nexus import main
from nexus.adapters import make_adapters
from test_reliability_audit import PlanFixture
root=Path(sys.argv[1]); adapters=make_adapters(root,False); adapters['deepseek-gateway']=PlanFixture()
main.make_adapters=lambda *args: adapters
main.worktrees.snapshot=lambda *args: os._exit(77)
app=main.create_app(root,enable_codex=False)
with TestClient(app) as client:
 client.headers['X-Nexus-Token']=app.state.token
 task=client.post('/api/tasks',json={'title':'LOCAL HARD CRASH FIXTURE','agent_id':'deepseek-gateway'}).json()
 (root/'fixture-task-id.txt').write_text(task['id'])
 client.post('/api/tasks/'+task['id']+'/run',json={'cost_approved':True})
 time.sleep(30)
raise RuntimeError('Fixture crash barrier was not reached')
'''
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(root/'backend'), str(root/'backend/tests')]),
                   'NEXUS_KEY_VAULT': 'memory', 'NEXUS_SECRET_DIR': str(tmp_path/'work/secrets')}
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path)], env=environment, capture_output=True, timeout=90)
    assert child.returncode == 77, child.stderr.decode('utf-8', 'replace')
    task_id = (tmp_path/'fixture-task-id.txt').read_text()
    tree = tmp_path/'work/worktrees'/task_id
    written = (tree/'hello.txt').read_bytes(), (tree/'hello.txt').stat().st_mtime_ns
    adapter = ExecutionFixture()
    with TestClient(app_for(tmp_path, monkeypatch, adapter)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        task = next(t for t in client.get('/api/tasks').json() if t['id'] == task_id)
        assert task['execution_state'] == 'PAUSED' and task['checkpoint']['stage'] == 'SNAPSHOTTING'
        preview = client.get(f'/api/tasks/{task_id}/recovery').json()
        assert preview['can_release'] is False and all(f['matches_saved_result'] for f in preview['files'])
        checkpoint = preview['checkpoint']
        assert client.post(f'/api/tasks/{task_id}/recovery/review', json={'checkpoint_id': checkpoint['id'], 'version': checkpoint['version'],
            'reviewer':'LOCAL FIXTURE','note':'Do not replay writes','acknowledge_unknown_result':True}).status_code == 409
        assert client.post(f'/api/tasks/{task_id}/run', json={'cost_approved':True}).status_code == 409
        assert written == ((tree/'hello.txt').read_bytes(), (tree/'hello.txt').stat().st_mtime_ns)
        assert adapter.calls == 0
