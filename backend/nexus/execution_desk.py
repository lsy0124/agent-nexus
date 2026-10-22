"""Read-only execution diagnosis + explicitly approved one-click execution."""
import asyncio
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .orchestration import digest


def task_revision(task):
    return digest({key: task.get(key) for key in ('id', 'title', 'description', 'kind', 'agent_id', 'input_files', 'requires_skills', 'dependencies', 'worktree')})


class Execute(BaseModel):
    task_id: str = Field(max_length=64)
    agent_id: str | None = None
    revision: str = Field(max_length=64)
    approve_usage: bool = False
    approve_revisions: bool = False
    approve_codex: bool = False


class ExecutionDesk:
    def __init__(self, app, store, registry, collaborator, binding, task, dispatch, guard):
        self.store, self.registry, self.collaborator = store, registry, collaborator
        self.binding, self.task, self.dispatch, self.guard = binding, task, dispatch, guard
        self.lock = asyncio.Lock()
        app.state.execution_desk = self

        @app.get('/api/execution/preview')
        async def preview(task_id: str | None = None, agent_id: str | None = None):
            return self.preview(task_id, agent_id)

        @app.post('/api/execution/run')
        async def run(data: Execute):
            return await self.run(data)

    def items(self):
        result = []
        for t in self.store.tasks():
            member = self.collaborator.membership(t['id'])
            if member and member[1]:
                continue
            if t['status'] in ('done', 'running'):
                continue
            if (t['last_attempt'] or {}).get('purpose') == 'connection' or t['description'].startswith(('官方接口随机文本验证', '重新验证当前 Provider 配置', '用户批准本次最小真实请求')):
                continue
            result.append({'id': t['id'], 'title': t['title'], 'status': t['status'],
                           'mode': 'collaboration' if member else 'standalone'})
        return result

    def preview(self, task_id=None, agent_id=None):
        items = self.items()
        if task_id:
            member = self.collaborator.membership(task_id)
            task_id = member[0] if member else task_id
        if not task_id:
            task_id = next((i['id'] for i in items if i['mode'] == 'collaboration'), items[0]['id'] if items else None)
        if not task_id:
            return {'items': [], 'selected': None, 'scopes': [], 'blockers': ['没有待执行开发任务'], 'revision': digest([]), 'eligible': False}
        if task_id not in {i['id'] for i in items}:
            raise HTTPException(409, '任务已完成、正在运行或不是可派发开发任务，请刷新')
        owner = self.task(task_id)
        member = self.collaborator.membership(task_id)
        scopes, blockers, repairs = [], [], []
        plan = self.collaborator.public(task_id) if member else None
        if plan:
            for dep in owner['dependencies']:
                prerequisite = self.task(dep)
                if prerequisite['status'] != 'done' or ((prerequisite['last_attempt'] or {}).get('artifacts') and prerequisite.get('review_status') != 'merged'):
                    blockers.append('总任务外部依赖尚未完成并通过 Git 审核')
            if plan['state'] not in ('draft', 'paused', 'waiting_review'):
                blockers.append('协作队列已启动或完成，请先查看总览')
            for node in plan['nodes']:
                t = node.get('task')
                if t and t['status'] == 'done':
                    if (node.get('review') or {}).get('decision') == 'reject' or t.get('review_status') == 'rejected':
                        blockers.append(node['title'] + ' 已被人工拒绝，必须先修订方案')
                    continue
                repair = bool(t and (t['status'] in ('failed', 'cancelled', 'blocked') or (node.get('phase') == 'submitted' and not t['last_attempt'])))
                if repair:
                    repairs.append(node['key'])
                if t and ((t['last_attempt'] or {}).get('artifacts') or (t.get('latest_handoff') or {}).get('artifacts') or (repair and node.get('review'))):
                    blockers.append(node['title'] + ' 有待审核文件，不能自动重建')
                if len(node.get('history', [])) >= 20 and repair:
                    blockers.append(node['title'] + ' 修订次数过多，请人工拆小任务')
                scopes.append(self.scope(node['key'], node['title'], node['agent_id'], t or owner, repair, node['dependencies']))
                scopes[-1].update(objective=node['objective'], acceptance=node['acceptance'], directory=node['directory'])
                # Approval is bound to the exact execution context, including the
                # real goal behind a planner and any repaired child description.
                scopes[-1]['execution_context'] = self.collaborator.child_description(owner, node)
            # A pending downstream node cannot bypass prerequisite review.
            actionable = any(not s['dependencies'] or all((next(n for n in plan['nodes'] if n['key'] == dep).get('review') or {}).get('decision') == 'approve' for dep in s['dependencies']) for s in scopes)
            if not actionable:
                blockers.append('没有可启动节点：请先人工审核前置产物')
        elif owner['kind'] == 'diagnostic':
            scopes = [{'key': 'single', 'title': owner['title'], 'agent_id': 'local-check', 'provider': 'local', 'model': None, 'billing': 'local', 'binding': None, 'repair': False, 'dependencies': [], 'blockers': []}]
        else:
            if (owner['last_attempt'] or {}).get('artifacts'):
                blockers.append('已有待审核文件，先审核再创建修订任务')
            route = agent_id if owner['agent_id'] == 'auto' else owner['agent_id']
            if not route:
                blockers.append('请明确选择本次执行路径；不会自动改用付费模型')
            else:
                scopes.append(self.scope('single', owner['title'], route, owner, False, []))
                scopes[-1]['objective'] = owner['description']
                if scopes[-1].get('busy'):
                    blockers.append('执行器正在运行其他任务，请稍后提交')
            for dep in owner['dependencies']:
                if self.task(dep)['status'] != 'done':
                    blockers.append('前置任务尚未完成：' + self.task(dep)['title'])
        for scope in scopes:
            blockers.extend(scope['blockers'])
        if not scopes:
            blockers.append('没有尚待执行的子任务，请查看产物审核')
        selected = {'id': task_id, 'title': owner['title'], 'mode': 'collaboration' if plan else 'standalone',
                    'agent_id': owner['agent_id'], 'plan_revision': plan['revision'] if plan else None,
                    'state': plan['state'] if plan else owner['status'], 'repair_keys': repairs,
                    'task_revision': task_revision(owner),
                    'attempt_id': (owner['last_attempt'] or {}).get('id')}
        payload = {'selected': selected, 'scopes': scopes, 'blockers': list(dict.fromkeys(blockers))}
        return {**payload, 'items': items, 'revision': digest(payload), 'eligible': not blockers,
                'note': '一次选择一个总目标，协作按就绪依赖并行，不执行其他同名项目；启动不等于完成，合并仍需审核。'}

    def scope(self, key, title, agent_id, task, repair, dependencies):
        if agent_id not in self.registry.adapters or agent_id == 'workbuddy':
            return {'key': key, 'title': title, 'agent_id': agent_id, 'provider': None, 'model': None, 'billing': 'unknown', 'binding': None, 'repair': repair, 'dependencies': dependencies, 'blockers': [title + ' 的执行路径不可自动执行']}
        info, binding = self.registry.snapshot(agent_id), self.binding(agent_id)
        holds = []
        if task.get('recovery_hold'):
            holds.append(title + '：执行结果需要恢复审核，不会重复提交模型或文件写入')
        options = binding.get('execution')
        prior = task.get('last_attempt') or {}
        if repair and prior.get('error_kind') == 'output_limit' and options:
            prior_output = (prior.get('usage') or {}).get('output_tokens')
            previous_mode = (prior.get('request_profile') or {}).get('reasoning', 'provider')
            if prior_output is not None and options['max_output_tokens'] <= prior_output and options.get('reasoning', 'provider') == previous_mode:
                holds.append(title + '：上次已达到输出上限；请先调整思考模式、明确提高输出上限，或另建更小的协作方案，不能原样付费重跑')
        required = set(task['requires_skills']) | {'file.edit' if task['input_files'] else 'file.generate'}
        if not info.get('ready'):
            holds.append(title + '：尚未配置就绪')
        if not required.issubset(info.get('supported_capabilities', [])):
            holds.append(title + '：执行器缺少所需能力')
        gate = self.guard.evaluate(agent_id=agent_id, provider_id=binding['provider']['id'], model_key=getattr(self.registry.adapters[agent_id], 'model_key', None),
                                   task={**task, 'id': 'revision-preview' if repair else task['id'], 'cost_approved': True})
        holds.extend(d['detail'] for d in gate['denials'])
        return {'key': key, 'title': title, 'agent_id': agent_id, 'provider': binding['provider']['id'], 'model': binding['model_id'],
                'billing': binding['provider']['billing'], 'binding': digest(binding), 'repair': repair, 'dependencies': dependencies,
                'request_options': options, 'previous_error': prior.get('error_message'), 'blockers': holds, 'busy': bool(info.get('busy')), 'attempt_id': prior.get('id')}

    async def run(self, data):
        if not data.approve_usage:
            raise HTTPException(403, '请明确批准本次任务范围、订阅或 API 额度消耗')
        async with self.lock:
            preview = self.preview(data.task_id, data.agent_id)
            if preview['revision'] != data.revision or not preview['eligible']:
                raise HTTPException(409, '预览已变化或任务仍受阻，请重新核对：' + '；'.join(preview['blockers']))
            if any(s['agent_id'] == 'codex' for s in preview['scopes']) and not data.approve_codex:
                raise HTTPException(403, '使用 Codex 需单独确认，不自动切换')
            selected = preview['selected']
            if selected['mode'] == 'collaboration':
                plan = await self.collaborator.confirm_execution(selected['id'], selected['plan_revision'],
                    {s['key']: s['binding'] for s in preview['scopes']}, data.approve_revisions, data.approve_codex,
                    expected_contexts={s['key']: s['execution_context'] for s in preview['scopes']})
                return {'accepted': True, 'mode': 'collaboration', 'parent_id': selected['id'], 'plan': plan,
                        'reason': '协作队列已启动；就绪节点将实际执行，文件产物仍需审核'}
            scope = preview['scopes'][0]
            return await self.dispatch(selected['id'], cost_approved=True,
                                       selected_agent=data.agent_id if selected['agent_id'] == 'auto' else None,
                                       expected_binding=scope['binding'], expected_task_revision=selected['task_revision'])
