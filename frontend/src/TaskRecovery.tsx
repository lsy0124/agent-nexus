import { useState } from 'react';
import { request } from './api';
import type { Task, ExecutionCheckpoint } from './types';

const stages: Record<string, string> = {
  RESERVED: '已保存执行意图', INPUTS_VALIDATED: '已核对输入文件', REQUEST_STARTED: '请求已开始，结果待确认',
  RESPONSE_RECEIVED: '已保存模型响应', OUTPUT_VALIDATED: '已核对输出范围', WRITING_FILES: '已进入文件落盘',
  FILES_WRITTEN: '文件已校验写入', SNAPSHOTTING: 'Git 快照待确认', COMPLETED: '完成记录已保存', LEGACY_UNKNOWN: '历史执行边界未知',
};
type Preview = { checkpoint: ExecutionCheckpoint | null; can_release: boolean; requires_unknown_acknowledgement: boolean;
  note: string; path_error?: string; files: { path: string; matches_saved_result: boolean }[] };

export function TaskRecovery({ task, busy, onChanged }: { task: Task; busy: boolean; onChanged: () => Promise<unknown> }) {
  const [preview, setPreview] = useState<Preview | null>(null), [working, setWorking] = useState(false);
  const [error, setError] = useState(''), [reviewer, setReviewer] = useState(''), [note, setNote] = useState(''), [ack, setAck] = useState(false);
  const checkpoint = task.checkpoint;
  if (!checkpoint) return null;
  const run = async (action: () => Promise<unknown>) => {
    setWorking(true); setError('');
    try { await action(); await onChanged(); } catch (e) { setError((e as Error).message); }
    finally { setWorking(false); }
  };
  return <section className="drawer-section" aria-label="执行检查点与恢复">
    <h3>执行检查点{task.recovery_hold ? ' · 已暂停待审核' : ''}</h3>
    <p>{stages[checkpoint.stage] ?? checkpoint.stage}</p>
    <small>最后保存：{new Date(checkpoint.updated_at).toLocaleString('zh-CN', { hour12: false })}</small>
    {checkpoint.payload.authorized_directory && <p className="path-display">授权目录：{checkpoint.payload.authorized_directory}</p>}
    {task.recovery_hold && <p className="amber-text">执行结果需要核对，页面重连和服务重启均不会自动重放。此前请求可能已经完成或计费。</p>}
    <div className="drawer-actions">
      {task.status === 'running' && <button className="button secondary" disabled={busy || working || ['WRITING_FILES', 'FILES_WRITTEN', 'SNAPSHOTTING'].includes(checkpoint.stage)}
        onClick={() => void run(async () => { await request(`/tasks/${task.id}/pause`, 'POST', {}); setPreview(null); })}>暂停并保存状态</button>}
      <button className="button secondary" disabled={busy || working} onClick={() => void run(async () => {
        setPreview(await request<Preview>(`/tasks/${task.id}/recovery`)); setAck(false);
      })}>只读恢复检查</button>
    </div>
    {preview && <div className="result-box">
      <p>{preview.note}</p>
      {preview.path_error && <p className="amber-text">{preview.path_error}</p>}
      {preview.files.map(file => <p key={file.path}><code>{file.path}</code> · {file.matches_saved_result ? '匹配已保存哈希' : '缺失或内容已变更'}</p>)}
      {preview.can_release && task.recovery_hold && <>
        <label>恢复审核人<input aria-label="恢复审核人" maxLength={100} value={reviewer} onChange={e => setReviewer(e.target.value)}/></label>
        <label>核对说明<textarea aria-label="恢复核对说明" maxLength={2000} value={note} onChange={e => setNote(e.target.value)}/></label>
        {preview.requires_unknown_acknowledgement && <label className="check-row"><input aria-label="确认此前请求结果可能未知" type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)}/><span>已理解此前请求可能完成或计费；新尝试仍需另行批准。</span></label>}
        <button className="button secondary" disabled={busy || working || !reviewer.trim() || !note.trim() || (preview.requires_unknown_acknowledgement && !ack)} onClick={() => void run(async () => {
          await request(`/tasks/${task.id}/recovery/review`, 'POST', { checkpoint_id: preview.checkpoint!.id, version: preview.checkpoint!.version,
            reviewer, note, acknowledge_unknown_result: ack }); setPreview(null);
        })}>记录审核，解除新尝试阻塞（不执行）</button>
      </>}
      {!preview.can_release && task.recovery_hold && <p className="amber-text">可能已有文件或 Git 副作用，保持暂停；请人工审核现有 Worktree。</p>}
    </div>}
    {error && <p role="alert" className="amber-text">{error}</p>}
  </section>;
}
