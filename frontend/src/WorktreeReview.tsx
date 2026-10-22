import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, CheckCheck, FileDiff, GitMerge, RefreshCw, XCircle } from 'lucide-react';
import { request } from './api';
import type { Task } from './types';

type ReviewFile = { path: string; added: number | null; removed: number | null; binary: boolean };
type ReviewStatus = {
  task_id: string; branch: string; base: string; task_head: string;
  files: ReviewFile[]; file_count: number;
  project: { branch: string; head: string; working_tree_dirty: boolean; commits_behind: number };
  overlapping_files: string[]; already_merged: boolean; has_changes: boolean;
  merge_identity: { configured: boolean; name: string | null };
};
type Preflight = { status: 'clean' | 'conflict' | 'not_checked'; detail: string; conflicts?: string[] };

/**
 * Worktree review gate: real Git diff, read-only conflict preflight, and a merge
 * that only happens after an explicit human decision. Nothing here stages,
 * discards or pushes anything; an approved merge is an ordinary Git merge commit
 * that `git revert` can undo.
 */
export function WorktreeReview({ task, busy, onChanged }: { task: Task; busy: boolean; onChanged: () => void }) {
  const eligible = task.kind === 'agent' && !!task.worktree && task.status !== 'running';
  const [status, setStatus] = useState<ReviewStatus | null>(null), [preflight, setPreflight] = useState<Preflight | null>(null);
  const [diff, setDiff] = useState(''), [truncated, setTruncated] = useState(false);
  const [pending, setPending] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [reviewer, setReviewer] = useState(''), [note, setNote] = useState(''), [ack, setAck] = useState(false);

  const load = useCallback(async () => {
    if (!eligible) return;
    try { const result = await request<{ status: ReviewStatus }>(`/tasks/${task.id}/review`); setStatus(result.status); setError(''); }
    catch (e) { setError((e as Error).message); }
  }, [eligible, task.id]);
  useEffect(() => { void load(); }, [load]);
  if (!eligible) return null;

  const act = async (action: () => Promise<void>) => {
    setPending(true); setError(''); setNotice('');
    try { await action(); onChanged(); } catch (e) { setError((e as Error).message); } finally { setPending(false); }
  };
  const showDiff = () => act(async () => {
    const result = await request<{ diff: string; truncated: boolean }>(`/tasks/${task.id}/review/diff`);
    setDiff(result.diff || '（该分支相对基线没有文本差异）'); setTruncated(result.truncated);
  });
  const runPreflight = () => act(async () => {
    const result = await request<Preflight>(`/tasks/${task.id}/review/preflight`, 'POST');
    setPreflight(result);
    setNotice(result.status === 'clean' ? '合并预检通过：无冲突。' : result.status === 'conflict' ? '发现冲突，未修改任何文件。' : '未能完成预检，请查看说明。');
  });
  const decide = (approve: boolean) => act(async () => {
    const payload = { reviewer: reviewer.trim(), note: note.trim(), ...(approve ? { approve_merge: true, acknowledge_unpushed: ack } : {}) };
    const result = await request<{ merged: boolean; record: { merge_commit?: string } }>(
      `/tasks/${task.id}/review/${approve ? 'merge' : 'reject'}`, 'POST', payload);
    setNotice(approve ? `已合并到当前分支，合并提交 ${result.record.merge_commit?.slice(0, 12) ?? ''}；未推送远程。` : '已记录拒绝决定，未修改任何文件。');
    setPreflight(null); setDiff(''); setNote(''); setAck(false);
    await load();
  });
  const ready = reviewer.trim().length > 0 && note.trim().length > 0 && !pending && !busy;
  const blocked = !status?.has_changes || status?.already_merged || !status?.merge_identity.configured;

  return <div className="drawer-section worktree-review">
    <h3><GitMerge size={15}/>Worktree 审查<span>{status ? `${status.file_count} 个文件` : '读取中'}</span></h3>
    {status && <>
      <dl className="task-meta">
        <div><dt>任务分支</dt><dd className="mono">{status.branch}</dd></div>
        <div><dt>基线 / 任务提交</dt><dd className="mono">{status.base} → {status.task_head}</dd></div>
        <div><dt>当前分支</dt><dd className="mono">{status.project.branch || '（游离 HEAD）'}{status.project.working_tree_dirty && <small className="amber-text">工作区有未提交改动</small>}</dd></div>
        <div><dt>落后提交</dt><dd>{status.project.commits_behind}{status.project.commits_behind > 0 && <small>项目已前进，合并前请先看预检</small>}</dd></div>
      </dl>
      {status.already_merged && status.has_changes && <p className="form-note">该任务分支已合并到当前分支，无需再次合并。</p>}
      {!status.has_changes && <p className="form-note">该分支尚无提交：还没有可审查或合并的内容。</p>}
      {!status.merge_identity.configured && <p className="amber-text">Git 未配置 user.name / user.email，无法创建合并提交。</p>}
      {status.overlapping_files.length > 0 && <p className="amber-text"><AlertTriangle size={12}/> 项目分支也改动了这些文件，请重点核对：{status.overlapping_files.join('、')}</p>}
      {status.files.length > 0 && <ul className="review-files">{status.files.map(f => <li key={f.path}><span className="mono">{f.path}</span><em>{f.binary ? '二进制' : `+${f.added ?? 0} / -${f.removed ?? 0}`}</em></li>)}</ul>}
      {status.files.length === 0 && <p className="form-note">该分支相对基线没有文件改动。</p>}
      <div className="review-actions">
        <button className="button secondary" disabled={pending} onClick={() => void showDiff()}><FileDiff size={13}/>加载真实 Diff</button>
        <button className="button secondary" disabled={pending} onClick={() => void runPreflight()}><RefreshCw size={13}/>合并预检（只读）</button>
      </div>
      {diff && <details className="review-diff" open><summary>Git Diff{truncated && '（已截断）'}</summary><pre>{diff}</pre></details>}
      {preflight && <p className={preflight.status === 'clean' ? 'form-note' : 'amber-text'}>{preflight.detail}</p>}
      <form className="review-decision" onSubmit={e => { e.preventDefault(); void decide(true); }}>
        <label>审核人<input required maxLength={100} value={reviewer} onChange={e => setReviewer(e.target.value)}/></label>
        <label>审核说明<textarea required rows={2} maxLength={2000} value={note} onChange={e => setNote(e.target.value)} placeholder="说明核对过的差异与验收结论"/></label>
        <label className="check-row"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)}/><span>我确认当前工作区存在未提交改动，仍要在此基础上合并</span></label>
        <div className="review-actions">
          <button className="button primary" disabled={!ready || blocked || preflight?.status !== 'clean'}><CheckCheck size={13}/>批准并合并到当前分支</button>
          <button type="button" className="button secondary" disabled={!ready} onClick={() => void decide(false)}><XCircle size={13}/>拒绝</button>
        </div>
        <small>合并只在预检通过后执行，产生一个普通 Git 合并提交，可用 git revert 撤销；Nexus 不会推送远程，也不会删除任务分支或 Worktree。</small>
      </form>
    </>}
    {error && <p role="alert" className="amber-text">{error}</p>}{notice && <p role="status">{notice}</p>}
  </div>;
}
