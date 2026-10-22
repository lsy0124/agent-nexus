import { useEffect, useState } from 'react';
import type { Agent, Task } from './types';
import { request } from './api';

type Specification = { nodes: { key: string; title: string; objective: string; acceptance: string; agent_id: string; deliverables: string[]; dependencies: string[] }[] };
type Run = { task_id: string; imported_parent: string | null; task: Task; manifest: string; source_current: boolean; specification: Specification | null; error: string | null; candidate?: string | null; candidate_digest?: string | null; revisions?: { id: string; manifest: string; specification: Specification; imported_parent: string | null }[] };

export function PlanningWorkbench({ parent, agents, disabled, onOpenTask, onChanged }: { parent: string; agents: Agent[]; disabled: boolean; onOpenTask: (id: string) => void; onChanged: () => Promise<unknown> }) {
  const [runs, setRuns] = useState<Run[]>([]), [route, setRoute] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [approved, setApproved] = useState('');
  const [edits, setEdits] = useState<Record<string, string>>({});
  const endpoint = `/tasks/${parent}/planning`;
  useEffect(() => {
    let active = true;
    const refresh = () => request<{ runs: Run[] }>(endpoint).then(data => { if (active) setRuns(data.runs); }).catch(e => { if (active) setError(e.message); });
    refresh(); const timer = setInterval(refresh, 2500);
    return () => { active = false; clearInterval(timer); };
  }, [endpoint]);
  const act = async (fn: () => Promise<void>) => { if (busy) return; setBusy(true); setError(''); try { await fn(); await onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const showSpec = (run: Run, specification: Specification, manifest: string, revisionId?: string) => {
    const id = revisionId ?? run.task_id;
    return <><ol>{specification.nodes.map(n => <li key={n.key}><strong>{n.key} · {n.title}</strong><p>{n.objective}</p><p className="form-note">{n.agent_id} · 依赖：{n.dependencies.join('、') || '无'}<br/>文件：{n.deliverables.join('、')}<br/>验收：{n.acceptance}</p></li>)}</ol>
      {!run.imported_parent && <><label className="collaboration-check"><input type="checkbox" aria-label={`确认导入 ${id}`} checked={approved === id} onChange={e => setApproved(e.target.checked ? id : '')}/><span>已检查具体任务规格，创建新的协作草案并保留旧记录；不批准执行或合并</span></label><button className="button primary" disabled={busy || disabled || !run.source_current || approved !== id} onClick={() => act(async () => { const plan = await request<{ parent_id: string }>(endpoint + '/import', 'POST', { task_id: run.task_id, manifest, revision_id: revisionId, approve_import: true }); onOpenTask(plan.parent_id); })}>导入为新协作草案</button></>}
    </>;
  };
  return <section className="planning-workbench" aria-label="项目 AI 规划">
    <h3>项目 AI 规划</h3>
    <p className="form-note">先生成具体任务规格，再审核分发。每项限定 1–3 个文件、输入上下文、接口与验收条件。规划本身是独立任务，准备后需在普通执行入口批准一次调用；这里不会直接消耗额度。</p>
    <div className="form-row"><label>规划执行路径<select aria-label="规划执行路径" value={route} disabled={busy || disabled} onChange={e => setRoute(e.target.value)}><option value="">请选择配置就绪的路径</option>{agents.filter(a => a.id !== 'workbuddy' && a.ready && a.supported_capabilities?.includes('file.generate')).map(a => <option value={a.id} key={a.id}>{a.name} · {a.model ?? '模型未报告'}</option>)}</select></label>
    <button className="button primary" disabled={busy || disabled || !route} onClick={() => act(async () => { const run = await request<Run>(endpoint, 'POST', { agent_id: route }); onOpenTask(run.task_id); })}>准备 AI 规划任务</button></div>
    {disabled && <p className="amber-text">请先暂停当前协作队列，再重新规划。</p>}
    {error && <p className="amber-text" role="alert">{error}</p>}
    {runs.map(run => <details key={run.task_id} className="planning-run"><summary>{run.task.title} · {run.task.status} {run.imported_parent ? '· 已导入' : ''}</summary>
      <button className="button secondary" onClick={() => onOpenTask(run.task_id)}>查看规划任务与真实日志</button>
      {run.task.status === 'failed' && <p className="amber-text">{run.task.result ?? '规划失败，请查看真实日志；未自动重试。'}</p>}
      {!run.source_current && <p className="amber-text">总目标已变化，此规划不能导入。</p>}{run.error && <p className="amber-text">{run.error}</p>}
      {run.specification && showSpec(run, run.specification, run.manifest)}
      {run.candidate && !run.imported_parent && <details><summary>本地修订规格 JSON · 不调用模型</summary><p className="form-note">原始候选和失败状态保留。此操作只校验并保存本地修订；文件保护规则不变，仍需另外确认导入。</p><label>规格 JSON<textarea aria-label={`规格 JSON ${run.task_id}`} maxLength={48000} rows={12} value={edits[run.task_id] ?? run.candidate} onChange={e => { setEdits({ ...edits, [run.task_id]: e.target.value }); setApproved(''); }}/></label><button className="button secondary" disabled={busy || disabled || !run.source_current} onClick={() => act(async () => { await request(endpoint + '/revise', 'POST', { task_id: run.task_id, candidate_digest: run.candidate_digest, specification: edits[run.task_id] ?? run.candidate }); const data = await request<{ runs: Run[] }>(endpoint); setRuns(data.runs); setApproved(''); })}>校验并保存本地修订</button></details>}
      {(run.revisions ?? []).map(revision => <details key={revision.id} className="planning-revision"><summary>本地修订 {revision.id} · 已通过结构校验 · 尚未批准执行</summary>{showSpec(run, revision.specification, revision.manifest, revision.id)}</details>)}
      {run.imported_parent && <button className="button secondary" onClick={() => onOpenTask(run.imported_parent!)}>打开已导入的协作目标</button>}
    </details>)}
  </section>;
}
