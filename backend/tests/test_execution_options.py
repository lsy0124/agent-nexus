"""Settings-only acceptance. No credentials or external model calls."""
from fastapi.testclient import TestClient
from nexus.main import create_app
from nexus.orchestration import digest


def test_options_approval_persistence_binding_invalidation_and_no_requests(tmp_path):
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        options = client.get('/api/execution/options').json()['routes']
        current = next(r for r in options if r['agent_id'] == 'deepseek-gateway')
        old_binding = client.get('/api/agents').json()
        old_binding = next(r['binding'] for r in old_binding if r['id'] == 'deepseek-gateway')
        body = {**current, 'stream': True, 'max_output_tokens': 16384}
        url = '/api/execution/options/deepseek-gateway'
        assert client.put(url, json=body).status_code == 403
        assert client.get('/api/tasks').json() == []
        body['approve_increase'] = True
        response = client.put(url, json=body)
        assert response.status_code == 200, response.text
        assert response.json()['stream'] is True
        assert client.put(url, json=body).status_code == 409
        binding = next(r['binding'] for r in client.get('/api/agents').json() if r['id'] == 'deepseek-gateway')
        assert digest(binding) != digest(old_binding)
        assert client.get('/api/tasks').json() == []
    restored = create_app(tmp_path, enable_codex=False)
    assert restored.state.registry.adapters['deepseek-gateway'].max_output_tokens == 16384
    assert restored.state.registry.adapters['deepseek-gateway'].stream_enabled is True


def test_unknown_adapter_and_invalid_limits_rejected(tmp_path):
    app = create_app(tmp_path, enable_codex=False)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token'] = app.state.token
        assert client.put('/api/execution/options/codex', json={'revision':'x'}).status_code == 404
        assert client.put('/api/execution/options/deepseek-gateway', json={'revision':'x','max_output_tokens':999999}).status_code == 422


def test_glm_cannot_disable_thinking_and_deepseek_can_persist_mode(tmp_path):
    app=create_app(tmp_path,enable_codex=False)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token']=app.state.token
        routes={r['agent_id']:r for r in client.get('/api/execution/options').json()['routes']}
        assert client.put('/api/execution/options/zcode-gateway',json={**routes['zcode-gateway'],'reasoning':'none'}).status_code==422
        response=client.put('/api/execution/options/deepseek-gateway',json={**routes['deepseek-gateway'],'reasoning':'none'})
        assert response.status_code==200,response.text
        assert response.json()['reasoning']=='none'
    restored=create_app(tmp_path,enable_codex=False)
    assert restored.state.registry.adapters['deepseek-gateway'].reasoning_mode=='none'
