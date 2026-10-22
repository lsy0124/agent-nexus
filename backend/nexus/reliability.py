"""Durable execution evidence and conservative pause/recovery, never model replay.

Filesystem writes and SQLite are not one transaction. A crash after WRITING_FILES
requires human review; this module never claims exactly-once remote execution.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import file_changes, worktrees
from .storage import now

STEPS = ['RESERVED', 'INPUTS_VALIDATED', 'REQUEST_STARTED', 'RESPONSE_RECEIVED',
         'OUTPUT_VALIDATED', 'WRITING_FILES', 'FILES_WRITTEN', 'SNAPSHOTTING', 'COMPLETED']
WRITE_STAGES = {'WRITING_FILES', 'FILES_WRITTEN', 'SNAPSHOTTING', 'COMPLETED', 'LEGACY_UNKNOWN'}


class RecoveryDecision(BaseModel):
    checkpoint_id: str = Field(max_length=80)
    version: int = Field(ge=1)
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    acknowledge_unknown_result: bool = False


class Checkpoints:
    def __init__(self, root, store):
        self.root, self.store = root, store

    def get(self, attempt_id, db=None):
        if db is None:
            with self.store.connect() as con:
                return self.get(attempt_id, con)
        row = db.execute('SELECT * FROM execution_checkpoints WHERE id=?', (attempt_id,)).fetchone()
        return self.store.decode_checkpoint(row) if row else None

    def reserve(self, db, attempt_id, task, adapter_id, binding, directory=None):
        target = Path(task['worktree'])
        if directory is not None:
            file_changes.relative_path(directory + '/scope.txt')
        scope = file_changes.checked_path(target, directory + '/scope.txt').parent if directory else target
        payload = {
            'agent_id': adapter_id, 'worktree': str(target), 'authorized_directory': str(scope),
            'directory': directory, 'input_files': task['input_files'], 'input_hashes': {},
            'task_digest': hashlib.sha256(json.dumps({k: task.get(k) for k in
                ('id', 'title', 'description', 'input_files', 'dependencies', 'requires_skills')}, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'provider_id': binding['provider']['id'], 'model_key': (binding.get('model') or {}).get('key'),
            'requested_model': binding.get('model_id'),
            'binding_digest': hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
            'known_result': 'NOT_STARTED', 'completed_steps': [], 'proposed_artifacts': [], 'artifacts': [],
            'retry_count': max(0, db.execute('SELECT count(*) FROM attempts WHERE task_id=?', (task['id'],)).fetchone()[0] - 1),
        }
        stamp = now()
        db.execute('INSERT INTO execution_checkpoints VALUES(?,?,?, ?,0,1,?,?,?)',
                   (attempt_id, task['id'], 'RESERVED', 'RUNNING', json.dumps(payload, ensure_ascii=False), stamp, stamp))

    def advance(self, db, attempt_id, stage, **metadata):
        checkpoint = self.get(attempt_id, db)
        if not checkpoint or checkpoint['state'] != 'RUNNING':
            raise ValueError('执行检查点已终结或不存在，拒绝重复副作用')
        if stage not in STEPS or STEPS.index(stage) <= STEPS.index(checkpoint['stage']):
            raise ValueError('执行检查点不能倒退')
        payload = checkpoint['payload']
        payload.update(metadata)
        if checkpoint['stage'] not in payload['completed_steps']:
            payload['completed_steps'].append(checkpoint['stage'])
        if stage == 'COMPLETED' and stage not in payload['completed_steps']:
            payload['completed_steps'].append(stage)
        db.execute('UPDATE execution_checkpoints SET stage=?,state=?,payload=?,version=version+1,updated_at=? WHERE id=?',
                   (stage, 'COMPLETED' if stage == 'COMPLETED' else 'RUNNING', json.dumps(payload, ensure_ascii=False), now(), attempt_id))

    def halt(self, db, attempt_id, reason, *, pause=False, cancelled=False, metrics=None, failure_kind=None):
        checkpoint = self.get(attempt_id, db)
        if not checkpoint or checkpoint['state'] == 'COMPLETED':
            return
        payload = checkpoint['payload']
        uncertain = checkpoint['stage'] == 'REQUEST_STARTED' or checkpoint['stage'] in WRITE_STAGES
        known_failure = failure_kind in {'authentication', 'billing', 'model_or_route_not_found', 'rate_limit',
                                        'provider_unavailable', 'http_error', 'protocol', 'output_limit', 'configuration', 'not_connected'}
        if checkpoint['stage'] == 'REQUEST_STARTED' and known_failure:
            uncertain = False
        payload.update(reason=reason, known_result='LOCAL_RESULT_UNKNOWN' if checkpoint['stage'] in WRITE_STAGES
                       else 'REMOTE_RESULT_UNKNOWN' if checkpoint['stage'] == 'REQUEST_STARTED'
                       else payload.get('known_result', 'NOT_STARTED'))
        if known_failure and checkpoint['stage'] == 'REQUEST_STARTED':
            payload['known_result'] = 'FAILED_RESPONSE'
        if metrics is not None:
            payload['metrics'] = metrics
        db.execute('UPDATE execution_checkpoints SET state=?,hold=?,payload=?,version=version+1,updated_at=? WHERE id=?',
                   ('PAUSED' if pause or uncertain else 'CANCELLED' if cancelled else 'FAILED',
                    int(pause or uncertain), json.dumps(payload, ensure_ascii=False), now(), attempt_id))
        if pause or uncertain:
            self.store.event(db, 'task.recovery_required', {'task_id': checkpoint['task_id'], 'checkpoint_id': attempt_id})

    def interrupt(self, db, task_id):
        rows = db.execute("SELECT id FROM execution_checkpoints WHERE task_id=? AND state='RUNNING'", (task_id,)).fetchall()
        for row in rows:
            self.halt(db, row['id'], '后端重启：上次执行中断，未自动重放', pause=True)
        if not rows:
            # Historical running tasks have no trustworthy write boundary.
            stamp = now()
            task = db.execute('SELECT agent_id,worktree FROM tasks WHERE id=?', (task_id,)).fetchone()
            payload = {'agent_id': task['agent_id'], 'worktree': task['worktree'], 'known_result': 'LOCAL_RESULT_UNKNOWN',
                       'reason': '后端重启：历史执行没有检查点，须人工核对', 'completed_steps': [], 'artifacts': [], 'proposed_artifacts': []}
            db.execute("INSERT OR IGNORE INTO execution_checkpoints VALUES(?,?,'LEGACY_UNKNOWN','PAUSED',1,1,?,?,?)",
                       ('legacy:' + task_id, task_id, json.dumps(payload, ensure_ascii=False), stamp, stamp))

    def preview(self, task):
        checkpoint = task.get('checkpoint')
        if not checkpoint:
            return {'checkpoint': None, 'can_release': False, 'files': [], 'note': '没有本次执行检查点；历史记录不推断为可无损续跑'}
        files, path_error = [], None
        expected = checkpoint['payload'].get('artifacts') or checkpoint['payload'].get('proposed_artifacts', [])
        try:
            if expected:
                target = worktrees.validate(self.root, task['id'], checkpoint['payload']['worktree'])
                for artifact in expected:
                    path = file_changes.checked_path(target, artifact['path'])
                    match = path.is_file() and path.stat().st_size == artifact['size'] and file_changes.sha(path.read_bytes()) == artifact['sha256']
                    files.append({'path': artifact['path'], 'matches_saved_result': match})
        except (ValueError, OSError) as exc:
            path_error = str(exc)
        return {'checkpoint': checkpoint, 'files': files, 'path_error': path_error,
                'can_release': checkpoint['hold'] and checkpoint['stage'] not in WRITE_STAGES,
                'requires_unknown_acknowledgement': checkpoint['payload'].get('known_result') == 'REMOTE_RESULT_UNKNOWN',
                'note': '恢复的是持久化状态。不会重放模型、写文件或 Git 提交；落盘可能已发生时保留暂停，需人工审核 Worktree。'}

    def release(self, task, decision):
        preview = self.preview(task)
        checkpoint = preview['checkpoint']
        if not checkpoint or checkpoint['id'] != decision.checkpoint_id or checkpoint['version'] != decision.version:
            raise HTTPException(409, '检查点已变化，请重新核对')
        if not preview['can_release'] or task['status'] == 'running':
            raise HTTPException(409, '本地副作用可能已经发生或任务未暂停；不能直接再次执行，请人工审核')
        if not decision.reviewer.strip() or not decision.note.strip():
            raise HTTPException(422, '需要填写审核人与核对说明')
        if preview['requires_unknown_acknowledgement'] and not decision.acknowledge_unknown_result:
            raise HTTPException(403, '必须明确确认此前请求可能已完成或计费；解除暂停不会自动执行')
        with self.store.connect() as db:
            current = self.get(checkpoint['id'], db)
            if current['version'] != decision.version or not current['hold']:
                raise HTTPException(409, '检查点已经变化')
            payload = current['payload'] | {'recovery_decision': decision.model_dump() | {'at': now()}}
            db.execute('UPDATE execution_checkpoints SET hold=0,payload=?,version=version+1,updated_at=? WHERE id=?',
                       (json.dumps(payload, ensure_ascii=False), now(), current['id']))
            self.store.log(db, task['id'], '人工恢复审核已记录；只解除新尝试的阻塞，不会自动调用模型或重放写入', source='recovery')
            self.store.event(db, 'task.recovery_reviewed', {'task_id': task['id'], 'checkpoint_id': current['id']})
        return {'released': True, 'executed': False}

    def reconcile_usage(self, collector):
        """Repair only missing local bookkeeping, not the remote request."""
        from .usage import Usage, UsageEvent
        with self.store.connect() as db:
            rows = db.execute("SELECT c.*,a.purpose,a.status attempt_status,a.error_kind FROM execution_checkpoints c JOIN attempts a ON a.id=c.id "
                              "WHERE c.stage NOT IN ('RESERVED','INPUTS_VALIDATED') AND c.state!='RUNNING' "
                              'AND NOT EXISTS(SELECT 1 FROM usage_records u WHERE u.attempt_id=c.id)').fetchall()
        for row in rows:
            checkpoint = self.store.decode_checkpoint(row)
            payload = checkpoint['payload']
            metrics = dict(payload.get('metrics') or {})
            source = metrics.pop('usage_source', 'unavailable')
            observed = Usage(source=source, **metrics)
            collector.record(UsageEvent(agent_id=payload['agent_id'], provider_id=payload['provider_id'],
                model_key=payload.get('reported_model_key') or payload.get('model_key'), requested_model=payload.get('requested_model'),
                task_id=row['task_id'], attempt_id=row['id'], purpose=row['purpose'], usage=observed,
                streaming=bool(payload.get('streaming')), status='ok' if row['attempt_status'] == 'done' else 'error',
                error_kind=row['error_kind']))
