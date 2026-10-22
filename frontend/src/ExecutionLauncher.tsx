import { useEffect, useState } from 'react';
import { request } from './api';
import { useDialog } from './useDialog';
import { GatewayRequestOptions } from './GatewayRequestOptions';
import type { Agent } from './types';

type Scope = { execution_context?: string; key: string; title: string; agent_id: string; provider: string; model: string | null; repair: boolean; dependencies: string[]; busy?: boolean; objective?: string; acceptance?: string; directory?: string; request_options?: { stream: boolean; max_output_tokens: number; reasoning?: string } | null; previous_error?: string | null };
type Preview = { items: { id: string; title: string; mode: string }[]; selected: { id: string; title: string; agent_id: string; mode: string; repair_keys: string[] } | null; scopes: Scope[]; blockers: string[]; eligible: boolean; revision: string; note?: string };

export function ExecutionLauncher({ initialTask, agents, onClose, onChanged }: { initialTask: string; agents: Agent[]; onClose: () => void; onChanged: () => Promise<unknown> }) {
  const panel = useDialog(onClose);
  const [task, setTask] = useState(initialTask), [route, setRoute] = useState(''), [refresh, setRefresh] = useState(0);
  const [preview, setPreview] = useState<Preview | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState(false), [repair, setRepair] = useState(false), [codex, setCodex] = useState(false);
  useEffect(() => {
    let alive = true; setBusy(true); setError(''); setUsage(false); setRepair(false); setCodex(false);
    const params = new URLSearchParams(); if (task) params.set('task_id', task); if (route) params.set('agent_id', route);
    request<Preview>('/execution/preview?' + params).then(p => { if (alive) setPreview(p); }).catch(e => { if (alive) { setPreview(null); setError(e.message); } }).finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [task, route, refresh]);
  const submit = async () => {
    if (!preview?.selected || busy) return;
    setBusy(true); setError('');
    try {
      const result = await request<{ accepted: boolean; reason?: string }>('/execution/run', 'POST', { task_id: preview.selected.id, agent_id: route || null, revision: preview.revision, approve_usage: usage, approve_revisions: repair, approve_codex: codex });
      if (!result.accepted) throw new Error(result.reason ?? '任务未获调度，请刷新检查');
      await onChanged(); onClose();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const needsRepair = !!preview?.selected?.repair_keys.length, needsCodex = preview?.scopes.some(s => s.agent_id === 'codex');
  return <div className="modal-backdrop"><section ref={panel} className="modal collaboration-modal" role="dialog" aria-modal="true" aria-label="一键执行任务"><div className="modal-header"><h2>一键执行</h2><button aria-label="关闭执行预览" disabled={busy} onClick={onClose}>×</button></div><div className="batch-body">
    <p>选择一个总目标，核对执行范围后启动。协作任务按依赖并行派发，不会把全部任务交给一个 Agent。</p>
    {preview && <><label className="field">本次目标<select aria-label="本次执行目标" disabled={busy} value={task || preview.selected?.id || ''} onChange={e => { setTask(e.target.value); setRoute(''); }}><option value="" disabled>选择任务</option>{preview.items.map(i => <option key={i.id} value={i.id}>{i.title} · {i.mode === 'collaboration' ? '多 Agent 协作' : '独立任务'} · {i.id}</option>)}</select></label>
      {preview.selected?.mode === 'standalone' && preview.selected.agent_id === 'auto' && <label className="field">执行路径<select aria-label="一键执行路径" value={route} disabled={busy} onChange={e => setRoute(e.target.value)}><option value="">请选择已配置路径</option>{agents.filter(a => a.ready && a.id !== 'workbuddy').map(a => <option key={a.id} value={a.id}>{a.name} · {a.model ?? '模型未报告'}</option>)}</select></label>}
      <p className="form-note">{preview.note}</p>
      {preview.scopes.map(s => <div className="batch-route" key={s.key}><span><strong>{s.title}{s.repair ? ' · 新建修订任务' : ''}</strong><small>{s.agent_id} · {s.provider} · {s.model ?? '由执行器决定，结果中核对'}</small><small>{s.dependencies.length ? `等待前置产物人工审核：${s.dependencies.join('、')}` : '独立节点，可并行'}{s.busy ? ' · 当前忙碌，协作队列等待空闲' : ''}</small>{s.request_options && <small>{s.request_options.stream ? 'SSE 流式' : '非流式'} · 最大输出 {s.request_options.max_output_tokens} Token（含服务商计入的推理输出） · 请求思考：{s.request_options.reasoning ?? 'provider'}</small>}{s.previous_error && <small className="amber-text">上次失败：{s.previous_error}</small>}{s.objective && <details><summary>查看目标、目录与验收</summary><p style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{s.execution_context ?? s.objective}</p>{s.directory && <small>仅允许输出：{s.directory}/</small>}{s.acceptance && <small>验收：{s.acceptance}</small>}</details>}</span></div>)}
      {preview.scopes.some(s => s.request_options) && <GatewayRequestOptions runtimeIds={preview.scopes.map(s => s.agent_id)} onSaved={() => { setUsage(false); setRepair(false); setCodex(false); setRefresh(n => n + 1); }}/>}
      {!!preview.blockers.length && <div className="result-box" role="status"><strong>尚不能启动</strong>{preview.blockers.map(b => <p key={b}>{b}</p>)}</div>}
      {needsRepair && <label className="batch-route"><input aria-label="批准失败节点修订" type="checkbox" checked={repair} onChange={e => setRepair(e.target.checked)} disabled={busy}/><span>批准为 {preview.selected?.repair_keys.length} 个失败或受阻节点创建新任务。保留旧日志、结果与 Worktree，每个新节点仅尝试一次。</span></label>}
      {needsCodex && <label className="batch-route"><input aria-label="单独批准 Codex" type="checkbox" checked={codex} onChange={e => setCodex(e.target.checked)} disabled={busy}/><span>单独批准本次使用 Codex 订阅额度，仍检查预算。</span></label>}
      <label className="batch-route"><input aria-label="批准本次任务执行" type="checkbox" checked={usage} onChange={e => setUsage(e.target.checked)} disabled={busy || !preview.eligible}/><span>批准上述任务及其依赖解锁后的节点消耗当前路径额度；不自动切换 Provider，不自动重试。最终合并仍需另行批准。</span></label>
    </>}
    {error && <p role="alert" className="amber-text">{error}</p>}
    <div className="modal-footer"><button className="button secondary" disabled={busy} onClick={onClose}>返回</button><button className="button primary" disabled={busy || !preview?.eligible || !usage || (needsRepair && !repair) || (needsCodex && !codex)} onClick={() => void submit()}>{busy ? '正在检查…' : '确认并一键执行'}</button></div>
  </div></section></div>;
}
