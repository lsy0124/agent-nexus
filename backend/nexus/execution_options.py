"""Non-secret gateway settings, with explicit approval for a larger allowance."""
from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool
from .orchestration import digest
from .storage import now
import json
from typing import Literal


class Options(BaseModel):
    stream: StrictBool = False
    max_output_tokens: int = Field(default=8192, ge=1024, le=32768, strict=True)
    reasoning: Literal['provider', 'none', 'low', 'high', 'max'] = 'provider'


class Update(Options):
    revision: str
    approve_increase: bool = False


def public(adapter):
    return {'stream': adapter.stream_enabled, 'max_output_tokens': adapter.max_output_tokens, 'reasoning': adapter.reasoning_mode}


def mount(app, store, registry, mutation):
    ids = tuple(key for key in ('zcode-gateway', 'deepseek-gateway') if hasattr(registry.adapters[key], 'stream_enabled'))
    for key in ids:
        adapter = registry.adapters[key]
        saved = store.setting('execution_options', key)
        if saved:
            try:
                options = Options.model_validate(saved)
                adapter.stream_enabled, adapter.max_output_tokens = options.stream, options.max_output_tokens
                adapter.reasoning_mode = options.reasoning
            except ValueError:
                with store.connect() as db:
                    store.log(db, None, f'{key} 执行设置无效，回到非流式 / 8192 默认值；未自动调用', 'warning', 'settings')

    def entry(key):
        value = public(registry.adapters[key])
        return {'agent_id': key, **value, 'revision': digest(value)}

    @app.get('/api/execution/options')
    async def get():
        return {'routes': [entry(key) for key in ids], 'note': '流式支持与输出上限均需由实际 Provider 验证；改设置不会调用模型。没有自动重试或非流式回退。'}

    @app.put('/api/execution/options/{key}')
    async def put(key: str, data: Update):
        if key not in ids:
            raise HTTPException(404, '该执行器不支持网关请求设置')
        async with mutation:
            adapter = registry.adapters[key]
            if registry.snapshot(key).get('busy') or any(t['status'] == 'running' and t['agent_id'] == key for t in store.tasks()):
                raise HTTPException(409, '执行器忙碌，不能修改请求设置')
            if data.revision != entry(key)['revision']:
                raise HTTPException(409, '请求设置已变化，请刷新')
            if data.max_output_tokens > adapter.max_output_tokens and not data.approve_increase:
                raise HTTPException(403, '增大输出上限可能提高费用，必须明确批准')
            if key == 'zcode-gateway' and data.reasoning == 'none':
                raise HTTPException(422, 'GLM-5.3 不支持关闭思考，请使用 low 或服务商默认值')
            options = Options(stream=data.stream, max_output_tokens=data.max_output_tokens, reasoning=data.reasoning)
            with store.connect() as db:
                db.execute("INSERT INTO settings(scope,key,value,updated_at) VALUES('execution_options',?,?,?) ON CONFLICT(scope,key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (key, json.dumps(options.model_dump()), now()))
                store.log(db, None, f'{key} 请求设置已保存：stream={options.stream}，最大输出={options.max_output_tokens}；未调用模型，旧执行批准作废', source='settings')
                store.event(db, 'agent.execution_options', {'agent_id': key})
            adapter.stream_enabled, adapter.max_output_tokens = options.stream, options.max_output_tokens
            adapter.reasoning_mode = options.reasoning
            adapter.verified_at = None
            return entry(key)
