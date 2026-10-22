"""Persistent, bounded collaboration. Local drafts are not model-made plans.

Each approved node permits one attempt at one binding. Restart pauses queues;
review permits read-only dependency context, never an implicit Git merge.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator

from . import file_changes, worktrees
from .storage import now


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Node(BaseModel):
    key: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,29}$')
    title: str = Field(min_length=1, max_length=100)
    objective: str = Field(min_length=1, max_length=5000)
    acceptance: str = Field(min_length=1, max_length=2000)
    agent_id: str = Field(max_length=50)
    directory: str = Field(min_length=1, max_length=150)
    dependencies: list[str] = Field(default_factory=list, max_length=12)
    cost_approved: bool = False
    deliverables: list[str] = Field(default_factory=list, max_length=3)
    input_files: list[str] | None = Field(default=None, max_length=20)

    @model_validator(mode='after')
    def normalize(self):
        self.directory = self.directory.strip().rstrip('/')
        file_changes.relative_path(self.directory + '/scope.txt')
        if not self.title.strip() or not self.objective.strip() or not self.acceptance.strip():
            raise ValueError('子任务目标与验收标准不能为空')
        for name in self.deliverables:
            file_changes.relative_path(name)
            if not name.casefold().startswith(self.directory.casefold() + '/'):
                raise ValueError('交付文件必须位于子任务独占输出目录')
        # Use the approved directory's spelling even on case-sensitive volumes.
        self.deliverables = [self.directory + '/' + '/'.join(name.split('/')[len(self.directory.split('/')):]) for name in self.deliverables]
        if len({p.casefold() for p in self.deliverables}) != len(self.deliverables):
            raise ValueError('交付文件重复')
        for name in self.input_files or []:
            file_changes.relative_path(name)
        if self.input_files is not None and len({p.casefold() for p in self.input_files}) != len(self.input_files):
            raise ValueError('输入文件重复或大小写冲突')
        return self


class Plan(BaseModel):
    nodes: list[Node] = Field(min_length=2, max_length=12)
    max_parallel: int = Field(default=2, ge=2, le=3)

    @model_validator(mode='after')
    def graph(self):
        keys = [n.key for n in self.nodes]
        if len(set(keys)) != len(keys):
            raise ValueError('子任务 key 重复')
        graph = {n.key: n.dependencies for n in self.nodes}
        visiting, visited = set(), set()
        def visit(key):
            if key not in graph or key in visiting:
                raise ValueError('依赖不存在或形成循环')
            if key in visited:
                return
            visiting.add(key)
            for dep in graph[key]:
                visit(dep)
            visiting.remove(key)
            visited.add(key)
        for key in keys:
            visit(key)
        scopes = [n.directory.casefold() + '/' for n in self.nodes]
        if any(a.startswith(b) or b.startswith(a) for i, a in enumerate(scopes) for b in scopes[i+1:]):
            raise ValueError('子任务输出目录必须互不重叠，防止并行写入冲突')
        return self


class Start(BaseModel):
    revision: str
    approve_plan: bool = False
    approve_codex: bool = False


class Review(BaseModel):
    decision: Literal['approve', 'reject']
    manifest: str
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)


class Collaborator:
    def __init__(self, root, store, registry, binding, task, dispatch, cancel, mutation):
        self.root, self.store, self.registry, self.binding = root, store, registry, binding
        self.task, self.dispatch, self.cancel, self.mutation = task, dispatch, cancel, mutation
        self.lock = asyncio.Lock()
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS collaborations(parent_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL)')
            db.execute("UPDATE collaborations SET state='paused' WHERE state IN ('running','waiting_review')")

    def load(self, parent):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM collaborations WHERE parent_id=?', (parent,)).fetchone()
        if not row:
            raise HTTPException(404, '尚未创建协作方案')
        return {**json.loads(row['payload']), 'state': row['state'], 'parent_id': parent}

    def save(self, plan, message=None):
        payload = {k: v for k, v in plan.items() if k not in ('state', 'parent_id')}
        with self.store.connect() as db:
            db.execute('INSERT INTO collaborations VALUES(?,?,?,?) ON CONFLICT(parent_id) DO UPDATE SET state=excluded.state,payload=excluded.payload,updated_at=excluded.updated_at',
                       (plan['parent_id'], plan['state'], json.dumps(payload, ensure_ascii=False), now()))
            if message:
                self.store.log(db, plan['parent_id'], message, source='orchestrator')
            self.store.event(db, 'collaboration.updated', {'task_id': plan['parent_id'], 'state': plan['state']})

    def membership_map(self):
        """All membership facts in one pass over stored plans.

        The console asks for every task at once, so a per-task query would open
        one connection and re-parse every plan for each row. Callers that need a
        single answer can still use ``membership``.
        """
        members = {}
        with self.store.connect() as db:
            rows = db.execute('SELECT parent_id,payload FROM collaborations').fetchall()
        for row in rows:
            members.setdefault(row['parent_id'], (row['parent_id'], None))
            for node in json.loads(row['payload']).get('nodes', []):
                for task_id in (node.get('task_id'), *node.get('history', [])):
                    if task_id:
                        members.setdefault(task_id, (row['parent_id'], node.get('key')))
        return members

    def membership(self, task_id):
        return self.membership_map().get(task_id)

    def public(self, parent):
        plan = self.load(parent)
        for node in plan['nodes']:
            task = self.task(node['task_id']) if node.get('task_id') else None
            node['task'] = task
            node['manifest'] = digest((task.get('last_attempt') or {}).get('artifacts', [])) if task else None
        return plan

    def draft(self, parent):
        owner = self.task(parent)
        if owner['kind'] != 'agent' or owner['last_attempt'] or owner['status'] == 'running' or self.membership(parent):
            raise HTTPException(409, '只能拆分尚未执行、尚未加入协作的 Agent 总任务')
        candidates = []
        logical = set()
        for name in ('deepseek-gateway', 'zcode-gateway', 'mimo', 'gemini', 'zcode', 'dsh'):
            info = self.registry.snapshot(name)
            if info.get('ready') and 'file.generate' in info.get('supported_capabilities', []) and name not in logical:
                candidates.append(name)
                logical.add(name)
        roles = [('frontend', '前端与交互', '实现面向用户的页面和交互，说明与后端的数据契约。'),
                 ('backend', '后端与数据', '实现业务接口和数据处理，说明与前端的数据契约。'),
                 ('integration', '集成与验收材料', '基于已审核的前端和后端产物，提供集成说明和测试文件；不能宣称未运行的测试通过。')]
        nodes = [Node(key=key, title=title, objective=objective,
                      acceptance='交付与总目标对应的实际文件；列出验证方法、接口约定及未完成项。',
                      agent_id=candidates[i % len(candidates)] if candidates else '',
                      directory='nexus-delivery/' + key, dependencies=['frontend', 'backend'] if i == 2 else []).model_dump()
                 for i, (key, title, objective) in enumerate(roles)]
        plan = {'parent_id': parent, 'state': 'draft', 'nodes': nodes, 'max_parallel': 2,
                'planner': 'local-template', 'planner_note': '本地角色模板，不是 AI 理解后的拆分；请根据项目编辑目标、验收与目录。', 'revision': digest(nodes)}
        self.save(plan, '已生成本地协作拆分草案；未调用模型、未创建执行子任务。')
        return self.public(parent)

    def configure(self, parent, data):
        plan = self.load(parent)
        if plan['state'] != 'draft':
            raise HTTPException(409, '启动后方案锁定；不能变更已批准的执行范围')
        nodes = [n.model_dump() for n in data.nodes]
        if plan.get('planner', '').startswith('model-spec'):
            from .planning import parse_spec
            document = data.model_dump()
            for node in document['nodes']:
                node['cost_approved'] = False  # User approvals stay separate from specification validation.
            try:
                parse_spec(json.dumps(document, ensure_ascii=False), self.task(parent), self.registry)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        for node in nodes:
            if node['agent_id'] not in self.registry.adapters or node['agent_id'] == 'workbuddy':
                raise HTTPException(422, '请选择注册的自动执行路径')
        plan.update(nodes=nodes, max_parallel=data.max_parallel, revision=digest(data.model_dump()))
        self.save(plan, '协作草案已更新；尚未批准执行。')
        return self.public(parent)

    async def start(self, parent, data):
        async with self.lock:
            plan = self.load(parent)
            if not data.approve_plan or data.revision != plan['revision']:
                raise HTTPException(409, '请批准当前版本的完整方案')
            if plan['state'] not in ('draft', 'paused', 'waiting_review'):
                raise HTTPException(409, '当前方案不能启动')
            Plan.model_validate(plan)
            if any(n.get('task_id') and self.task(n['task_id'])['status'] in ('failed', 'cancelled', 'blocked') for n in plan['nodes']):
                raise HTTPException(409, '存在失败或受阻节点，请使用任务工作台“一键执行”确认修订；普通继续不会重放旧请求')
            owner = self.task(parent)
            for dependency in owner['dependencies']:
                prerequisite = self.task(dependency)
                artifacts = (prerequisite['last_attempt'] or {}).get('artifacts', [])
                if prerequisite['status'] != 'done' or (artifacts and prerequisite.get('review_status') != 'merged'):
                    raise HTTPException(409, '总任务的外部依赖尚未完成并通过 Git 审核，协作不能绕过前置条件')
            routes = set()
            for node in plan['nodes']:
                if node.get('task_id') and self.task(node['task_id'])['last_attempt']:
                    continue
                info = self.registry.snapshot(node['agent_id']) if node['agent_id'] in self.registry.adapters else {}
                if not info.get('ready') or 'file.generate' not in info.get('supported_capabilities', []):
                    raise HTTPException(409, f"{node['title']} 的执行路径未配置就绪或缺少文件能力")
                selected_inputs = node.get('input_files')
                if selected_inputs is None:
                    selected_inputs = owner['input_files']
                if not set(selected_inputs).issubset(owner['input_files']):
                    raise HTTPException(422, '子任务只能选择总任务已批准的输入文件')
                required = set(owner['requires_skills']) | ({'file.edit'} if selected_inputs else set())
                if not required.issubset(info.get('supported_capabilities', [])):
                    raise HTTPException(409, f"{node['title']} 不具备总任务声明的所需能力，拆分不能绕过能力要求")
                if node['agent_id'] == 'codex' and not data.approve_codex:
                    raise HTTPException(403, '协作使用 Codex 需单独批准；仍受独立预算保护')
                if self.binding(node['agent_id'])['provider']['billing'] == 'metered-api' and not node['cost_approved']:
                    raise HTTPException(403, f"请批准子任务 {node['title']} 的单次费用")
                binding = self.binding(node['agent_id'])
                signature = digest(binding)
                if node.get('binding') and node['binding'] != signature:
                    raise HTTPException(409, 'Provider 或模型已变化；旧方案批准不能复用')
                node['binding'], node['binding_detail'] = signature, binding
                routes.add(node['agent_id'])
            # Distinct routes must also be distinct logical Agents.
            from . import catalog
            owners = {catalog.logical_agent(n['agent_id']) for n in plan['nodes']}
            if len(owners) < 2:
                raise HTTPException(409, '多 Agent 协作至少需要两个不同的逻辑 Agent')
            if plan['state'] == 'draft':
                async with self.mutation:
                    owner = self.task(parent)
                    if owner['last_attempt'] or owner['status'] in ('running', 'done'):
                        raise HTTPException(409, '总任务已开始单独执行，请刷新')
                    ids = {n['key']: uuid.uuid4().hex[:12] for n in plan['nodes']}
                    # Never hand a planner instruction set to an executing child.
                    with self.store.connect() as db:
                        for node in plan['nodes']:
                            tid = ids[node['key']]
                            description = self.child_description(owner, node)
                            selected_inputs = node.get('input_files')
                            if selected_inputs is None:
                                selected_inputs = owner['input_files']
                            db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files,cost_approved) VALUES(?,?,?,?,?,'backlog',?,?,NULL,NULL,?,?,?,0)",
                                       (tid, node['title'], description, node['agent_id'], 'agent', owner['priority'],
                                        json.dumps(owner['requires_skills']), now(), now(), json.dumps(selected_inputs)))
                            node['task_id'], node['phase'] = tid, 'pending'
                            self.store.log(db, tid, f'协作子任务已创建；总任务 {parent}；输出目录 {node["directory"]}/', source='orchestrator')
                        # Persist every task before edges: a valid DAG may list
                        # dependent nodes before their prerequisites.
                        for node in plan['nodes']:
                            dependencies = {ids[dep] for dep in node['dependencies']} | set(owner['dependencies'])
                            for dep in dependencies:
                                db.execute('INSERT INTO dependencies VALUES(?,?)', (node['task_id'], dep))
                        db.execute("UPDATE tasks SET status='blocked',cost_approved=0,result=?,updated_at=? WHERE id=?", ('协作总任务：由子任务执行，禁止整包提交给单个 Agent。', now(), parent))
                        plan['state'] = 'running'
                        # Persist child ids in the same transaction as their creation.
                        payload = {k: v for k, v in plan.items() if k not in ('state', 'parent_id')}
                        db.execute('UPDATE collaborations SET state=?,payload=?,updated_at=? WHERE parent_id=?', ('running', json.dumps(payload, ensure_ascii=False), now(), parent))
            plan['state'] = 'running'
            self.save(plan, '协作方案已获明确批准；每个子任务最多启动一次，费用与预算按实际路径检查。')
            return self.public(parent)

    async def confirm_execution(self, parent, revision, bindings, repair, approve_codex, expected_contexts=None):
        """Explicit bounded revisions, never delete an old task or its evidence."""
        async with self.lock, self.mutation:
            plan = self.load(parent)
            if plan['revision'] != revision or plan['state'] not in ('draft', 'paused', 'waiting_review'):
                raise HTTPException(409, '协作方案已变化，请重新预览')
            if expected_contexts is not None:
                owner = self.task(parent)
                for node in plan['nodes']:
                    if node['key'] in expected_contexts and self.child_description(owner, node) != expected_contexts[node['key']]:
                        raise HTTPException(409, '执行上下文已变化，请重新预览并批准')
            replacements = {}
            for node in plan['nodes']:
                if not node.get('task_id'):
                    continue
                old = self.task(node['task_id'])
                if old.get('recovery_hold'):
                    raise HTTPException(409, '子任务需要恢复审核，不能通过修订绕过未知执行结果')
                needs_revision = old['status'] in ('failed', 'cancelled', 'blocked') or (node.get('phase') == 'submitted' and not old['last_attempt'])
                if not needs_revision:
                    continue
                if not repair:
                    raise HTTPException(403, '需要明确批准创建失败节点修订')
                if (old.get('last_attempt') or {}).get('artifacts') or (old.get('latest_handoff') or {}).get('artifacts') or node.get('review'):
                    raise HTTPException(409, '节点已有文件或审核记录，必须先人工处理，不能一键覆盖')
                if len(node.get('history', [])) >= 20:
                    raise HTTPException(409, '该节点修订过多，请重新拆小任务；不继续调用')
                replacements[old['id']] = uuid.uuid4().hex[:12]
            with self.store.connect() as db:
                for node in plan['nodes']:
                    if node.get('task_id') in replacements:
                        old = self.task(node['task_id'])
                        new_id = replacements[old['id']]
                        db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files,cost_approved) VALUES(?,?,?,?,?,'backlog',?,?,NULL,NULL,?,?,?,0)",
                                   (new_id, old['title'], self.child_description(self.task(parent), node), old['agent_id'], old['kind'], old['priority'], json.dumps(old['requires_skills']), now(), now(), json.dumps(old['input_files'])))
                        node['history'] = [*node.get('history', []), old['id']]
                        node.update(task_id=new_id, phase='pending')
                        node.pop('error', None)
                        self.store.log(db, new_id, f"用户确认修订：替代 {old['id']}；旧记录和 Worktree 保留，不复用旧文件目录", source='orchestrator')
                    elif node.get('task_id'):
                        pending = self.task(node['task_id'])
                        if not pending['last_attempt']:
                            # The approved preview includes this rebuilt context.
                            # Do not rewrite a task that has execution evidence.
                            db.execute('UPDATE tasks SET description=?,updated_at=? WHERE id=?',
                                       (self.child_description(self.task(parent), node), now(), pending['id']))
                    fresh = node.get('task_id') in replacements.values()
                    if fresh or not node.get('task_id') or not self.task(node['task_id'])['last_attempt']:
                        # Assign only the exact binding shown in the approved preview.
                        if node['key'] in bindings:
                            node['binding'] = bindings[node['key']]
                            node['cost_approved'] = True
                if replacements:
                    # All descendants are still unattempted when a prerequisite
                    # failed. Never rewrite completed/running dependency graphs.
                    for node in plan['nodes']:
                        if node.get('task_id') and not (node.get('task_id') in replacements.values()):
                            current = self.task(node['task_id'])
                            if current['last_attempt'] and any(d in replacements for d in current['dependencies']):
                                raise HTTPException(409, '已有后续执行记录，拒绝修改其依赖')
                    for node in plan['nodes']:
                        if not node.get('task_id'):
                            continue
                        current = db.execute('SELECT id FROM tasks WHERE id=?', (node['task_id'],)).fetchone()
                        if not current:
                            raise HTTPException(409, '修订任务丢失')
                        if node['task_id'] in replacements.values():
                            dependencies = [next(n['task_id'] for n in plan['nodes'] if n['key'] == key) for key in node['dependencies']]
                            dependencies += self.task(parent)['dependencies']
                            for dep in set(dependencies):
                                db.execute('INSERT INTO dependencies VALUES(?,?)', (node['task_id'], dep))
                        else:
                            for old_id, new_id in replacements.items():
                                db.execute('UPDATE dependencies SET depends_on=? WHERE task_id=? AND depends_on=?', (new_id, node['task_id'], old_id))
                    plan['revision'] = digest({'nodes': plan['nodes'], 'max_parallel': plan['max_parallel']})
                payload = {k: v for k, v in plan.items() if k not in ('state', 'parent_id')}
                db.execute('UPDATE collaborations SET payload=?,updated_at=? WHERE parent_id=?', (json.dumps(payload, ensure_ascii=False), now(), parent))
                self.store.log(db, parent, f'一键执行已明确批准；新建 {len(replacements)} 个修订节点，不自动重试', source='orchestrator')
            start_revision = plan['revision']
        return await self.start(parent, Start(revision=start_revision, approve_plan=True, approve_codex=approve_codex))

    def approved_files(self, plan, node):
        files = []
        for dep in node['dependencies']:
            source = next(n for n in plan['nodes'] if n['key'] == dep)
            task = self.task(source['task_id'])
            artifacts = (task['last_attempt'] or {}).get('artifacts', [])
            if not source.get('review') or source['review']['decision'] != 'approve' or source['review']['manifest'] != digest(artifacts):
                raise ValueError('前置产物尚未通过当前版本人工协作审核')
            target = worktrees.validate(self.root, task['id'], task['worktree'])
            for artifact in artifacts:
                path = file_changes.checked_path(target, artifact['path'])
                if not path.is_file() or path.stat().st_size > file_changes.MAX_BYTES:
                    raise ValueError('前置文件不可读取')
                data = path.read_bytes()
                if file_changes.sha(data) != artifact['sha256']:
                    raise ValueError('前置产物在审核后发生变化')
                files.append({'path': artifact['path'], 'content': data.decode('utf-8')})
        if sum(len(f['content'].encode()) for f in files) > file_changes.MAX_BYTES * 2:
            raise ValueError('前置上下文超出安全大小限制')
        return files

    def context(self, task_id):
        member = self.membership(task_id)
        if not member or not member[1]:
            return []
        plan = self.load(member[0])
        return self.approved_files(plan, next(n for n in plan['nodes'] if n['key'] == member[1]))

    def child_description(self, owner, node):
        description = (f"总目标：{owner['title']}\n{self.child_background(owner)}\n"
                       f"子任务：{node['objective']}\n验收标准：{node['acceptance']}\n"
                       f"只允许输出目录：{node['directory']}/；其他模块由协作任务负责。测试未经 Nexus 执行，不得宣称通过。")
        if node.get('deliverables'):
            description = '总目标仅为背景，不要求本子任务实现整个项目。\n' + description
            description += '\n本任务只交付这些文件：' + '、'.join(node['deliverables']) + '。不得扩展为整个模块；其他任务负责其余功能。'
        return description

    def child_background(self, owner):
        """The project background a child execution task may see.

        A collaboration can be created from a planning task, and a planning task's
        description is the planner's own instruction set ("你是项目规划者…外层文件方案中
        只返回一个文件 nexus-plan/tasks.json"). Injecting it verbatim into an executing
        child orders that child to emit a plan file, which then fails the approved
        directory guard below. Resolve the real goal instead of the planner prompt.
        """
        with self.store.connect() as db:
            run = db.execute('SELECT parent_id FROM planning_runs WHERE task_id=?', (owner['id'],)).fetchone()
            if not run:
                return owner['description'] or ''
            goal = db.execute('SELECT title, description FROM tasks WHERE id=?', (run['parent_id'],)).fetchone()
        if not goal:
            return owner['title'] or ''
        return '\n'.join(part for part in (goal['title'], goal['description']) if part)

    def validate_output(self, task_id, files):
        member = self.membership(task_id)
        if member and member[1]:
            node = next(n for n in self.load(member[0])['nodes'] if n['key'] == member[1])
            approved = node['directory'] + '/'
            outside = []
            normalized = []
            for f in files:
                name = file_changes.relative_path(f['path'])
                if not name.casefold().startswith(approved.casefold()):
                    outside.append(name)
                else:
                    # Canonicalize to the authorized spelling, rather than granting
                    # access to a sibling with different case on sensitive volumes.
                    normalized.append(node['directory'] + '/' + '/'.join(name.split('/')[len(node['directory'].split('/')):]))
            if outside:
                # Names only: a rejection must stay diagnosable without retaining
                # or logging any generated file content.
                detail = '、'.join(outside[:20])
                if len(outside) > 20:
                    detail += f'（共 {len(outside)} 个）'
                raise ValueError(f'协作输出越过已批准子任务目录：只允许 {approved}，实际提出 {detail}')
            if node.get('deliverables') and {f['path'].casefold() for f in files} != {p.casefold() for p in node['deliverables']}:
                raise ValueError('实际文件与已批准的逐文件交付范围不一致；未写入文件')
            # Persisted plans may predate Node.normalize's canonical spelling.
            declared = {p.casefold(): node['directory'] + '/' + '/'.join(p.split('/')[len(node['directory'].split('/')):])
                        for p in node.get('deliverables', [])}
            for f, name in zip(files, normalized):
                f['path'] = declared.get(name.casefold(), name)

    async def review(self, parent, key, data):
        async with self.lock:
            plan = self.load(parent)
            node = next((n for n in plan['nodes'] if n['key'] == key), None)
            if not node or not node.get('task_id'):
                raise HTTPException(404, '子任务不存在')
            task = self.task(node['task_id'])
            artifacts = (task['last_attempt'] or {}).get('artifacts', [])
            if task['status'] != 'done' or not artifacts or digest(artifacts) != data.manifest or node.get('review'):
                raise HTTPException(409, '只能审核当前已完成且尚未审核的文件产物')
            if not data.reviewer.strip() or not data.note.strip():
                raise HTTPException(422, '需要审核人与说明')
            node['review'] = data.model_dump() | {'at': now()}
            # Validate the actual bytes even for terminal nodes before approval.
            if data.decision == 'approve':
                self.approved_files(plan, {'dependencies': [key]})
            else:
                plan['state'] = 'paused'
            self.save(plan, f"人工协作审核：{node['title']} · {data.decision}；不代表 Git 合并批准。")
            return self.public(parent)

    async def pause(self, parent):
        async with self.lock:
            plan = self.load(parent)
            if plan['state'] not in ('running', 'waiting_review'):
                raise HTTPException(409, '方案当前没有运行')
            plan['state'] = 'paused'
            self.save(plan, '协作队列已暂停；正在尝试取消运行子任务，已落盘产物保留待审。')
            results = []
            for node in plan['nodes']:
                if node.get('task_id') and self.task(node['task_id'])['status'] == 'running':
                    try:
                        await self.cancel(node['task_id'])
                        results.append({'key': node['key'], 'cancelled': True})
                    except HTTPException as exc:
                        results.append({'key': node['key'], 'cancelled': False, 'detail': exc.detail})
            return {'plan': self.public(parent), 'cancellations': results}

    async def tick(self):
        async with self.lock:
            with self.store.connect() as db:
                parents = [r[0] for r in db.execute("SELECT parent_id FROM collaborations WHERE state IN ('running','waiting_review')")]
            for parent in parents:
                plan = self.load(parent)
                active = sum(self.task(n['task_id'])['status'] == 'running' for n in plan['nodes'])
                changed = False
                for node in plan['nodes']:
                    task = self.task(node['task_id'])
                    if task.get('recovery_hold') or task['status'] in ('failed', 'cancelled') or task.get('review_status') == 'rejected' or (node.get('review') or {}).get('decision') == 'reject':
                        plan['state'], changed = 'paused', True
                        break
                    if node['phase'] == 'pending' and task['last_attempt']:
                        node['phase'], changed = 'submitted', True
                    if node['phase'] != 'pending' or active >= plan['max_parallel']:
                        continue
                    deps = [next(n for n in plan['nodes'] if n['key'] == d) for d in node['dependencies']]
                    if any(not n.get('review') or n['review']['decision'] != 'approve' for n in deps):
                        continue
                    info = self.registry.snapshot(node['agent_id'])
                    occupied = any(t['status'] == 'running' and t['agent_id'] == node['agent_id'] for t in self.store.tasks())
                    if info.get('busy') or occupied:
                        continue
                    if not info.get('ready') or digest(self.binding(node['agent_id'])) != node['binding']:
                        plan['state'], node['error'], changed = 'paused', '配置未就绪或 Provider / 模型变化，旧批准不能复用', True
                        break
                    try:
                        self.approved_files(plan, node)
                        node['phase'] = 'submitted'
                        self.save(plan)  # Persist reservation before any real request.
                        result = await self.dispatch(node['task_id'], cost_approved=node['cost_approved'], workflow=True, expected_binding=node['binding'])
                        if not result['accepted']:
                            node['error'], plan['state'] = result['reason'], 'paused'
                            changed = True
                            break
                        active += 1
                    except Exception as exc:
                        node['error'], plan['state'] = type(exc).__name__ + ': 协作调度失败，请检查子任务日志', 'paused'
                        changed = True
                        break
                    changed = True
                tasks = [self.task(n['task_id']) for n in plan['nodes']]
                if plan['state'] != 'paused' and all(t['status'] == 'done' for t in tasks):
                    next_state = 'complete' if all(t.get('review_status') == 'merged' for t in tasks) else 'waiting_review'
                    changed = changed or plan['state'] != next_state
                    plan['state'] = next_state
                if changed:
                    self.save(plan, '协作状态：' + plan['state'])
                    with self.store.connect() as db:
                        db.execute('UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?',
                                   ('done' if plan['state'] == 'complete' else 'blocked',
                                    f"协作 {plan['state']}：{sum(t['status'] == 'done' for t in tasks)}/{len(tasks)} 个子任务产出；逐项审核文件与测试证据，最终合并需单独批准。", now(), parent))

    async def worker(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fail closed: unexpected scheduler failures never retry paid work.
                with self.store.connect() as db:
                    db.execute("UPDATE collaborations SET state='paused' WHERE state IN ('running','waiting_review')")
                    self.store.log(db, None, '协作调度异常，队列已暂停；未自动重试。', 'error', 'orchestrator')
            await asyncio.sleep(1)


def mount(app, collaborator):
    @app.get('/api/tasks/{parent}/collaboration')
    async def get(parent: str):
        return collaborator.public(parent)

    @app.post('/api/tasks/{parent}/collaboration/draft')
    async def draft(parent: str):
        async with collaborator.lock, collaborator.mutation:
            return collaborator.draft(parent)

    @app.put('/api/tasks/{parent}/collaboration')
    async def edit(parent: str, data: Plan):
        async with collaborator.lock:
            return collaborator.configure(parent, data)

    @app.post('/api/tasks/{parent}/collaboration/start')
    async def start(parent: str, data: Start):
        return await collaborator.start(parent, data)

    @app.post('/api/tasks/{parent}/collaboration/pause')
    async def pause(parent: str):
        return await collaborator.pause(parent)

    @app.post('/api/tasks/{parent}/collaboration/review/{key}')
    async def review(parent: str, key: str, data: Review):
        try:
            return await collaborator.review(parent, key, data)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
