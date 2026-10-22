"""Local model fixtures exercise planning -> import -> parallel execution, no API."""
import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

from nexus.adapters import AgentEvent
from nexus.planning import PLAN_PATH, parse_spec
from test_orchestration import FileFixture, setup, wait


def specification():
    return {'max_parallel': 2, 'nodes': [
        {'key': 'quote', 'title': '报价校验', 'objective': '实现缺失报价识别，返回明确错误码',
         'acceptance': '空报价返回 missing_quote；有效报价保持原值；提供验收方法，不宣称运行',
         'agent_id': 'deepseek-gateway', 'directory': 'delivery/quote',
         'deliverables': ['delivery/quote/validation.json'], 'input_files': [], 'dependencies': []},
        {'key': 'portfolio', 'title': '持仓数据契约', 'objective': '定义持仓数量与金额数据字段',
         'acceptance': '字段类型和缺失值策略明确', 'agent_id': 'mimo', 'directory': 'delivery/portfolio',
         'deliverables': ['delivery/portfolio/schema.json'], 'input_files': [], 'dependencies': []}]}


class PlannerFixture(FileFixture):
    async def execute(self, context):
        self.shared['contexts'].append(context)
        self.shared['active'] += 1
        self.shared['max_active'] = max(self.shared['max_active'], self.shared['active'])
        try:
            if '你是项目规划者' in context.objective:
                files = [{'path': PLAN_PATH, 'content': json.dumps(self.shared.get('spec', specification()), ensure_ascii=False)}]
            else:
                barrier = self.shared.setdefault('parallel_barrier', asyncio.Event())
                if self.shared['active'] == 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=10)
                name = re.search(r'本任务只交付这些文件：([^。]+)', context.objective)[1]
                files = [{'path': name, 'content': '{"fixture_only":true}'}]
            if self.shared.get('extra_file'):
                files.append({'path': 'outside.txt', 'content': 'LOCAL FIXTURE'})
            yield AgentEvent('completion', 'LOCAL FIXTURE, NOT A REAL MODEL', {
                'plan': {'summary': 'LOCAL FIXTURE', 'files': files, 'approval_requests': []},
                'usage': {'input_tokens': 10, 'output_tokens': 20}, 'exit_code': 0})
        finally:
            self.shared['active'] -= 1


def prepare(setup, agent_id='deepseek-gateway'):
    client, app, parent, endpoint, shared, root = setup
    for key in ('deepseek-gateway', 'mimo'):
        app.state.adapters[key] = PlannerFixture(key, shared)
    response = client.post(f"/api/tasks/{parent['id']}/planning", json={'agent_id': agent_id})
    assert response.status_code == 200, response.text
    return response.json()


def execute(client, tid):
    preview = client.get('/api/execution/preview', params={'task_id': tid}).json()
    assert preview['eligible'], preview
    response = client.post('/api/execution/run', json={'task_id': tid, 'revision': preview['revision'], 'approve_usage': True})
    assert response.status_code == 200 and response.json()['accepted'], response.text


def finished(client, tid):
    import time
    for _ in range(100):
        task = next(t for t in client.get('/api/tasks').json() if t['id'] == tid)
        if task['status'] in ('done', 'failed', 'blocked', 'cancelled'):
            return task
        time.sleep(.05)
    raise AssertionError(task)


def test_planning_import_and_real_scheduler_parallel_writes_without_merge(setup):
    client, app, parent, endpoint, shared, root = setup
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root)
    original = client.post(endpoint + '/draft').json()
    run = prepare(setup)
    assert not shared['contexts']  # Preparing never makes a model call.
    with app.state.store.connect() as db:
        assert db.execute('SELECT cost_approved FROM tasks WHERE id=?', (run['task_id'],)).fetchone()[0] == 0
    execute(client, run['task_id'])
    task = finished(client, run['task_id'])
    assert task['status'] == 'done', task
    assert [a['path'] for a in task['last_attempt']['artifacts']] == [PLAN_PATH]
    run = client.get(f"/api/tasks/{parent['id']}/planning").json()['runs'][0]
    assert run['specification']['nodes'][0]['key'] == 'quote'
    body = {'task_id': task['id'], 'manifest': run['manifest'], 'approve_import': True}
    url = f"/api/tasks/{parent['id']}/planning/import"
    assert client.post(url, json={**body, 'approve_import': False}).status_code == 403
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['planner'] == 'model-spec' and plan['parent_id'] != parent['id']
    assert client.get(endpoint).json()['revision'] == original['revision']
    assert all(not n['cost_approved'] and not n.get('task_id') for n in plan['nodes'])
    assert len(shared['contexts']) == 1
    assert client.post(url, json=body).status_code == 409
    target = '/api/tasks/' + plan['parent_id'] + '/collaboration'
    for node in plan['nodes']:
        node['cost_approved'] = True
    saved = client.put(target, json=plan)
    assert saved.status_code == 200, saved.text
    plan = saved.json()
    assert client.post(target + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    done = wait(client, target, lambda p: all(n['task']['status'] == 'done' for n in p['nodes']))
    assert len(shared['contexts']) == 3 and shared['max_active'] == 2
    assert len({n['task']['worktree'] for n in done['nodes']}) == 2
    for node in done['nodes']:
        assert node['task']['review_status'] == 'pending'
        path = Path(node['task']['worktree']) / node['deliverables'][0]
        assert json.loads(path.read_text()) == {'fixture_only': True}
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root) == head
    assert not (root / 'delivery').exists()
    assert client.post(target + '/pause').status_code == 200


@pytest.mark.parametrize('mutation', ['extra', 'approval', 'outside', 'generic', 'input', 'same-agent', 'cycle', 'too-many-files'])
def test_untrusted_planner_spec_rejected(setup, mutation):
    client, app, parent, endpoint, shared, root = setup
    spec = specification()
    first = spec['nodes'][0]
    if mutation == 'extra': first['task_id'] = 'injected'
    if mutation == 'approval': first['cost_approved'] = True
    if mutation == 'outside': first['deliverables'] = ['outside.txt']
    if mutation == 'generic': first['deliverables'] = []
    if mutation == 'input': first['input_files'] = ['README.md']
    if mutation == 'same-agent': spec['nodes'][1]['agent_id'] = first['agent_id']
    if mutation == 'cycle': first['dependencies'] = ['portfolio']; spec['nodes'][1]['dependencies'] = ['quote']
    if mutation == 'too-many-files': first['deliverables'] = ['delivery/quote/' + str(i) + '.txt' for i in range(4)]
    with pytest.raises(ValueError):
        parse_spec(json.dumps(spec), next(t for t in client.get('/api/tasks').json() if t['id'] == parent['id']), app.state.registry)


def test_invalid_planning_output_never_written_and_no_retry(setup):
    client, app, parent, endpoint, shared, root = setup
    run = prepare(setup)
    shared['extra_file'] = True
    execute(client, run['task_id'])
    task = finished(client, run['task_id'])
    assert task['status'] == 'failed'
    assert not (Path(task['worktree']) / PLAN_PATH).exists()
    assert not (Path(task['worktree']) / 'outside.txt').exists()
    assert len(shared['contexts']) == 1


def test_mimo_planning_profile_is_persisted_and_is_not_development_verification(setup):
    client, app, parent, endpoint, shared, root = setup
    run = prepare(setup, 'mimo')
    execute(client, run['task_id'])
    task = finished(client, run['task_id'])
    assert task['status'] == 'done', task
    assert task['planning_parent'] == parent['id']
    assert task['last_attempt']['request_profile'] == {'stream': True, 'max_output_tokens': 8192, 'reasoning': 'none', 'purpose': 'planning'}
    assert shared['contexts'][0].planning_only is True
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM verifications WHERE kind='task_execution_capable'").fetchone()[0] == 0


def test_tampered_or_stale_spec_cannot_import(setup):
    client, app, parent, endpoint, shared, root = setup
    run = prepare(setup)
    execute(client, run['task_id'])
    task = finished(client, run['task_id'])
    run = client.get(f"/api/tasks/{parent['id']}/planning").json()['runs'][0]
    url = f"/api/tasks/{parent['id']}/planning/import"
    body = {'task_id': task['id'], 'manifest': run['manifest'], 'approve_import': True}
    assert client.post(url, json={**body, 'manifest': 'stale'}).status_code == 409
    path = Path(task['worktree']) / PLAN_PATH
    old = path.read_bytes()
    path.write_bytes(old + b' ')
    assert client.get(f"/api/tasks/{parent['id']}/planning").json()['runs'][0]['specification'] is None
    assert client.post(url, json=body).status_code == 409
    path.write_bytes(old)
    assert client.put('/api/tasks/' + parent['id'], json={'title': 'CHANGED', 'agent_id': 'auto'}).status_code == 200
    assert client.post(url, json=body).status_code == 409


def test_atomic_scope_and_selected_context_enforced(setup):
    client, app, parent, endpoint, shared, root = setup
    client.put('/api/tasks/' + parent['id'], json={'title': parent['title'], 'agent_id': 'auto', 'input_files': ['README.md']})
    plan = client.post(endpoint + '/draft').json()
    for node in plan['nodes']:
        node.update(input_files=[], deliverables=[node['directory'] + '/required.txt'], cost_approved=True)
    saved = client.put(endpoint, json=plan).json()
    assert client.post(endpoint + '/start', json={'revision': saved['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: p['state'] == 'paused')
    assert all(not c.input_files for c in shared['contexts'])
    assert any(n['task']['status'] == 'failed' for n in plan['nodes'])
    assert all(not (Path(n['task']['worktree']) / n['directory']).exists() for n in plan['nodes'] if n['task']['worktree'])


def test_failed_model_spec_can_be_revised_locally_without_request_or_false_success(setup):
    client, app, parent, endpoint, shared, root = setup
    run = prepare(setup)
    spec = specification()
    spec['nodes'][0]['deliverables'] = ['delivery/quote/auth.py']
    shared['spec'] = spec
    execute(client, run['task_id'])
    failed = finished(client, run['task_id'])
    assert failed['status'] == 'failed' and failed['last_attempt']['artifacts'] == []
    planning_url = f"/api/tasks/{parent['id']}/planning"
    run = client.get(planning_url).json()['runs'][0]
    assert run['candidate'] and run['specification'] is None
    assert run['revisions'] == []
    body = {'task_id': run['task_id'], 'candidate_digest': run['candidate_digest'], 'specification': run['candidate']}
    assert client.post(planning_url + '/revise', json=body).status_code == 422
    corrected = json.loads(run['candidate'])
    corrected['nodes'][0]['deliverables'] = ['delivery/quote/access_control.py']
    body['specification'] = json.dumps(corrected)
    assert client.post(planning_url + '/revise', json={**body, 'candidate_digest': 'changed'}).status_code == 409
    response = client.post(planning_url + '/revise', json=body)
    assert response.status_code == 200, response.text
    revision = response.json()
    assert len(shared['contexts']) == 1
    current = next(t for t in client.get('/api/tasks').json() if t['id'] == failed['id'])
    assert current['status'] == 'failed' and current['last_attempt'] == failed['last_attempt']
    request = {'task_id': run['task_id'], 'manifest': revision['manifest'], 'revision_id': revision['id']}
    assert client.post(planning_url + '/import', json=request).status_code == 403
    assert client.post(planning_url + '/import', json={**request, 'approve_import': True, 'manifest': 'changed'}).status_code == 409
    response = client.post(planning_url + '/import', json={**request, 'approve_import': True})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['planner'] == 'model-spec-revised' and plan['planning_revision_id'] == revision['id']
    assert all(not n.get('task_id') and not n['cost_approved'] for n in plan['nodes'])
    assert len(shared['contexts']) == 1
    assert client.post(planning_url + '/import', json={**request, 'approve_import': True}).status_code == 409
    # The revised plan cannot remove the file contract after import.
    plan['nodes'][0]['deliverables'] = []
    assert client.put('/api/tasks/' + plan['parent_id'] + '/collaboration', json=plan).status_code == 422


def test_local_revision_cannot_inject_approval_or_recover_truncated_output(setup):
    client, app, parent, endpoint, shared, root = setup
    run = prepare(setup)
    planning_url = f"/api/tasks/{parent['id']}/planning"
    body = {'task_id': run['task_id'], 'candidate_digest': 'none', 'specification': json.dumps(specification())}
    assert client.post(planning_url + '/revise', json=body).status_code == 409
    execute(client, run['task_id'])
    assert finished(client, run['task_id'])['status'] == 'done'
    run = client.get(planning_url).json()['runs'][0]
    malicious = specification()
    malicious['nodes'][0]['cost_approved'] = True
    response = client.post(planning_url + '/revise', json={**body, 'candidate_digest': run['candidate_digest'], 'specification': json.dumps(malicious)})
    assert response.status_code == 422
    assert len(shared['contexts']) == 1
