import { useEffect, useState } from 'react';
import type { Agent, Task } from './types';
import { request } from './api';
import { useDialog } from './useDialog';
import { PlanningWorkbench } from './PlanningWorkbench';

type Node = { key: string; title: string; objective: string; acceptance: string; directory: string; agent_id: string; dependencies: string[]; cost_approved: boolean; deliverables?: string[]; input_files?: string[] | null; task_id?: string; task?: Task | null; phase?: string; error?: string; review?: { decision: string; reviewer: string; note: string }; manifest?: string };
type Plan = { parent_id: string; state: string; revision: string; planner_note: string; max_parallel: number; nodes: Node[] };
const labels: Record<string, string> = { draft: '拆分草案 · 尚未执行', running: '并行编排中', paused: '已暂停 · 不自动重试', waiting_review: '产物待人工审核', complete: '全部已批准合并' };

export function CollaborationPanel({ task, agents, onClose, onSelect, onChanged, onExecute }: { task: Task; agents: Agent[]; onExecute: () => void; onClose: () => void; onSelect: (id: string) => void; onChanged: () => Promise<unknown> }) {
  const panel = useDialog(onClose);
  const parent = task.collaboration_parent ?? task.id;
  const endpoint = `/tasks/${parent}/collaboration`;
  const [plan, setPlan] = useState<Plan | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false), [approve, setApprove] = useState(false), [codex, setCodex] = useState(false);
  const [reviewer, setReviewer] = useState(''), [note, setNote] = useState('');
  const load = async () => { const p = await request<Plan>(endpoint); setPlan(p); return p; };
  useEffect(() => {
    if (!task.collaboration_parent) return;
    let active = true;
    const refresh = () => request<Plan>(endpoint).then(p => { if (active) setPlan(previous => previous?.state === 'draft' ? previous : p); }).catch(e => { if (active) setError(e.message); });
    refresh(); const timer = setInterval(refresh, 1800);
    return () => { active = false; clearInterval(timer); };
  }, [endpoint, task.collaboration_parent]);
  // Draft editing is deliberately not polled: unsaved changes must survive.
  useEffect(() => {
    if (!plan || plan.state === 'draft' || task.collaboration_parent) return;
    const timer = setInterval(() => load().catch(e => setError(e.message)), 1800);
    return () => clearInterval(timer);
  }, [plan?.state, endpoint]);
  const act = async (fn: () => Promise<unknown>) => { if (busy) return; setBusy(true); setError(''); try { await fn(); await onChanged(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } };
  const edit = (key: string, patch: Partial<Node>) => { if (!plan) return; setPlan({ ...plan, nodes: plan.nodes.map(n => n.key === key ? { ...n, ...patch } : n) }); setDirty(true); setApprove(false); setCodex(false); };
  const review = (node: Node, decision: string) => act(async () => { setPlan(await request<Plan>(`${endpoint}/review/${node.key}`, 'POST', { decision, manifest: node.manifest, reviewer, note })); });
  return <div className="modal-backdrop"><section ref={panel} className="modal collaboration-modal" role="dialog" aria-modal="true" aria-label="多 Agent 协作">
    <div className="modal-header"><div><span className="eyebrow">COLLABORATION</span><h2>多 Agent 协作</h2></div><button aria-label="关闭协作方案" onClick={onClose}>×</button></div>
    <div className="collaboration-body"><h3>{task.title}</h3>{error && <p role="alert" className="amber-text">{error}</p>}
    <PlanningWorkbench parent={parent} agents={agents} disabled={busy || !!plan && ['running', 'waiting_review'].includes(plan.state)} onChanged={onChanged} onOpenTask={id => { onClose(); onSelect(id); }}/>
    {!plan ? <><p>总目标先拆成子任务，再分配给不同 Agent。总任务不会整包提交给一个执行器。</p><p className="form-note">先生成免费的本地模板草案，需你编辑确认；不会调用模型或产生费用。当前桥接支持文本文件产物，通用 Shell 与自动运行模型生成测试关闭。</p><button className="button primary" disabled={busy} onClick={() => act(async () => { setPlan(await request<Plan>(endpoint + '/draft', 'POST')); })}>生成拆分草案</button></> : <>
      <p className="success-text">{labels[plan.state] ?? plan.state} · 并发上限 {plan.max_parallel}</p>
      <p className="form-note">{plan.planner_note} 独立子任务并行；有依赖的任务需先完成人工协作审核。每个子任务最多尝试一次；重启暂停队列。审核允许只读传递产物，不会自动合并。</p>
      {plan.state === 'draft' && <label>最大并发<select aria-label="协作并发上限" value={plan.max_parallel} onChange={e => { setPlan({ ...plan, max_parallel: +e.target.value }); setDirty(true); setApprove(false); }}><option value={2}>2 个子任务</option><option value={3}>3 个子任务</option></select></label>}
      <div className="collaboration-nodes">{plan.nodes.map(node => <article className="collaboration-node" key={node.key}>
        <div className="section-heading"><h3>{node.key} · {node.title}</h3><span>{node.task ? node.task.status === 'done' ? '产出完成 · 仍需审核' : node.task.status : '尚未创建执行任务'}</span></div>
        {plan.state === 'draft' ? <>
          <label>子任务标题<input aria-label={`${node.key} 标题`} maxLength={100} value={node.title} onChange={e => edit(node.key, { title: e.target.value })}/></label>
          <label>具体目标<textarea aria-label={`${node.key} 目标`} maxLength={5000} rows={3} value={node.objective} onChange={e => edit(node.key, { objective: e.target.value })}/></label>
          <label>验收标准<textarea aria-label={`${node.key} 验收`} maxLength={2000} rows={2} value={node.acceptance} onChange={e => edit(node.key, { acceptance: e.target.value })}/></label>
          <label>具体交付文件（每行一个，最多 3 个；旧模板可留空）<textarea aria-label={`${node.key} 交付文件`} rows={2} value={(node.deliverables ?? []).join('\n')} onChange={e => edit(node.key, { deliverables: e.target.value.split('\n').map(s => s.trim()).filter(Boolean) })}/></label>
          <label>输入文件（每行一个，必须已在总任务选择）<textarea aria-label={`${node.key} 输入文件`} rows={2} value={(node.input_files ?? task.input_files ?? []).join('\n')} onChange={e => edit(node.key, { input_files: e.target.value.split('\n').map(s => s.trim()).filter(Boolean) })}/></label>
          <div className="form-row"><label>执行路径<select aria-label={`${node.key} 执行路径`} value={node.agent_id} onChange={e => edit(node.key, { agent_id: e.target.value, cost_approved: false })}><option value="">请选择</option>{agents.filter(a => a.id !== 'workbuddy').map(a => <option key={a.id} value={a.id} disabled={!a.ready}>{a.name} · {a.model ?? '未报告模型'} · {a.provider_id ?? a.binding?.provider?.id} · {a.ready ? '配置就绪' : '未就绪'}</option>)}</select></label>
          <label>独占输出目录<input aria-label={`${node.key} 输出目录`} maxLength={150} value={node.directory} onChange={e => edit(node.key, { directory: e.target.value })}/></label></div>
          <label>依赖 key（逗号分隔）<input aria-label={`${node.key} 依赖`} value={node.dependencies.join(',')} onChange={e => edit(node.key, { dependencies: e.target.value.split(',').map(s => s.trim()).filter(Boolean) })}/></label>
          {agents.find(a => a.id === node.agent_id)?.billing === 'metered-api' && <label className="collaboration-check"><input type="checkbox" aria-label={`${node.key} 费用批准`} checked={node.cost_approved} onChange={e => edit(node.key, { cost_approved: e.target.checked })}/><span>批准该子任务当前 Provider 的一次按量调用</span></label>}
        </> : <><p>{node.objective}</p><p className="form-note">验收：{node.acceptance}</p><code>{node.directory}/</code><p>执行端：{node.agent_id} · 依赖：{node.dependencies.join('、') || '无，可并行'}</p>
          {node.error && <p className="amber-text">{node.error}</p>}
          {node.task && <><p className="form-note">{node.task.result ?? '等待依赖审核或执行资源'} · Token：{node.task.last_attempt?.usage ? JSON.stringify(node.task.last_attempt.usage) : '未报告'}。测试：未由通用测试执行器运行，不能视为通过。</p><button className="button secondary" onClick={() => { if (node.task_id) { onClose(); onSelect(node.task_id); } }}>查看文件、日志与 Git 审核</button></>}
          {node.review ? <p>协作审核：{node.review.decision === 'approve' ? '批准只读传递产物' : '拒绝'} · {node.review.reviewer}</p> : node.task?.status === 'done' && <div className="drawer-actions"><button className="button secondary" disabled={busy || !reviewer.trim() || !note.trim()} onClick={() => review(node, 'approve')}>批准产物供后续任务读取</button><button className="button secondary" disabled={busy || !reviewer.trim() || !note.trim()} onClick={() => review(node, 'reject')}>拒绝产物并暂停</button></div>}
        </>}
        {plan.state === 'draft' && <button className="text-button" disabled={plan.nodes.length <= 2} onClick={() => { setPlan({ ...plan, nodes: plan.nodes.filter(n => n.key !== node.key) }); setDirty(true); setApprove(false); }}>移除此子任务</button>}
      </article>)}</div>
      {plan.state !== 'draft' && <div className="form-row"><label>协作审核人<input aria-label="协作审核人" value={reviewer} onChange={e => setReviewer(e.target.value)}/></label><label>审核依据与测试证据<input aria-label="协作审核说明" value={note} onChange={e => setNote(e.target.value)}/></label></div>}
      {['draft', 'paused', 'waiting_review'].includes(plan.state) && <>
        {plan.state === 'draft' && <div className="drawer-actions"><button className="button secondary" disabled={busy || plan.nodes.length >= 12} onClick={() => { const key = 'task_' + Date.now().toString(36); setPlan({ ...plan, nodes: [...plan.nodes, { key, title: '新增子任务', objective: '', acceptance: '', agent_id: '', directory: 'nexus-delivery/' + key, dependencies: [], cost_approved: false }] }); setDirty(true); setApprove(false); }}>添加子任务</button><button className="button secondary" disabled={busy} onClick={() => act(async () => { setPlan(await request<Plan>(endpoint, 'PUT', { nodes: plan.nodes, max_parallel: plan.max_parallel })); setDirty(false); setApprove(false); })}>保存拆分方案</button></div>}
        <label className="collaboration-check"><input type="checkbox" aria-label="批准协作方案" checked={approve} disabled={dirty} onChange={e => setApprove(e.target.checked)}/><span>批准当前方案与逐项单次调用；不批准自动合并</span></label>
        {plan.nodes.some(n => n.agent_id === 'codex') && <label className="collaboration-check"><input type="checkbox" checked={codex} onChange={e => setCodex(e.target.checked)}/><span>另行批准本方案使用 Codex，保留预算检查</span></label>}
        {dirty && <p className="amber-text">请先保存修改，再批准当前版本。</p>}
        <button className="button secondary" disabled={busy || dirty} onClick={onExecute}>一键执行 / 修订失败节点</button>
        <button className="button primary" disabled={busy || dirty || !approve} onClick={() => act(async () => { setPlan(await request<Plan>(endpoint + '/start', 'POST', { revision: plan.revision, approve_plan: approve, approve_codex: codex })); setApprove(false); })}>{plan.state === 'draft' ? '批准并启动协作' : '继续尚未尝试的子任务'}</button>
      </>}
      {['running', 'waiting_review'].includes(plan.state) && <button className="button secondary" disabled={busy} onClick={() => act(async () => { await request(endpoint + '/pause', 'POST'); await load(); })}>暂停队列并停止运行任务</button>}
      <p className="form-note">最终代码合并由各子任务 Git 审核面板单独批准。失败或取消不会自动重试；失败节点请点击“一键执行 / 修订失败节点”，确认创建修订，旧记录保留。</p>
    </>}
    </div>
  </section></div>;
}
