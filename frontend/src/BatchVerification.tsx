import { useState } from 'react';
import { RefreshCw, X } from 'lucide-react';
import { request } from './api';
import { useDialog } from './useDialog';
import type { Task } from './types';

type Route = { agent_id: string; name: string; runtime_id: string; model: string | null; provider_id: string; billing: string; eligible: boolean; reason: string | null };
type Preview = { routes: Route[]; revision: string; note: string };
type Result = { runtime_id: string; task_id?: string; accepted: boolean; reason?: string };

export function BatchVerification({ tasks, onChanged }: { tasks: Task[]; onChanged: () => Promise<unknown> }) {
  const [preview, setPreview] = useState<Preview | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<string[]>([]), [approved, setApproved] = useState(false), [results, setResults] = useState<Result[]>([]);
  const active = results.some(r => r.accepted && (!tasks.find(t => t.id === r.task_id) || tasks.some(t => t.id === r.task_id && ['running', 'backlog'].includes(t.status))));
  const open = async () => { setBusy(true); setError(''); try { const p = await request<Preview>('/agents/verification-batch'); setPreview(p); setSelected(p.routes.filter(r => r.eligible).map(r => r.runtime_id)); setApproved(false); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const submit = async () => { if (!preview) return; setBusy(true); setError(''); try { const result = await request<{ results: Result[] }>('/agents/verification-batch', 'POST', { revision: preview.revision, runtime_ids: selected, approve_usage: approved }); setResults(result.results); setPreview(null); setApproved(false); await onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  return <div className="batch-verification"><button className="button secondary" disabled={busy || active} onClick={() => void open()}><RefreshCw size={14} className={busy || active ? 'spin' : ''}/>{active ? '正在验证连接…' : '一键验证连接'}</button>
    {error && <p role="alert" className="amber-text">{error}</p>}
    {!!results.length && <div className="verification-results" role="status">{results.map(r => { const task = tasks.find(t => t.id === r.task_id); return <p key={r.runtime_id}><strong>{r.runtime_id}</strong> · {!r.accepted ? r.reason ?? '未提交' : task?.status === 'done' ? '真实连接验证通过' : task?.status === 'failed' ? `验证失败：${task.result ?? ''}` : task?.status === 'cancelled' ? '验证已取消' : task?.status === 'blocked' ? `受阻：${task.result ?? ''}` : '已提交，等待真实结果'}</p>; })}</div>}
    {preview && <VerificationDialog preview={preview} selected={selected} approved={approved} busy={busy} error={error} onClose={() => setPreview(null)} onToggle={id => { setSelected(selected.includes(id) ? selected.filter(x => x !== id) : [...selected, id]); setApproved(false); }} onApprove={setApproved} onSubmit={() => void submit()}/>}
  </div>;
}

function VerificationDialog({ preview, selected, approved, busy, error, onClose, onToggle, onApprove, onSubmit }: { preview: Preview; selected: string[]; approved: boolean; busy: boolean; error: string; onClose: () => void; onToggle: (id: string) => void; onApprove: (value: boolean) => void; onSubmit: () => void }) {
  const panel = useDialog(onClose);
  return <div className="modal-backdrop"><section ref={panel} className="modal" role="dialog" aria-modal="true" aria-label="一键验证连接确认"><div className="modal-header"><h2>一键验证连接</h2><button aria-label="关闭批量验证" onClick={onClose} disabled={busy}><X size={18}/></button></div><div className="batch-body">
    <p>{preview.note}</p><p className="form-note">验证会发送真实最小请求，消耗所选路径的订阅额度或 API 费用；提交成功不等于验证通过。忙碌、未授权及人工交接路径跳过，不自动切换。</p>
    {preview.routes.map(r => <label className="batch-route" key={r.runtime_id}><input type="checkbox" aria-label={`验证 ${r.name}`} disabled={busy || !r.eligible} checked={selected.includes(r.runtime_id)} onChange={() => onToggle(r.runtime_id)}/><span><strong>{r.name}</strong><small>{r.provider_id} · {r.model ?? '模型未报告'} · {r.billing === 'metered-api' ? '按量计费' : '订阅或当前账户额度'}</small><small>{r.eligible ? '配置就绪，可验证' : r.reason}</small></span></label>)}
    <label className="batch-route"><input type="checkbox" aria-label="批准本次批量验证" checked={approved} disabled={busy || !selected.length} onChange={e => onApprove(e.target.checked)}/><span>批准所选 {selected.length} 条路径各一次真实验证及相应额度消耗</span></label>
    {error && <p role="alert" className="amber-text">{error}</p>}
    <div className="modal-footer"><button className="button secondary" disabled={busy} onClick={onClose}>取消</button><button className="button primary" disabled={busy || !approved || !selected.length} onClick={onSubmit}>确认并验证</button></div>
  </div></section></div>;
}
