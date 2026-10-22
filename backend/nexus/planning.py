"""Model planning uses ordinary, explicitly started tasks: one call, normal budgets.

Planning is inert JSON, never executable tools. Import does not approve execution.
Existing collaborations are preserved by creating a new goal on import.
"""
from __future__ import annotations

import json
import subprocess
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import catalog, file_changes, worktrees
from .orchestration import Node, Plan, digest
from .storage import now

PLAN_PATH = 'nexus-plan/tasks.json'
MAX_PLAN_BYTES = 48_000


class Prepare(BaseModel):
    agent_id: str = Field(min_length=1, max_length=50)


class Import(BaseModel):
    task_id: str
    manifest: str
    approve_import: bool = False
    revision_id: str | None = None


class Revision(BaseModel):
    task_id: str = Field(max_length=64)
    candidate_digest: str = Field(max_length=64)
    specification: str = Field(min_length=1, max_length=MAX_PLAN_BYTES)


def source_revision(owner):
    return digest({key: owner[key] for key in ('title', 'description', 'input_files', 'requires_skills', 'dependencies')})


def parse_spec(content, owner, registry):
    if len(content.encode('utf-8')) > MAX_PLAN_BYTES:
        raise ValueError('任务规格超过 48KB，请减少规划范围')
    try:
        document = json.loads(content)
    except RecursionError:
        raise ValueError('任务规格嵌套过深') from None
    if not isinstance(document, dict) or set(document) != {'nodes', 'max_parallel'}:
        raise ValueError('任务规格必须只包含 nodes 和 max_parallel')
    if not isinstance(document['nodes'], list):
        raise ValueError('nodes 必须是数组')
    allowed = set(Node.model_fields)
    for node in document['nodes']:
        if not isinstance(node, dict) or not set(node) <= allowed:
            raise ValueError('任务规格包含未知字段，拒绝自动指令或状态注入')
        if node.get('cost_approved') not in (None, False):
            raise ValueError('模型不能批准费用')
        if not node.get('deliverables') or node.get('input_files') is None:
            raise ValueError('每个子任务必须明确 1–3 个交付文件和输入文件清单')
    plan = Plan.model_validate(document)
    owners = set()
    for node in plan.nodes:
        if len(node.objective) > 1600:
            raise ValueError('子任务目标过长，请进一步拆分')
        if node.agent_id not in registry.adapters or node.agent_id == 'workbuddy':
            raise ValueError('任务规格引用了未知或人工执行路径')
        if not set(node.input_files).issubset(owner['input_files']):
            raise ValueError('任务规格读取范围超出总任务已选输入文件')
        owners.add(catalog.logical_agent(node.agent_id))
    if len(owners) < 2:
        raise ValueError('多 Agent 规格需要至少两个不同逻辑 Agent；不能把 Provider 当作不同 Agent')
    return plan


class PlanningDesk:
    def __init__(self, app, root, store, registry, collaborator, task, mutation):
        self.root, self.store, self.registry = root, store, registry
        self.collaborator, self.task = collaborator, task
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS planning_runs(task_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, source_revision TEXT NOT NULL, imported_parent TEXT, created_at TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS planning_revisions(id TEXT PRIMARY KEY, task_id TEXT NOT NULL, parent_id TEXT NOT NULL, candidate_digest TEXT NOT NULL, specification TEXT NOT NULL, manifest TEXT NOT NULL, imported_parent TEXT, created_at TEXT NOT NULL)')

        @app.get('/api/tasks/{parent}/planning')
        async def status(parent: str):
            task(parent)
            with store.connect() as db:
                rows = db.execute('SELECT * FROM planning_runs WHERE parent_id=? ORDER BY created_at DESC', (parent,)).fetchall()
            return {'runs': [self.public(dict(row)) for row in rows]}

        @app.post('/api/tasks/{parent}/planning')
        async def prepare(parent: str, data: Prepare):
            async with mutation:
                return self.prepare(parent, data.agent_id)

        @app.post('/api/tasks/{parent}/planning/import')
        async def import_spec(parent: str, data: Import):
            async with collaborator.lock:
                async with mutation:
                    return self.import_spec(parent, data)

        @app.post('/api/tasks/{parent}/planning/revise')
        async def revise(parent: str, data: Revision):
            async with mutation:
                return self.revise(parent, data)

    def is_planning(self, task_id):
        with self.store.connect() as db:
            return db.execute('SELECT 1 FROM planning_runs WHERE task_id=?', (task_id,)).fetchone() is not None

    def request_profile(self, task_id, agent_id, default):
        if agent_id == 'mimo' and self.is_planning(task_id):
            return {'stream': True, 'max_output_tokens': 8192, 'reasoning': 'none', 'purpose': 'planning'}
        return default

    def public(self, row):
        task = self.task(row['task_id'])
        artifacts = (task.get('last_attempt') or {}).get('artifacts', [])
        specification, error = None, None
        if task['status'] == 'done':
            try:
                specification = self.verified_spec(task, self.task(row['parent_id'])).model_dump()
            except (ValueError, OSError):
                error = '规格文件已变化或不再有效，不能导入'
        candidate = self.candidate(task)
        with self.store.connect() as db:
            revisions = [dict(r) for r in db.execute('SELECT * FROM planning_revisions WHERE task_id=? ORDER BY created_at DESC', (task['id'],)).fetchall()]
        for revision in revisions:
            revision['specification'] = json.loads(revision['specification'])
        return {**row, 'task': task, 'manifest': digest(artifacts),
                'specification': specification, 'error': error,
                'candidate': candidate, 'candidate_digest': digest(candidate) if candidate else None,
                'revisions': revisions,
                'source_current': source_revision(self.task(row['parent_id'])) == row['source_revision']}

    def candidate(self, task):
        """Recover only the inert specification from an actual completion record.

        A failed writer/validator is never promoted to a successful model task.
        """
        raw = (task.get('last_attempt') or {}).get('result')
        if not isinstance(raw, str) or len(raw.encode('utf-8')) > 600_000:
            return None
        try:
            files = json.loads(raw).get('files')
            if not isinstance(files, list) or len(files) != 1 or files[0].get('path') != PLAN_PATH:
                return None
            content = files[0].get('content')
            if isinstance(content, str) and len(content.encode('utf-8')) <= MAX_PLAN_BYTES:
                return content
        except (ValueError, AttributeError, RecursionError):
            pass
        return None

    def revise(self, parent, data):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM planning_runs WHERE task_id=? AND parent_id=?', (data.task_id, parent)).fetchone()
        if not row or row['imported_parent']:
            raise HTTPException(409, '规划不存在或已导入')
        task, owner = self.task(data.task_id), self.task(parent)
        candidate = self.candidate(task)
        if task['status'] not in ('done', 'failed') or not candidate or digest(candidate) != data.candidate_digest:
            raise HTTPException(409, '没有可修订的完整模型规格，或原始候选已变化')
        if source_revision(owner) != row['source_revision']:
            raise HTTPException(409, '总目标已变化，请重新规划')
        try:
            spec = parse_spec(data.specification, owner, self.registry).model_dump()
        except ValueError as exc:
            raise HTTPException(422, '本地规格修订校验失败：' + str(exc)) from None
        rid, stamp, manifest = uuid.uuid4().hex[:12], now(), digest(spec)
        with self.store.connect() as db:
            db.execute('INSERT INTO planning_revisions VALUES(?,?,?,?,?,?,NULL,?)',
                       (rid, task['id'], parent, data.candidate_digest, json.dumps(spec, ensure_ascii=False), manifest, stamp))
            self.store.log(db, task['id'], f'本地规格修订 {rid} 已通过结构校验，等待人工确认导入；原请求状态保持不变，未调用模型、未批准执行。', source='planner')
            self.store.event(db, 'planning.revised', {'task_id': task['id'], 'revision_id': rid})
        return {'id': rid, 'manifest': manifest, 'specification': spec, 'imported_parent': None}

    def verified_spec(self, task, owner):
        artifacts = (task.get('last_attempt') or {}).get('artifacts', [])
        if len(artifacts) != 1 or artifacts[0]['path'] != PLAN_PATH:
            raise ValueError('规划必须交付唯一的任务规格文件')
        tree = worktrees.validate(self.root, task['id'], task['worktree'])
        path = file_changes.checked_path(tree, PLAN_PATH)
        if path.stat().st_size > MAX_PLAN_BYTES:
            raise ValueError('任务规格过大')
        content = path.read_bytes()
        if file_changes.sha(content) != artifacts[0]['sha256']:
            raise ValueError('任务规格内容已变化')
        return parse_spec(content.decode('utf-8'), owner, self.registry)

    def prepare(self, parent, agent_id):
        owner = self.task(parent)
        member = self.collaborator.membership(parent)
        if owner['kind'] != 'agent' or owner['status'] == 'running' or (member and member[1]):
            raise HTTPException(409, '请从未运行的协作总目标创建规划')
        if member and self.collaborator.load(parent)['state'] in ('running', 'waiting_review'):
            raise HTTPException(409, '请先暂停现有协作队列，规划不会自动停止执行')
        info = self.registry.snapshot(agent_id) if agent_id in self.registry.adapters else {}
        if not info.get('ready') or 'file.generate' not in info.get('supported_capabilities', []):
            raise HTTPException(409, '规划执行路径未配置就绪或没有文件生成能力')
        candidates = []
        for key in self.registry.adapters:
            snapshot = self.registry.snapshot(key)
            if key != 'workbuddy' and snapshot.get('ready') and 'file.generate' in snapshot.get('supported_capabilities', []):
                candidates.append({'id': key, 'logical_agent': catalog.logical_agent(key),
                                   'model': snapshot.get('model'), 'verified': snapshot.get('status') == 'connected'})
        if len({item['logical_agent'] for item in candidates}) < 2:
            raise HTTPException(409, '至少需要两个配置就绪的逻辑 Agent；配置就绪不代表已验证执行成功')
        # Tracked names only; never traverse caches, credentials or private client data.
        result = subprocess.run(['git', 'ls-files', '-z'], cwd=self.root, capture_output=True, check=True)
        inventory = []
        for name in result.stdout.decode('utf-8').split('\0'):
            try:
                file_changes.relative_path(name)
            except ValueError:
                continue
            if name:
                inventory.append(name)
            if len(inventory) >= 160:
                break
        context = {'goal': owner['title'], 'requirements': owner['description'],
                   'approved_input_files': owner['input_files'], 'tracked_file_names': inventory,
                   'execution_routes': candidates}
        instructions = (
            '你是项目规划者。本任务只产出任务规格，不写实现代码。结合总目标、提供的输入内容和仓库文件名，'
            '拆成 2–12 个可独立交付的小任务，不要机械地拆成整个前端、整个后端。'
            '每个任务最多 3 个具体文件；描述具体接口、行为和可验证验收条件。先定义共享契约，再并行开发独立功能，'
            '规格应简洁，不写代码、推演过程或长篇设计；每项目标建议不超过300字，验收建议不超过240字。'
            '输出路径须遵守本地文件保护：避免 auth.py、tokens、credentials、cookies、secrets、.env 等敏感命名；'
            '认证业务源码可命名 access_control.py，用量源码可命名 usage.py；绝不能写入真实凭证。'
            '依赖必须使用 key。每个任务独占输出目录且目录之间不能嵌套或重叠。'
            '必要的共同接口通过已审核依赖产物只读传递，不得要求跨目录写入。'
            '只能从 execution_routes 选择路径，至少两个不同 logical_agent；不要把已配置当作已验证。'
            'input_files 只能选择 approved_input_files 的子集；没有则为 []。不创建任何费用批准或执行状态。'
            '不能运行 Shell、安装软件、访问网络或宣称测试通过。只提出验收方法。'
            f'外层文件方案中只返回一个文件 {PLAN_PATH}，其 content 为纯 JSON，结构为 '
            '{"max_parallel":2,"nodes":[{"key":"contract","title":"具体功能",'
            '"objective":"具体目标与接口约定，最多1600字", "acceptance":"可验证条件与验证方法",'
            '"agent_id":"execution_routes中的id","directory":"delivery/contract",'
            '"deliverables":["delivery/contract/schema.json"],"input_files":[],"dependencies":[],"cost_approved":false}]}。'
            '根据实际目标填充所有任务，不要照抄示例。若信息不足，将待确认内容写入目标和验收，不得虚构现有文件内容。\n'
        )
        description = instructions + json.dumps(context, ensure_ascii=False)
        if len(description) > 12000:
            raise HTTPException(422, '总目标上下文过长，请精简需求或选择必要输入文件')
        tid, stamp = uuid.uuid4().hex[:12], now()
        with self.store.connect() as db:
            db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files,cost_approved) VALUES(?,?,?,?,?,'backlog',?,?,NULL,NULL,?,?,?,0)",
                       (tid, ('规划：' + owner['title'])[:160], description, agent_id, 'agent', owner['priority'], '[]', stamp, stamp, json.dumps(owner['input_files'])))
            db.execute('INSERT INTO planning_runs VALUES(?,?,?,NULL,?)', (tid, parent, source_revision(owner), stamp))
            self.store.log(db, tid, '规划任务已准备；尚未调用模型。通过普通执行入口批准一次调用，费用、用量、取消和日志沿用现有机制。', source='planner')
            self.store.event(db, 'task.created', {'task_id': tid})
        return self.public({'task_id': tid, 'parent_id': parent, 'source_revision': source_revision(owner), 'imported_parent': None, 'created_at': stamp})

    def validate_output(self, task_id, files):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM planning_runs WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return
        if len(files) != 1 or files[0]['path'] != PLAN_PATH:
            raise ValueError('规划任务只允许写入任务规格 JSON，不能写实现代码')
        owner = self.task(row['parent_id'])
        if source_revision(owner) != row['source_revision']:
            raise ValueError('规划期间总目标已变化；旧规格不落盘，请重新规划')
        parse_spec(files[0]['content'], owner, self.registry)

    def import_spec(self, parent, data):
        if not data.approve_import:
            raise HTTPException(403, '请确认导入任务规格；此确认不批准模型调用或代码合并')
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM planning_runs WHERE task_id=? AND parent_id=?', (data.task_id, parent)).fetchone()
        if not row:
            raise HTTPException(404, '规划任务不属于当前总目标')
        if row['imported_parent']:
            raise HTTPException(409, '该规划已导入，不能重复创建任务')
        owner, task = self.task(parent), self.task(data.task_id)
        if source_revision(owner) != row['source_revision']:
            raise HTTPException(409, '总目标已变化，请重新规划')
        artifacts = (task.get('last_attempt') or {}).get('artifacts', [])
        if data.revision_id:
            with self.store.connect() as db:
                revised = db.execute('SELECT * FROM planning_revisions WHERE id=? AND task_id=? AND parent_id=?', (data.revision_id, task['id'], parent)).fetchone()
            candidate = self.candidate(task)
            if not revised or revised['imported_parent'] or data.manifest != revised['manifest'] or not candidate or revised['candidate_digest'] != digest(candidate):
                raise HTTPException(409, '本地修订不存在、已导入或原始候选已变化')
            try:
                specification = parse_spec(revised['specification'], owner, self.registry)
                if digest(specification.model_dump()) != revised['manifest']:
                    raise ValueError('本地修订内容哈希不符')
            except ValueError as exc:
                raise HTTPException(409, '本地修订校验失败：' + str(exc)) from None
        else:
            if task['status'] != 'done' or len(artifacts) != 1 or artifacts[0]['path'] != PLAN_PATH or data.manifest != digest(artifacts):
                raise HTTPException(409, '规划尚未完成或文件清单已变化')
            try:
                specification = self.verified_spec(task, owner)
            except (ValueError, OSError) as exc:
                raise HTTPException(409, '任务规格校验失败：' + str(exc)) from None
        # Do not overwrite existing plans/history, even a draft. Import creates a new goal.
        target, stamp = uuid.uuid4().hex[:12], now()
        nodes = [node.model_dump() for node in specification.nodes]
        plan = {'parent_id': target, 'state': 'draft', 'nodes': nodes, 'max_parallel': specification.max_parallel,
                'planner': 'model-spec-revised' if data.revision_id else 'model-spec', 'planning_task_id': task['id'], 'origin_parent_id': parent,
                'planning_revision_id': data.revision_id,
                'planner_note': ('模型候选经本地修订，原请求状态保持不变；' if data.revision_id else '模型已生成逐文件任务规格，') + '尚未批准执行；请核对范围、接口、依赖、路径与费用。',
                'revision': digest(specification.model_dump())}
        with self.store.connect() as db:
            db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,result,worktree,created_at,updated_at,input_files,cost_approved) VALUES(?,?,?,?,?,'backlog',?,?,NULL,NULL,?,?,?,0)",
                       (target, owner['title'][:145] + ' · 规划修订', owner['description'], 'auto', 'agent', owner['priority'], json.dumps(owner['requires_skills']), stamp, stamp, json.dumps(owner['input_files'])))
            for dep in owner['dependencies']:
                db.execute('INSERT INTO dependencies VALUES(?,?)', (target, dep))
            db.execute('INSERT INTO collaborations VALUES(?,?,?,?)', (target, 'draft', json.dumps({k: v for k, v in plan.items() if k not in ('parent_id', 'state')}, ensure_ascii=False), stamp))
            db.execute('UPDATE planning_runs SET imported_parent=? WHERE task_id=?', (target, task['id']))
            if data.revision_id:
                db.execute('UPDATE planning_revisions SET imported_parent=? WHERE id=?', (target, data.revision_id))
            self.store.log(db, target, f'已从规划任务 {task["id"]} 导入规格；原目标 {parent} 与历史保持不变。未创建执行子任务，未批准费用或合并。', source='planner')
            self.store.event(db, 'task.created', {'task_id': target})
        return self.collaborator.public(target)
