"""Real scheduler/worktrees, labelled in-process model fixtures. No paid calls."""
import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from nexus import main, orchestration
from nexus.adapters import AgentEvent, make_adapters
from nexus.storage import now


class FileFixture:
    transport = 'local-orchestration-fixture'
    name = 'LOCAL COLLABORATION FIXTURE'
    version = 'fixture'
    supported_capabilities = capabilities = ('file.generate', 'file.edit')
    verified_at = None
    ready = True

    def __init__(self, agent_id, shared):
        self.id, self.shared = agent_id, shared
        self.model = 'LOCAL FIXTURE MODEL'

    async def detect(self):
        return self.probe()

    def probe(self):
        return {'id': self.id, 'name': self.name, 'ready': True, 'busy': False, 'verified_at': self.verified_at,
                'status': 'disconnected' if not self.verified_at else 'connected', 'capabilities': list(self.capabilities), 'detail': 'LOCAL FIXTURE'}

    async def execute(self, context):
        self.shared['contexts'].append(context)
        self.shared['active'] += 1
        self.shared['max_active'] = max(self.shared['max_active'], self.shared['active'])
        try:
            await asyncio.sleep(1.2)
            directory = re.search(r'只允许输出目录：([^；]+)；', context.objective)[1].rstrip('/')
            path = 'outside.txt' if self.shared.get('escape') else directory + '/fixture.txt'
            yield AgentEvent('completion', 'LOCAL FIXTURE NOT REAL MODEL', {'plan': {'summary': 'LOCAL FIXTURE', 'files': [{'path': path, 'content': 'LOCAL COLLABORATION FIXTURE'}], 'approval_requests': []}, 'usage': {'input_tokens': 4, 'output_tokens': 2}, 'exit_code': 0})
        finally:
            self.shared['active'] -= 1

    async def cancel(self, task_id):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / 'README.md').write_text('LOCAL FIXTURE')
    for args in (['init', '-q'], ['add', 'README.md'], ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-qm', 'fixture']):
        subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, check=True)
    shared = {'contexts': [], 'active': 0, 'max_active': 0}
    adapters = make_adapters(tmp_path, False)
    for key in ('deepseek-gateway', 'mimo'):
        adapters[key] = FileFixture(key, shared)
    monkeypatch.setattr(main, 'make_adapters', lambda *a: adapters)
    app = main.create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        parent = client.post('/api/tasks', json={'title': 'LOCAL COLLABORATION GOAL', 'agent_id': 'auto'}).json()
        endpoint = f"/api/tasks/{parent['id']}/collaboration"
        yield client, app, parent, endpoint, shared, tmp_path


def configured(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = client.post(endpoint + '/draft').json()
    for node in plan['nodes']:
        node['cost_approved'] = True
    response = client.put(endpoint, json=plan)
    assert response.status_code == 200, response.text
    return response.json()


def wait(client, endpoint, predicate):
    for _ in range(160):
        plan = client.get(endpoint).json()
        if predicate(plan):
            return plan
        time.sleep(.05)
    raise AssertionError(json.dumps(plan))


def test_parallel_tasks_review_dependencies_context_and_no_implicit_merge(setup):
    client, app, parent, endpoint, shared, root = setup
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root)
    plan = configured(setup)
    assert client.post(f"/api/tasks/{parent['id']}/run").status_code == 409
    assert client.post(endpoint + '/start', json={'revision': 'old', 'approve_plan': True}).status_code == 409
    response = client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True})
    assert response.status_code == 200, response.text
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    assert shared['max_active'] == 2
    assert len(shared['contexts']) == 2
    assert plan['nodes'][2]['task']['status'] == 'backlog'
    assert len({n['task']['worktree'] for n in plan['nodes'][:2]}) == 2
    child = plan['nodes'][0]['task_id']
    assert client.post(f'/api/tasks/{child}/run', json={'cost_approved': True}).status_code == 409
    for node in plan['nodes'][:2]:
        assert client.post(endpoint + '/review/' + node['key'], json={'decision': 'approve', 'manifest': 'old', 'reviewer': 'LOCAL FIXTURE', 'note': 'fixture review'}).status_code == 409
        response = client.post(endpoint + '/review/' + node['key'], json={'decision': 'approve', 'manifest': node['manifest'], 'reviewer': 'LOCAL FIXTURE', 'note': 'fixture bytes reviewed, not model test evidence'})
        assert response.status_code == 200, response.text
    plan = wait(client, endpoint, lambda p: p['nodes'][2]['task']['status'] == 'done')
    assert len(shared['contexts']) == 3
    assert len(shared['contexts'][2].input_files) == 2
    assert all('LOCAL COLLABORATION FIXTURE' in f['content'] for f in shared['contexts'][2].input_files)
    assert plan['state'] in ('running', 'waiting_review')
    assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root) == head
    assert all(n['task']['review_status'] == 'pending' for n in plan['nodes'])
    assert not (root / 'nexus-delivery').exists()
    assert client.post(endpoint + '/pause').status_code == 200


def test_approval_required_and_dag_scopes_validated_before_any_call(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = client.post(endpoint + '/draft').json()
    response = client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True})
    assert response.status_code == 403
    assert len(client.get('/api/tasks').json()) == 1
    assert not shared['contexts']
    plan['nodes'][0]['dependencies'] = ['integration']
    assert client.put(endpoint, json=plan).status_code == 422
    plan['nodes'][0]['dependencies'] = []
    plan['nodes'][1]['directory'] = plan['nodes'][0]['directory'] + '/nested'
    assert client.put(endpoint, json=plan).status_code == 422
    plan['nodes'][1]['directory'] = '../escape'
    assert client.put(endpoint, json=plan).status_code == 422


def test_scope_escape_fails_without_writing_and_never_retries(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    shared['escape'] = True
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: p['state'] == 'paused')
    assert any(n['task']['status'] == 'failed' for n in plan['nodes'])
    before = len(shared['contexts'])
    time.sleep(1.5)
    assert len(shared['contexts']) == before
    assert all(not (Path(n['task']['worktree']) / 'outside.txt').exists() for n in plan['nodes'] if n['task']['worktree'])
    rejected = next(n for n in plan['nodes'] if n['task']['status'] == 'failed')
    message = rejected['task']['result'] or ''
    # A billed rejection must stay diagnosable: name what was refused and what was
    # allowed, without retaining or logging any generated file content.
    assert 'outside.txt' in message
    assert rejected['directory'] + '/' in message
    assert rejected['task']['execution_state'] == 'BLOCKED_PERMISSION'
    state = next(a for a in client.get('/api/agents').json() if a['id'] == rejected['agent_id'])
    assert state['status'] == 'connected'
    assert state['last_task_error']['state'] == 'BLOCKED_PERMISSION'


def test_child_tasks_never_inherit_a_planner_prompt(setup):
    """A collaboration can be created from a planning task, whose description is the
    planner's own instruction set ("只返回一个文件 nexus-plan/tasks.json"). Handing that
    to an executing child orders it to emit a plan file, which the approved-directory
    guard then rejects. The child must see the real goal instead."""
    client, app, parent, endpoint, shared, root = setup
    planner_marker = '你是项目规划者。本任务只产出任务规格，不写实现代码'
    plan_file = 'nexus-plan/tasks.json'
    with app.state.store.connect() as db:
        db.execute(
            "INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files,cost_approved)"
            " VALUES(?,?,?,?,?,'backlog',?,?,NULL,NULL,?,?,?,0)",
            ('goalfixture1', 'REAL GOAL TITLE', 'REAL GOAL REQUIREMENTS', 'auto', 'agent', 'normal', '[]', now(), now(), '[]'))
        db.execute("UPDATE tasks SET description=? WHERE id=?",
                   (planner_marker + ' 外层文件方案中只返回一个文件 ' + plan_file + ' ，其 content 为纯 JSON。', parent['id']))
        db.execute('INSERT INTO planning_runs VALUES(?,?,?,NULL,?)', (parent['id'], 'goalfixture1', 'fixture-revision', now()))
    plan = configured(setup)
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    child_ids = {n['task_id'] for n in app.state.collaborator.load(parent['id'])['nodes']}
    assert child_ids
    for child in client.get('/api/tasks').json():
        if child['id'] not in child_ids:
            continue
        assert planner_marker not in child['description']
        assert plan_file not in child['description']
        assert 'REAL GOAL TITLE' in child['description']
        assert 'REAL GOAL REQUIREMENTS' in child['description']
        assert '只允许输出目录：' in child['description']


def test_restart_pauses_and_does_not_restart_paid_nodes(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    # Persist an armed queue without ever launching a request.
    plan = app.state.collaborator.load(parent['id'])
    plan['state'] = 'running'
    app.state.collaborator.save(plan)
    orchestration.Collaborator(root, app.state.store, app.state.registry, lambda _: None, lambda _: None, None, None, asyncio.Lock())
    assert app.state.collaborator.load(parent['id'])['state'] == 'paused'
    assert not shared['contexts']


def test_changed_artifact_cannot_be_approved_for_dependency(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    node = plan['nodes'][0]
    artifact = node['task']['last_attempt']['artifacts'][0]
    (Path(node['task']['worktree']) / artifact['path']).write_text('LOCAL TAMPER FIXTURE')
    response = client.post(endpoint + '/review/' + node['key'], json={'decision': 'approve', 'manifest': node['manifest'], 'reviewer': 'LOCAL FIXTURE', 'note': 'must refuse changed bytes'})
    assert response.status_code == 409
    assert not client.get(endpoint).json()['nodes'][0].get('review')
    assert len(shared['contexts']) == 2
    assert client.post(endpoint + '/pause').status_code == 200


def test_changed_provider_or_model_pauses_before_next_paid_call(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    pending = plan['nodes'][2]
    app.state.adapters[pending['agent_id']].model = 'CHANGED LOCAL FIXTURE MODEL'
    for node in plan['nodes'][:2]:
        assert client.post(endpoint + '/review/' + node['key'], json={'decision': 'approve', 'manifest': node['manifest'], 'reviewer': 'LOCAL FIXTURE', 'note': 'review fixed fixture bytes'}).status_code == 200
    plan = wait(client, endpoint, lambda p: p['state'] == 'paused')
    assert len(shared['contexts']) == 2
    assert 'Provider' in plan['nodes'][2]['error']


def test_valid_graph_may_list_dependents_before_prerequisites(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    plan['nodes'].reverse()
    response = client.put(endpoint, json=plan)
    assert response.status_code == 200
    response = client.post(endpoint + '/start', json={'revision': response.json()['revision'], 'approve_plan': True})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert len({n['task_id'] for n in plan['nodes']}) == 3
    assert len(plan['nodes'][0]['task']['dependencies']) == 2
    assert client.post(endpoint + '/pause').status_code == 200


def test_splitting_cannot_drop_parent_capability_requirements(setup):
    client, app, parent, endpoint, shared, root = setup
    response = client.put(f"/api/tasks/{parent['id']}", json={'title': parent['title'], 'agent_id': 'auto', 'requires_skills': ['browser.test']})
    assert response.status_code == 200
    plan = configured(setup)
    response = client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True})
    assert response.status_code == 409 and '所需能力' in response.json()['detail']
    assert not shared['contexts']


def test_selected_parent_inputs_are_inherited_as_read_context(setup):
    client, app, parent, endpoint, shared, root = setup
    assert client.put(f"/api/tasks/{parent['id']}", json={'title': parent['title'], 'agent_id': 'auto', 'input_files': ['README.md']}).status_code == 200
    plan = configured(setup)
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    assert all(n['task']['input_files'] == ['README.md'] for n in plan['nodes'])
    assert all(c.input_files[0]['path'] == 'README.md' for c in shared['contexts'])
    assert client.post(endpoint + '/pause').status_code == 200


def test_external_parent_dependencies_cannot_be_bypassed(setup):
    client, app, parent, endpoint, shared, root = setup
    external = client.post('/api/tasks', json={'title': 'LOCAL EXTERNAL PREREQUISITE FIXTURE', 'agent_id': 'auto'}).json()
    assert client.put(f"/api/tasks/{parent['id']}", json={'title': parent['title'], 'agent_id': 'auto', 'dependencies': [external['id']]}).status_code == 200
    plan = configured(setup)
    body = {'revision': plan['revision'], 'approve_plan': True}
    assert client.post(endpoint + '/start', json=body).status_code == 409
    assert not shared['contexts']
    # Synthetic external completion with no artifacts: not real execution proof.
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='done',result='LOCAL STATE FIXTURE' WHERE id=?", (external['id'],))
    response = client.post(endpoint + '/start', json=body)
    assert response.status_code == 200, response.text
    assert all(external['id'] in n['task']['dependencies'] for n in response.json()['nodes'])
    assert client.post(endpoint + '/pause').status_code == 200


def test_execution_desk_revises_failed_nodes_without_replaying_history(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    shared['escape'] = True
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: p['state'] == 'paused' and all(n['task']['status'] != 'running' for n in p['nodes']))
    old = {n['key']: n['task'] for n in plan['nodes'][:2]}
    assert all(t['status'] == 'failed' for t in old.values())
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 409
    preview = client.get('/api/execution/preview', params={'task_id': parent['id']}).json()
    assert preview['eligible'], preview
    assert len(preview['selected']['repair_keys']) == 2
    body = {'task_id': parent['id'], 'revision': preview['revision'], 'approve_usage': True}
    before = len(client.get('/api/tasks').json())
    assert client.post('/api/execution/run', json=body).status_code == 403
    assert len(client.get('/api/tasks').json()) == before
    shared['escape'] = False
    body['approve_revisions'] = True
    response = client.post('/api/execution/run', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['accepted'] is True
    assert client.post('/api/execution/run', json=body).status_code == 409
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    for node in plan['nodes'][:2]:
        prior = old[node['key']]
        assert node['task_id'] != prior['id']
        assert prior['id'] in node['history']
        assert node['task']['worktree'] != prior['worktree']
        preserved = next(t for t in client.get('/api/tasks').json() if t['id'] == prior['id'])
        assert preserved['status'] == 'failed'
        assert preserved['last_attempt']['id'] == prior['last_attempt']['id']
        assert client.post('/api/tasks/' + prior['id'] + '/run').status_code == 409
    assert set(plan['nodes'][2]['task']['dependencies']) == {n['task_id'] for n in plan['nodes'][:2]}
    assert len(shared['contexts']) == 4
    for node in plan['nodes'][:2]:
        assert client.post(endpoint + '/review/' + node['key'], json={'decision': 'approve', 'manifest': node['manifest'], 'reviewer': 'LOCAL FIXTURE', 'note': 'synthetic fixture review'}).status_code == 200
    wait(client, endpoint, lambda p: p['nodes'][2]['task']['status'] == 'done')
    assert len(shared['contexts']) == 5
    assert not (root / 'nexus-delivery').exists()


def test_execution_desk_draft_requires_approval_and_stale_preview_never_calls(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    preview = client.get('/api/execution/preview', params={'task_id': parent['id']}).json()
    assert preview['eligible'], preview
    body = {'task_id': parent['id'], 'revision': preview['revision']}
    assert client.post('/api/execution/run', json=body).status_code == 403
    plan['max_parallel'] = 3
    assert client.put(endpoint, json=plan).status_code == 200
    body['approve_usage'] = True
    assert client.post('/api/execution/run', json=body).status_code == 409
    assert not shared['contexts']
    assert len(client.get('/api/tasks').json()) == 1


def test_execution_desk_recovers_submission_blocked_before_attempt(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    # Reproduce a pre-dispatch reservation left by a budget refusal or restart.
    client.post(endpoint + '/pause')
    plan = client.get(endpoint).json()
    child = plan['nodes'][2]
    assert not child['task']['last_attempt']
    loaded = app.state.collaborator.load(parent['id'])
    loaded['nodes'][2]['phase'] = 'submitted'
    app.state.collaborator.save(loaded)
    with app.state.store.connect() as db:
        db.execute("UPDATE tasks SET status='blocked' WHERE id=?", (child['task_id'],))
    preview = client.get('/api/execution/preview', params={'task_id': parent['id']}).json()
    assert child['key'] in preview['selected']['repair_keys']
    assert not any('尚未配置' in b for b in preview['blockers'])
    assert preview['eligible'], preview
    response = client.post('/api/execution/run', json={'task_id': parent['id'], 'revision': preview['revision'], 'approve_usage': True, 'approve_revisions': True})
    assert response.status_code == 200, response.text
    resumed = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    revised = resumed['nodes'][2]
    assert revised['task_id'] != child['task_id']
    assert child['task_id'] in revised['history']
    assert revised['phase'] == 'pending'
    assert revised['task']['last_attempt'] is None
    assert set(revised['task']['dependencies']) == {n['task_id'] for n in resumed['nodes'][:2]}


def test_explicit_ready_unverified_capability_is_allowed_without_claiming_verified(setup):
    client, app, parent, endpoint, shared, root = setup
    adapter = app.state.registry.adapters['mimo']
    original = adapter.probe
    adapter.probe = lambda: {**original(), 'capabilities': []}
    response = client.post('/api/tasks', json={'title': 'LOCAL DECLARED CAPABILITY FIXTURE', 'description': '只允许输出目录：fixture/；', 'agent_id': 'mimo', 'requires_skills': ['file.generate']})
    task = response.json()
    assert app.state.registry.snapshot('mimo')['status'] == 'disconnected'
    preview = client.get('/api/execution/preview', params={'task_id': task['id']}).json()
    assert preview['eligible'], preview
    result = client.post('/api/execution/run', json={'task_id': task['id'], 'revision': preview['revision'], 'approve_usage': True})
    assert result.status_code == 200 and result.json()['accepted'], result.text
    for _ in range(100):
        current = next(t for t in client.get('/api/tasks').json() if t['id'] == task['id'])
        if current['status'] != 'running': break
        time.sleep(.05)
    assert current['status'] == 'done', current
    assert len(shared['contexts']) == 1


def test_execution_desk_rechecks_task_after_async_environment_detection(setup):
    client, app, parent, endpoint, shared, root = setup
    task = client.post('/api/tasks', json={'title': 'LOCAL RACE FIXTURE', 'description': '只允许输出目录：fixture/；', 'agent_id': 'mimo'}).json()
    preview = client.get('/api/execution/preview', params={'task_id': task['id']}).json()
    assert preview['eligible'], preview
    async def changed_during_detect(agent_id):
        with app.state.store.connect() as db:
            db.execute('UPDATE tasks SET description=? WHERE id=?', ('LOCAL CHANGED OBJECTIVE', task['id']))
        return app.state.registry.snapshot(agent_id)
    app.state.registry.detect = changed_during_detect
    result = client.post('/api/execution/run', json={'task_id': task['id'], 'revision': preview['revision'], 'approve_usage': True})
    assert result.status_code == 409, result.text
    assert not shared['contexts']
    current = next(t for t in client.get('/api/tasks').json() if t['id'] == task['id'])
    assert current['last_attempt'] is None


def test_output_limit_requires_changed_allowance_before_same_scope_revision(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup); shared['escape'] = True
    assert client.post(endpoint + '/start', json={'revision':plan['revision'],'approve_plan':True}).status_code == 200
    plan = wait(client, endpoint, lambda p:p['state']=='paused' and all(n['task']['status']!='running' for n in p['nodes']))
    node=plan['nodes'][0]; adapter=app.state.registry.adapters[node['agent_id']]
    adapter.stream_enabled, adapter.max_output_tokens=False,8192
    with app.state.store.connect() as db:
        db.execute("UPDATE attempts SET error_kind='output_limit',usage=? WHERE id=?", (json.dumps({'output_tokens':8192}),node['task']['last_attempt']['id']))
    preview=client.get('/api/execution/preview',params={'task_id':parent['id']}).json()
    assert not preview['eligible']
    assert any('不能原样付费重跑' in b for b in preview['blockers'])
    adapter.max_output_tokens=16384
    updated=client.get('/api/execution/preview',params={'task_id':parent['id']}).json()
    assert updated['eligible'],updated
    assert updated['revision']!=preview['revision']


def test_phase_b_revision_rebuilds_poisoned_context_and_binds_goal_to_approval(setup):
    client, app, parent, endpoint, shared, root = setup
    plan = configured(setup)
    shared['escape'] = True
    assert client.post(endpoint + '/start', json={'revision': plan['revision'], 'approve_plan': True}).status_code == 200
    plan = wait(client, endpoint, lambda p: p['state'] == 'paused' and all(n['task']['status'] != 'running' for n in p['nodes']))
    goal = client.post('/api/tasks', json={'title': 'LOCAL REAL GOAL', 'description': 'LOCAL REAL REQUIREMENTS', 'agent_id': 'auto'}).json()
    poison = '你是项目规划者。本任务只产出任务规格，不写实现代码。外层文件方案中只返回一个文件 nexus-plan/tasks.json'
    with app.state.store.connect() as db:
        db.execute('INSERT INTO planning_runs VALUES(?,?,?,NULL,?)', (parent['id'], goal['id'], 'fixture', now()))
        db.execute('UPDATE tasks SET description=? WHERE id=?', (poison, parent['id']))
        for node in plan['nodes']:
            db.execute('UPDATE tasks SET description=? WHERE id=?', (poison, node['task_id']))
        old = [dict(db.execute('SELECT * FROM tasks WHERE id=?', (n['task_id'],)).fetchone()) for n in plan['nodes'][:2]]
    preview = client.get('/api/execution/preview', params={'task_id': parent['id']}).json()
    assert preview['eligible'], preview
    assert all('LOCAL REAL REQUIREMENTS' in s['execution_context'] and poison not in s['execution_context'] for s in preview['scopes'])
    body = {'task_id': parent['id'], 'revision': preview['revision'], 'approve_usage': True, 'approve_revisions': True}
    with app.state.store.connect() as db:
        db.execute('UPDATE tasks SET description=? WHERE id=?', ('LOCAL CHANGED REQUIREMENTS', goal['id']))
    calls = len(shared['contexts'])
    assert client.post('/api/execution/run', json=body).status_code == 409
    assert len(shared['contexts']) == calls
    body['revision'] = client.get('/api/execution/preview', params={'task_id': parent['id']}).json()['revision']
    shared['escape'] = False
    response = client.post('/api/execution/run', json=body)
    assert response.status_code == 200, response.text
    plan = wait(client, endpoint, lambda p: all(n['task']['status'] == 'done' for n in p['nodes'][:2]))
    for node in plan['nodes']:
        assert 'LOCAL CHANGED REQUIREMENTS' in node['task']['description']
        assert poison not in node['task']['description']
    with app.state.store.connect() as db:
        for previous in old:
            assert dict(db.execute('SELECT * FROM tasks WHERE id=?', (previous['id'],)).fetchone()) == previous
    assert all('nexus-plan/tasks.json' not in ctx.objective for ctx in shared['contexts'][calls:])
    assert len(shared['contexts']) == calls + 2
    client.post(endpoint + '/pause')


def test_phase_b_scope_case_is_canonical_and_escape_remains_rejected(setup, monkeypatch):
    from nexus import file_changes
    client, app, parent, endpoint, shared, root = setup
    node = orchestration.Node(key='front', title='FIXTURE', objective='FIXTURE', acceptance='FIXTURE',
                              agent_id='mimo', directory='Delivery/Front', deliverables=['delivery/front/Main.ts']).model_dump()
    assert node['deliverables'] == ['Delivery/Front/Main.ts']
    # Simulate an older persisted plan that passed the previous casefold gate.
    node['deliverables'] = ['delivery/front/Main.ts']
    collab = app.state.collaborator
    monkeypatch.setattr(collab, 'membership', lambda tid: (parent['id'], 'front'))
    monkeypatch.setattr(collab, 'load', lambda tid: {'nodes': [node]})
    files = [{'path': 'DELIVERY/FRONT/main.TS', 'content': 'LOCAL FIXTURE'}]
    collab.validate_output('fixture', files)
    assert files[0]['path'] == 'Delivery/Front/Main.ts'
    file_changes.apply_files(root, files, {})
    assert (root / 'Delivery/Front/Main.ts').read_text() == 'LOCAL FIXTURE'
    for path in ['Delivery/Frontend/Main.ts', 'Delivery/Other/Main.ts', '../Front/Main.ts',
                 'C:/Delivery/Front/Main.ts', '//host/share/Main.ts', 'Delivery/Front/../Main.ts',
                 'Delivery\\Front\\Main.ts']:
        with pytest.raises(ValueError):
            collab.validate_output('fixture', [{'path': path, 'content': 'LOCAL FIXTURE'}])
    with pytest.raises(ValueError):
        collab.validate_output('fixture', [{'path': 'Delivery/Front/Extra.ts', 'content': 'LOCAL FIXTURE'}])
