"""Task-sized SSE fixtures through the actual scheduler, database and Git writer."""
import json
import subprocess
import time
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from nexus import main
from nexus.adapters import make_adapters
from nexus.gateway_adapter import zcode_route,deepseek_route
from nexus.usage import STREAMING_USAGE_VERIFIED


@pytest.fixture(autouse=True)
def isolate_observations():
    before=set(STREAMING_USAGE_VERIFIED)
    yield
    STREAMING_USAGE_VERIFIED.clear();STREAMING_USAGE_VERIFIED.update(before)


@pytest.mark.parametrize('agent',['zcode-gateway','deepseek-gateway'])
def test_task_sized_stream_reaches_real_file_and_result(tmp_path,monkeypatch,agent):
    (tmp_path/'README.md').write_text('LOCAL ISOLATED SSE INTEGRATION FIXTURE')
    for args in (['init','-q'],['add','README.md'],['-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','local fixture']):
        subprocess.run(['git',*args],cwd=tmp_path,check=True,capture_output=True)
    head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=tmp_path)
    content='LOCAL TASK SIZED FILE\n'+'x'*9000
    plan=json.dumps({'summary':'LOCAL STREAM FIXTURE','files':[{'path':'delivered.txt','content':content}],'approval_requests':[]})
    packets=[{'id':'LOCAL_E2E_RESPONSE','model':'LOCAL_MODEL','object':'chat.completion.chunk','created':1234567890,'choices':[{'delta':{'content':char},'finish_reason':None}]} for char in plan]
    packets += [{'id':'LOCAL_E2E_RESPONSE','choices':[{'delta':{},'finish_reason':'stop'}]},{'choices':[],'usage':{'prompt_tokens':30,'completion_tokens':2000,'total_tokens':2030}}]
    body=''.join('data: '+json.dumps(p)+'\n\n' for p in packets)+'data: [DONE]\n\n'
    assert len(body.encode())>1_200_000
    calls=[]
    def handler(request):
        calls.append(request);return httpx.Response(200,headers={'content-type':'text/event-stream'},text=body)
    adapters=make_adapters(tmp_path,False)
    factory=zcode_route if agent=='zcode-gateway' else deepseek_route
    adapter=factory(tmp_path,True,http_transport=httpx.MockTransport(handler),api_key='sk-LOCAL-SSE-E2E-FIXTURE-ONLY',approved=True)
    adapter.stream_enabled=True
    adapters[agent]=adapter
    monkeypatch.setattr(main,'make_adapters',lambda *a:adapters)
    app=main.create_app(tmp_path,enable_codex=False)
    with TestClient(app) as client:
        client.headers['X-Nexus-Token']=app.state.token
        task=client.post('/api/tasks',json={'title':'LOCAL TASK SIZED SSE FIXTURE','agent_id':agent}).json()
        response=client.post('/api/tasks/'+task['id']+'/run',json={'cost_approved':True})
        assert response.status_code==200 and response.json()['accepted'],response.text
        for _ in range(250):
            actual=next(t for t in client.get('/api/tasks').json() if t['id']==task['id'])
            if actual['status']!='running':break
            time.sleep(.05)
        assert actual['status']=='done',actual
        assert (Path(actual['worktree'])/'delivered.txt').read_text(encoding='utf-8')==content
        assert actual['last_attempt']['usage']['total_tokens']==2030
        assert actual['last_attempt']['thread_id']=='LOCAL_E2E_RESPONSE'
        assert actual['review_status']=='pending'
        assert len(calls)==1
    assert not (tmp_path/'delivered.txt').exists()
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=tmp_path)==head
