"""Large and fragmented SSE regressions. In-memory bytes, no real requests."""
import asyncio
import json
import httpx
import pytest
from nexus.gateway_stream import chat_events
from nexus.bridge_protocol import BridgeError


def frame(event, newline='\n'):
    return ('data: ' + json.dumps(event,ensure_ascii=False) + newline*2).encode('utf-8')


def event(text,finish=None):
    return {'id':'LOCAL_STREAM','model':'LOCAL_MODEL','choices':[{'delta':{'content':text},'finish_reason':finish}]}


def run(body,**kwargs):
    async def collect():
        return [v async for v in chat_events(httpx.Response(200,content=body),**kwargs)]
    return asyncio.run(collect())


def test_sse_envelope_may_exceed_old_limit_while_file_text_stays_small():
    plan=json.dumps({'summary':'LOCAL LARGE RESPONSE FIXTURE','files':[{'path':'large.txt','content':'x'*9000}],'approval_requests':[]})
    body=b''.join(frame({**event(char),'created':1234567890,'object':'chat.completion.chunk','system_fingerprint':'LOCAL_REPEATED_HEADER_FIXTURE'}) for char in plan)
    body+=frame(event('', 'stop'))+frame({'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':3000,'total_tokens':3010}})+b'data: [DONE]\n\n'
    assert len(body)>1_200_000
    stats={};items=run(body,stats=stats)
    assert items[-1]['document']['choices'][0]['message']['content']==plan
    assert stats['wire_bytes']>1_200_000 and stats['content_bytes']==len(plan.encode())
    assert stats['content_bytes']<1_200_000
    assert items[-1]['document']['usage']['total_tokens']==3010


@pytest.mark.parametrize('kwargs,body,kind',[
    ({'wire_limit':100},frame(event('x'*200)),'stream_transport_limit'),
    ({'event_limit':100},b'data: '+b'x'*200,'stream_event_limit'),
    ({'event_limit':100},b'data: '+b'x'*70+b'\ndata: '+b'y'*70+b'\n\n','stream_event_limit'),
    ({'event_count_limit':2},b': ping\n\n'*3,'stream_event_limit'),
    ({'limit':3},frame(event('中文')),'response_content_limit'),
])
def test_independent_finite_limits_remain_enforced(kwargs,body,kind):
    with pytest.raises(BridgeError) as error:run(body,**kwargs)
    assert error.value.kind==kind


def test_utf8_split_crlf_multiline_data_comments_and_eof_done():
    # 16 KiB reader blocks split a Chinese character and a CRLF boundary.
    packet=event('中文文件')
    encoded=json.dumps(packet,ensure_ascii=False)
    split=encoded.index(', "choices"')
    body=b':'+b'p'*16380+b'\r\n\r\n'
    body+=b'data: '+encoded[:split+1].encode()+b'\r\ndata: '+encoded[split+1:].encode()+b'\r\n\r\n'
    body+=frame(event('', 'stop'),'\r\n')+b'data: [DONE]'
    result=run(body)
    assert result[-1]['document']['choices'][0]['message']['content']=='中文文件'


def test_intermediate_usage_is_not_treated_as_final_usage():
    body=frame({'choices':[],'usage':{'total_tokens':999999}})+frame(event('ok','stop'))+b'data: [DONE]\n\n'
    result=run(body)
    assert not any('usage' in item for item in result)
    assert 'usage' not in result[-1]['document']


def test_incomplete_stream_still_requires_done():
    with pytest.raises(BridgeError) as error:run(frame(event('valid but incomplete','stop')))
    assert error.value.kind=='protocol'


def test_discarded_reasoning_does_not_count_as_final_text():
    body=frame({'choices':[{'delta':{'reasoning_content':'r'*50000},'finish_reason':None}]})
    body+=frame(event('ok','stop'))+b'data: [DONE]\n\n'
    stats={};result=run(body,limit=10,stats=stats)
    assert stats['content_bytes']==2
    assert result[-1]['document']['choices'][0]['message']['content']=='ok'
