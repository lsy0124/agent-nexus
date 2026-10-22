"""Reliability regressions. All model responses here are labelled local fixtures."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import httpx

from fastapi.testclient import TestClient
import pytest

from nexus import main as service, keyvault, budget, usage
from nexus.adapters import AgentEvent, make_adapters
from nexus.storage import Store, now


def repository(root):
    (root / 'README.md').write_text('LOCAL TEST FIXTURE', encoding='utf-8')
    for args in (['init', '-q'], ['add', '--', 'README.md'],
                 ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@localhost', 'commit', '-qm', 'fixture']):
        subprocess.run(['git', *args], cwd=root, check=True, capture_output=True)


class PlanFixture:
    id = 'deepseek-gateway'
    name = 'LOCAL FIXTURE'
    provider_id = 'gonode-gateway'
    model = 'deepseek-v4.1-flash'
    model_key = 'gonode-gateway/deepseek-v4.1-flash'
    transport = 'local-test-fixture'
    capabilities = ('file.generate', 'file.edit')
    verified_at = None
    version = None
    slow = False

    async def detect(self):
        return self.probe()

    def probe(self):
        return {'id': self.id, 'name': self.name, 'status': 'connected' if self.verified_at else 'disconnected',
                'ready': True, 'busy': False, 'verified_at': self.verified_at,
                'capabilities': list(self.capabilities), 'detail': 'LOCAL FIXTURE'}

    async def execute(self, context):
        if self.slow:
            await asyncio.sleep(60)
        objective = context.objective
        nonce = objective.split('summary exactly ')[1].split(',')[0] if objective.startswith('Connection test:') else None
        plan = {'summary': nonce or 'LOCAL FIXTURE', 'files': [] if nonce else [{'path': 'hello.txt', 'content': 'LOCAL FIXTURE'}], 'approval_requests': []}
        yield AgentEvent('completion', 'LOCAL FIXTURE', {'plan': plan, 'usage': {'input_tokens': 4, 'output_tokens': 2}, 'exit_code': 0})

    async def cancel(self, task_id):
        return None


@pytest.fixture
def fixture_app(tmp_path, monkeypatch):
    repository(tmp_path)
    instance = PlanFixture()
    adapters = make_adapters(tmp_path, False)
    adapters[instance.id] = instance
    monkeypatch.setattr(service, 'make_adapters', lambda *a: adapters)
    with TestClient(service.create_app(tmp_path, enable_codex=False)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        yield client, instance, tmp_path


def wait(client, task_id):
    for _ in range(100):
        task = next(t for t in client.get('/api/tasks').json() if t['id'] == task_id)
        if task['status'] not in ('running', 'backlog'):
            return task
        time.sleep(.02)
    raise AssertionError('Fixture task did not finish')


def test_nonce_is_not_development_execution_proof(fixture_app):
    client, adapter, root = fixture_app
    task = client.post('/api/agents/deepseek-gateway/test').json()['task']
    assert wait(client, task['id'])['status'] == 'done'
    state = next(a for a in client.get('/api/agents').json() if a['id'] == adapter.id)
    assert state['model_api_callable'] is True
    assert state['task_execution_capable'] is False
    task = client.post('/api/tasks', json={'title': 'LOCAL FIXTURE', 'agent_id': adapter.id}).json()
    refused = client.post(f"/api/tasks/{task['id']}/run").json()
    assert refused['accepted'] is False and '批准' in refused['reason']
    assert not client.get(f"/api/tasks/{task['id']}/attempts").json()
    accepted = client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()
    assert accepted['accepted'] is True
    assert wait(client, task['id'])['status'] == 'done'
    state = next(a for a in client.get('/api/agents').json() if a['id'] == adapter.id)
    assert state['task_execution_capable'] is True
    assert state['execution_verification']['evidence']['artifacts']


def test_failed_gateway_plan_keeps_provider_usage_and_does_not_reuse_it(tmp_path, monkeypatch):
    from nexus.gateway_adapter import deepseek_route
    repository(tmp_path)
    calls = []
    def response(request):
        calls.append(1)
        return httpx.Response(200, json={'id': 'LOCAL_USAGE_FIXTURE', 'model': 'deepseek-v4.1-flash',
            'usage': {'prompt_tokens': 9, 'completion_tokens': 17, 'total_tokens': 26},
            'choices': [{'message': {'content': None, 'reasoning_content': 'LOCAL REASONING FIXTURE'}, 'finish_reason': 'length'}]})
    adapter = deepseek_route(tmp_path, http_transport=httpx.MockTransport(response), api_key='LOCAL_FIXTURE_SECRET', approved=True)
    adapters = make_adapters(tmp_path, False)
    adapters[adapter.id] = adapter
    monkeypatch.setattr(service, 'make_adapters', lambda *a: adapters)
    with TestClient(service.create_app(tmp_path, enable_codex=False)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        task = client.post('/api/tasks', json={'title': 'LOCAL FAILED USAGE FIXTURE', 'agent_id': adapter.id}).json()
        assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()['accepted']
        done = wait(client, task['id'])
        assert done['status'] == 'failed' and done['last_attempt']['error_kind'] == 'output_limit'
        assert done['last_attempt']['usage']['total_tokens'] == 26
        rows = client.get('/api/usage', params={'range': 'all'}).json()
        assert rows['totals']['total_tokens'] == 26
        assert rows['records'][0]['usage_source'] == 'provider-reported'
        assert done['last_attempt']['artifacts'] == [] and len(calls) == 1
        # A later local pre-submit failure must not reuse this billed response.
        monkeypatch.setattr(service.file_changes, 'context_files', lambda *a: (_ for _ in ()).throw(ValueError('LOCAL PRE-SUBMIT FIXTURE')))
        assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()['accepted']
        done = wait(client, task['id'])
        assert done['status'] == 'failed' and done['last_attempt']['usage'] == {}
        assert len(calls) == 1
        assert client.get('/api/usage', params={'range': 'all'}).json()['totals']['total_tokens'] == 26


def test_auto_task_can_choose_one_explicit_route_and_approve_once(fixture_app):
    client, adapter, root = fixture_app
    task = client.post('/api/tasks', json={'title': 'LOCAL AUTO CONFIRM FIXTURE', 'agent_id': 'auto', 'cost_approved': True}).json()
    endpoint = f"/api/tasks/{task['id']}/run"
    # Old automatic approval never permits a metered candidate or an unknown route.
    assert client.post(endpoint).json()['accepted'] is False
    assert client.post(endpoint, json={'agent_id': 'unknown', 'cost_approved': True}).status_code == 422
    assert client.post(endpoint, json={'agent_id': 'workbuddy'}).status_code == 422
    assert client.post(endpoint, json={'agent_id': adapter.id}).json()['accepted'] is False
    assert not client.get(f"/api/tasks/{task['id']}/attempts").json()
    # Selected route is persisted; a fresh explicit approval is still necessary.
    refused = client.post(endpoint, json={'agent_id': 'mimo', 'cost_approved': True})
    assert refused.status_code == 409
    result = client.post(endpoint, json={'cost_approved': True}).json()
    assert result['accepted'] is True
    done = wait(client, task['id'])
    assert done['status'] == 'done' and done['agent_id'] == adapter.id
    assert (Path(done['worktree']) / 'hello.txt').read_text('utf-8') == 'LOCAL FIXTURE'
    assert len(client.get(f"/api/tasks/{task['id']}/attempts").json()) == 1


def test_auto_confirm_can_execute_restored_unverified_route_directly(fixture_app):
    client, adapter, root = fixture_app
    task = client.post('/api/tasks', json={'title': 'LOCAL RESTORED ROUTE FIXTURE', 'agent_id': 'auto'}).json()
    assert adapter.verified_at is None
    result = client.post(f"/api/tasks/{task['id']}/run", json={'agent_id': adapter.id, 'cost_approved': True}).json()
    assert result['accepted'] is True
    done = wait(client, task['id'])
    assert done['status'] == 'done' and done['agent_id'] == adapter.id
    assert done['last_attempt']['artifacts']
    assert client.post(f"/api/tasks/{task['id']}/run", json={'agent_id': adapter.id, 'cost_approved': True}).status_code == 409


def test_inflight_request_reserves_call_limit_and_cancel_is_counted(fixture_app):
    client, adapter, root = fixture_app
    adapter.slow = True
    store = Store(root)
    guard = budget.BudgetGuard(store, usage.UsageCollector(store), budget.RateCard(store))
    guard.update_settings({'daily_calls': 1})
    first = client.post('/api/tasks', json={'title': 'LOCAL FIXTURE', 'agent_id': adapter.id, 'cost_approved': True}).json()
    assert client.post(f"/api/tasks/{first['id']}/run").json()['accepted'] is True
    gate = guard.evaluate(agent_id='mimo', provider_id='mimo-official', model_key=None, task={'id': 'other', 'cost_approved': True})
    assert any(d['code'] == 'call_limit_reached' for d in gate['denials'])
    client.post(f"/api/tasks/{first['id']}/cancel")
    assert wait(client, first['id'])['status'] == 'cancelled'
    assert usage.UsageCollector(store).totals('all')['requests'] == 1


def test_logical_usage_is_scoped_to_its_runtime_routes(fixture_app):
    client, adapter, root = fixture_app
    collector = usage.UsageCollector(Store(root))
    for agent, provider, count in [('deepseek-gateway', 'gonode-gateway', 11), ('zcode-gateway', 'hyai666-gateway', 29)]:
        collector.record(usage.UsageEvent(agent_id=agent, provider_id=provider, model_key=provider+'/fixture', attempt_id=agent,
                         usage=usage.parse_usage({'prompt_tokens': count, 'completion_tokens': 2})))
    deepseek = client.get('/api/usage/agents/deepseek').json()
    assert deepseek['totals']['total_tokens'] == 13
    assert {r['agent_id'] for r in deepseek['records']} == {'deepseek-gateway'}
    assert len(deepseek['by_model']) == 1
    assert deepseek['series'][0]['total_tokens'] == 13
    assert client.get('/api/usage/agents/zcode').json()['totals']['total_tokens'] == 31


def test_unknown_price_cannot_bypass_financial_limit(tmp_path):
    store = Store(tmp_path)
    guard = budget.BudgetGuard(store, usage.UsageCollector(store), budget.RateCard(store))
    guard.update_settings({'daily_budget': 10})
    result = guard.evaluate(agent_id='deepseek-gateway', provider_id='gonode-gateway', model_key='gonode-gateway/deepseek-v4.1-flash', task={'id': 'fixture', 'cost_approved': True})
    assert any(d['code'] == 'budget_unenforceable' for d in result['denials'])


def test_codex_has_an_independent_call_limit(tmp_path):
    store = Store(tmp_path)
    guard = budget.BudgetGuard(store, usage.UsageCollector(store), budget.RateCard(store))
    guard.update_settings({'codex_daily_calls': 0})
    assert any(d['code'] == 'codex_call_limit' for d in guard.evaluate(agent_id='codex', provider_id='codex-cli', model_key=None)['denials'])


def test_restored_configuration_can_attempt_explicit_task_without_fake_connection():
    from nexus.registry import AdapterRegistry
    class Restored(PlanFixture):
        supported_capabilities = ('file.generate',)
        def probe(self):
            return {**super().probe(), 'capabilities': []}
    adapter = Restored()
    registry = AdapterRegistry({adapter.id: adapter})
    assert registry.snapshot(adapter.id)['status'] == 'disconnected'
    assert registry.select(adapter.id, {'file.generate'}) is adapter
    assert registry.select('auto', {'file.generate'}) is None
    assert registry.select(adapter.id, {'shell.execute'}) is None


def test_provider_switch_clears_old_key_and_changes_real_endpoint(tmp_path, monkeypatch):
    from nexus import gateway_adapter
    monkeypatch.setattr(gateway_adapter, '_is_public_host', lambda *a, **k: True)
    adapters = make_adapters(tmp_path, False)
    adapter = adapters['deepseek-gateway']
    adapter.enabled = True
    adapter.configure(api_key='LOCAL-OLD-GATEWAY-FIXTURE', approved=True)
    monkeypatch.setattr(service, 'make_adapters', lambda *a: adapters)
    with TestClient(service.create_app(tmp_path, enable_codex=True)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        response = client.put('/api/agents/deepseek-gateway/binding', json={
            'provider_id': 'deepseek-official', 'acknowledge_unconfigured': True})
        assert response.status_code == 200
        assert adapter.provider_id == 'deepseek-official'
        assert adapter.endpoint.startswith('https://api.deepseek.com')
        assert adapter._api_key is None and adapter.ready is False
        assert Store(tmp_path).setting('binding', adapter.id)['provider_id'] == 'deepseek-official'


def test_token_ceiling_retains_usage_but_does_not_write(fixture_app):
    client, adapter, root = fixture_app
    store = Store(root)
    guard = budget.BudgetGuard(store, usage.UsageCollector(store), budget.RateCard(store))
    guard.update_settings({'per_task_tokens': 1})
    task = client.post('/api/tasks', json={'title': 'LOCAL FIXTURE', 'agent_id': adapter.id, 'cost_approved': True}).json()
    assert client.post(f"/api/tasks/{task['id']}/run").json()['accepted'] is True
    final = wait(client, task['id'])
    assert final['status'] == 'failed'
    assert not (Path(final['worktree']) / 'hello.txt').exists()
    records = usage.UsageCollector(store).rows('all', task_id=task['id'])
    assert records[0]['total_tokens'] == 6 and records[0]['error_kind'] == 'task_token_limit'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_opt_in_vault_restores_runtime_after_app_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('NEXUS_SECRET_DIR', raising=False)
    monkeypatch.delenv('NEXUS_KEY_VAULT', raising=False)
    def adapters(*a):
        result = make_adapters(tmp_path, False)
        result['mimo'].enabled = True
        return result
    monkeypatch.setattr(service, 'make_adapters', adapters)
    secret = 'LOCAL-SYNTHETIC-MIMO-NOT-A-CREDENTIAL'
    with TestClient(service.create_app(tmp_path, enable_codex=True)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        assert client.get('/api/settings/providers').json()['vault']['backend'] == 'memory'
        assert client.put('/api/settings/vault', json={'backend': 'dpapi'}).status_code == 403
        assert client.put('/api/settings/vault', json={'backend': 'dpapi', 'approve_persistence': True}).status_code == 200
        result = client.post('/api/agents/mimo/configure', json={'api_key': secret, 'model': 'mimo-v2.6-flash', 'approve_usage': True})
        assert result.status_code == 200
        fingerprint = next(p for p in client.get('/api/settings/providers').json()['providers'] if p['id'] == 'mimo-official')['credential']['fingerprint']
    with TestClient(service.create_app(tmp_path, enable_codex=True)) as client:
        state = next(a for a in client.get('/api/agents').json() if a['id'] == 'mimo')
        assert state['credential_configured'] is True and state['status'] == 'disconnected'
        assert client.get('/api/settings/providers').json()['vault']['backend'] == 'dpapi'
        credential = next(p for p in client.get('/api/settings/providers').json()['providers'] if p['id'] == 'mimo-official')['credential']
        assert credential['fingerprint'] == fingerprint
        assert secret not in client.get('/api/state').text


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_dpapi_survives_a_new_process_without_plaintext(tmp_path):
    directory = tmp_path / 'work' / 'secrets'
    vault = keyvault.SecretVault(tmp_path, backend='dpapi', directory=directory)
    vault.set('fixture', 'NEXUS-SYNTHETIC-KEY-NOT-A-CREDENTIAL')
    assert b'NEXUS-SYNTHETIC' not in (directory / 'fixture.dpapi').read_bytes()
    code = "from pathlib import Path; from nexus.keyvault import SecretVault; import sys; v=SecretVault(Path(sys.argv[1]),backend='dpapi',directory=Path(sys.argv[2])); assert v.get('fixture')=='NEXUS-SYNTHETIC-KEY-NOT-A-CREDENTIAL'; print('restored')"
    env = {**os.environ, 'PYTHONPATH': str(Path(service.__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path), str(directory)], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'restored'


@pytest.mark.parametrize('failure', ['permission', 'preflight', 'authentication', 'timeout', 'network'])
def test_phase_b_http_health_and_local_task_failure_are_separate(tmp_path, monkeypatch, failure):
    from nexus.gateway_adapter import deepseek_route
    repository(tmp_path)
    calls = []
    plan = {'summary': 'LOCAL FIXTURE', 'approval_requests': [],
            'files': [{'path': 'work/denied.txt' if failure == 'permission' else 'allowed.txt', 'content': 'LOCAL FIXTURE'}]}
    def response(request):
        calls.append(request.method)
        if failure == 'authentication':
            return httpx.Response(401, json={'error': 'LOCAL AUTH FIXTURE'})
        if failure == 'timeout':
            raise httpx.ReadTimeout('LOCAL TIMEOUT FIXTURE')
        if failure == 'network':
            raise httpx.RemoteProtocolError('LOCAL NETWORK FIXTURE')
        return httpx.Response(200, json={'id': 'LOCAL_FIXTURE', 'model': 'deepseek-v4.1-flash',
            'usage': {'prompt_tokens': 9, 'completion_tokens': 17, 'total_tokens': 26},
            'choices': [{'message': {'content': json.dumps(plan)}, 'finish_reason': 'stop'}]})
    adapter = deepseek_route(tmp_path, http_transport=httpx.MockTransport(response), api_key='LOCAL_FIXTURE_SECRET', approved=True)
    previous = adapter.verified_at = now()
    adapters = make_adapters(tmp_path, False); adapters[adapter.id] = adapter
    monkeypatch.setattr(service, 'make_adapters', lambda *a: adapters)
    with TestClient(service.create_app(tmp_path, enable_codex=False)) as client:
        client.headers['X-Nexus-Token'] = client.get('/api/session').json()['token']
        if failure == 'preflight':
            monkeypatch.setattr(service.file_changes, 'context_files', lambda *a: (_ for _ in ()).throw(ValueError('LOCAL PREFLIGHT PERMISSION FIXTURE')))
        task = client.post('/api/tasks', json={'title': 'LOCAL FAILURE FIXTURE', 'agent_id': adapter.id}).json()
        assert client.post(f"/api/tasks/{task['id']}/run", json={'cost_approved': True}).json()['accepted']
        done = wait(client, task['id'])
        assert done['status'] == 'failed' and done['last_attempt']['artifacts'] == []
        state = next(a for a in client.get('/api/agents').json() if a['id'] == adapter.id)
        if failure in ('permission', 'preflight'):
            assert state['status'] == 'connected' and state['last_error'] is None
            assert state['connection_state'] == 'ONLINE'
            assert done['execution_state'] == state['last_task_error']['state'] == 'BLOCKED_PERMISSION'
            assert not (Path(done['worktree']) / 'work/denied.txt').exists()
            if failure == 'preflight':
                assert adapter.verified_at == previous
                assert calls == []
            else:
                assert done['last_attempt']['usage']['total_tokens'] == 26
        else:
            assert state['status'] == 'disconnected' and state['connection_state'] == 'ERROR'
            assert state['status_reason'] == 'request_error'
            assert state['last_error_kind'] == done['last_attempt']['error_kind']
        assert len(calls) == (0 if failure == 'preflight' else 1)
        assert client.get('/api/health').json()['status'] == 'ok'
