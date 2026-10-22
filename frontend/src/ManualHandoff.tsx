import { useEffect, useState } from 'react';
import { CheckCheck, Copy, Download, FileCode2, PackageOpen, ShieldCheck, Upload } from 'lucide-react';
import { request } from './api';
import type { HandoffRecord, HandoffSubmission, Task } from './types';

const phases: Record<HandoffRecord['phase'], string> = {
  prepared: '待你确认', confirmed: '等待外部结果', awaiting_review: '待人工审查',
  approved: '已审核并写入', rejected: '已拒绝', cancelled: '已取消',
};
const claimLabel = (status: string) => ({ passed: '外部申报：通过', failed: '外部申报：失败',
  skipped: '外部申报：跳过', not_run: '外部申报：未运行', unknown: '外部申报：未知' }[status] ?? `外部申报：${status}`);
const MAX_IMPORT = 1_200_000;

function downloadText(text: string, name: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
  const anchor = document.createElement('a');
  anchor.href = url; anchor.download = name; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function ManualHandoff({ task, busy, onChanged }: {
  task: Task; busy: boolean; onChanged: () => Promise<unknown> | void;
}) {
  const [handoff, setHandoff] = useState<HandoffRecord | null>(task.latest_handoff ?? null);
  const [working, setWorking] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const [raw, setRaw] = useState(''), [sender, setSender] = useState('');
  const [reviewer, setReviewer] = useState(''), [note, setNote] = useState(''), [approved, setApproved] = useState(false);
  useEffect(() => {
    let active = true;
    const incoming = task.latest_handoff ?? null;
    setHandoff(current => incoming ? { ...incoming, packet: incoming.phase === 'prepared' ? null :
      incoming.packet ?? (current?.id === incoming.id ? current.packet : null) } : null);
    if (incoming && incoming.phase !== 'prepared') {
      request<{ handoff: HandoffRecord }>(`/handoffs/${incoming.id}`).then(result => {
        if (active) setHandoff(result.handoff);
      }).catch(cause => { if (active) setError((cause as Error).message); });
    }
    return () => { active = false; };
  }, [task.id, task.latest_handoff?.id, task.latest_handoff?.phase, task.updated_at]);
  useEffect(() => { setRaw(''); setSender(''); setReviewer(''); setNote(''); setApproved(false); setError(''); setMessage(''); }, [task.id]);
  if (task.agent_id !== 'workbuddy') return null;
  const pending = busy || working;
  const perform = async (action: () => Promise<void>) => {
    if (pending) return;
    setWorking(true); setError(''); setMessage('');
    try { await action(); await onChanged(); }
    catch (cause) { setError((cause as Error).message); }
    finally { setWorking(false); }
  };
  const prepare = () => perform(async () => {
    const result = await request<{ handoff: HandoffRecord }>(`/tasks/${task.id}/handoff/prepare`, 'POST');
    setHandoff(result.handoff); setRaw(''); setApproved(false);
    setMessage('任务包已在本地准备，尚未复制或发送。');
  });
  const copyPacket = () => perform(async () => {
    if (!handoff) return;
    let record = handoff;
    if (record.phase === 'prepared') {
      const result = await request<{ handoff: HandoffRecord; packet: string }>(`/handoffs/${record.id}/confirm`, 'POST');
      record = { ...result.handoff, packet: result.packet };
    } else if (!record.packet) {
      const result = await request<{ handoff: HandoffRecord }>(`/handoffs/${record.id}`);
      record = result.handoff;
    }
    setHandoff(record);
    if (!record.packet) throw new Error('任务包尚未确认，不能复制。');
    try {
      await navigator.clipboard.writeText(record.packet);
      setMessage('任务包已复制。请自行粘贴到 WorkBuddy；Nexus 没有发送消息。');
    } catch {
      setMessage('已确认任务包；浏览器未允许写入剪贴板。请使用下方文本框手动复制，或下载任务包。');
    }
  });
  const importResult = () => perform(async () => {
    if (!handoff || handoff.phase !== 'confirmed') throw new Error('请先确认本轮任务包。');
    if (new Blob([raw]).size > MAX_IMPORT) throw new Error('结果 JSON 超过 1.2 MB 限制。');
    let parsed: unknown;
    try { parsed = JSON.parse(raw); } catch { throw new Error('结果必须是有效 JSON；不会执行导入内容。'); }
    const candidate = parsed as { submission?: HandoffSubmission };
    const submission = (candidate && typeof candidate === 'object' && candidate.submission ? candidate.submission : parsed) as HandoffSubmission;
    if (!submission || typeof submission !== 'object' || Array.isArray(submission) ||
        submission.handoff_id !== handoff.id || submission.task_id !== task.id || submission.packet_sha256 !== handoff.packet_sha256) {
      throw new Error('交接 ID、任务 ID 或任务包哈希不匹配。请导入本轮任务包要求的返回 JSON。');
    }
    const result = await request<{ handoff: HandoffRecord }>(`/handoffs/${handoff.id}/import`, 'POST', { submission, sender: sender.trim() });
    setHandoff(result.handoff); setRaw(''); setApproved(false);
    setMessage('外部申报已导入待审区，尚未写入任务工作区，也未验证其测试结论。');
  });
  const review = (decision: 'approve' | 'reject') => perform(async () => {
    if (!handoff || !reviewer.trim() || !note.trim()) throw new Error('请填写审核人与审核说明。');
    if (decision === 'approve' && !approved) throw new Error('请先核对文件并明确批准本次写入。');
    const result = await request<{ handoff: HandoffRecord }>(`/handoffs/${handoff.id}/review`, 'POST', {
      decision, reviewer: reviewer.trim(), note: note.trim(), approve_files: decision === 'approve' && approved,
    });
    setHandoff(result.handoff); setApproved(false);
    setMessage(decision === 'approve' ? '已保存审核决定；Nexus 校验并写入获批文件。外部测试结论仍属于申报。' : '已拒绝本轮结果，没有写入导入文件。');
  });
  const readFile = async (file?: File) => {
    if (!file) return;
    setError('');
    if (file.size > MAX_IMPORT) { setError('结果 JSON 超过 1.2 MB 限制。'); return; }
    try { setRaw(await file.text()); setMessage(`已读取 ${file.name}，请确认后导入待审区。`); }
    catch { setError('无法读取所选 JSON 文件。'); }
  };
  const packet = handoff?.phase !== 'prepared' ? handoff?.packet : null;
  const reports = handoff?.test_reports ?? handoff?.submission?.test_reports ?? [];
  const files = handoff?.submission?.files ?? [];
  const sample = handoff ? JSON.stringify({ handoff_id: handoff.id, task_id: task.id,
    packet_sha256: handoff.packet_sha256, summary: '填写实际执行情况（外部申报）', files: [], test_reports: [] }, null, 2) : '';
  const canPrepare = !['running', 'done'].includes(task.status) && (!handoff || ['rejected', 'cancelled'].includes(handoff.phase));
  const step = !handoff || handoff.phase === 'prepared' ? 0 : handoff.phase === 'confirmed' ? 1 : handoff.phase === 'awaiting_review' ? 2 : 3;

  return <section className="drawer-section manual-handoff" aria-label="WorkBuddy 人工交接">
    <h3><PackageOpen size={16}/>WorkBuddy 人工交接{handoff && <span className={`handoff-phase phase-${handoff.phase}`}>{phases[handoff.phase]}</span>}</h3>
    <p className="handoff-intro">你负责在 WorkBuddy 中提交任务，再带回结果。Nexus 保存交接记录，并在你审核后校验、写入文件。</p>
    <ol className="handoff-steps">{['确认任务包', '带回外部结果', '人工审查', '校验并写入'].map((label, index) => <li className={index === step ? 'current' : index < step ? 'finished' : ''} key={label}><span>{index + 1}</span>{label}</li>)}</ol>
    <div className="handoff-boundary"><ShieldCheck size={15}/><p>人工交接不代表自动连接。不会自动向 WorkBuddy 发送消息；外部测试报告属于申报，不能作为 Nexus 实际运行的测试证据。</p></div>
    {canPrepare && <button className="button primary" disabled={pending} onClick={() => void prepare()}><PackageOpen size={14}/>{handoff ? '准备新一轮交接' : '准备交接任务'}</button>}
    {handoff?.phase === 'prepared' && <div className="handoff-confirm"><p>任务包包含目标和你选定的输入文件。确认可向 WorkBuddy 分享后，再复制并自行粘贴。</p><button className="button primary" disabled={pending} onClick={() => void copyPacket()}><Copy size={14}/>确认并复制任务</button></div>}
    {handoff && <dl className="handoff-meta"><div><dt>交接记录</dt><dd>{handoff.id}</dd></div><div><dt>冻结任务快照指纹</dt><dd>{handoff.packet_sha256}</dd></div></dl>}
    {packet && <details className="handoff-packet"><summary>查看任务包与返回格式</summary><textarea aria-label="已确认任务包" readOnly rows={8} value={packet} onFocus={event => event.currentTarget.select()}/><div className="handoff-buttons"><button className="button secondary" disabled={pending} onClick={() => void copyPacket()}><Copy size={13}/>再次复制任务包</button><button className="button secondary" onClick={() => downloadText(packet, `nexus-workbuddy-${handoff!.id}.txt`)}><Download size={13}/>下载任务包</button></div></details>}
    {handoff?.phase === 'confirmed' && <form className="handoff-import" onSubmit={event => { event.preventDefault(); void importResult(); }}>
      <h4><Upload size={15}/>导入外部申报</h4><p>粘贴 WorkBuddy 返回的完整 JSON，或选择你保存的 JSON 文件。导入仅进入待审区。</p>
      <label>结果提供者<input required aria-label="结果提供者" maxLength={100} value={sender} onChange={event => setSender(event.target.value)} placeholder="本地自填记录，未验证外部身份"/></label>
      <label className="handoff-file">选择结果 JSON<input aria-label="选择结果 JSON" type="file" accept=".json,application/json" disabled={pending} onChange={event => { void readFile(event.target.files?.[0]); event.currentTarget.value = ''; }}/></label>
      <label>返回 JSON<textarea required aria-label="返回 JSON" rows={9} maxLength={MAX_IMPORT} spellCheck={false} value={raw} onChange={event => setRaw(event.target.value)} placeholder={sample}/></label>
      <details><summary>查看本轮返回 JSON 模板</summary><pre>{sample}</pre><p>files 填写拟写入文件的相对路径与完整内容；test_reports 填写外部实际申报的检查名称、状态和输出。</p></details>
      <button className="button primary" disabled={pending || !raw.trim() || !sender.trim()}><Upload size={14}/>导入并进入人工审查</button>
    </form>}
    {handoff?.submission && <div className="handoff-submission"><h4><FileCode2 size={15}/>{handoff.phase === 'approved' ? '已审核的文件内容' : `拟写入 ${files.length} 份文件 · 尚未写入`}</h4><p className="handoff-external-label">外部申报摘要</p><p>{handoff.submission.summary}</p>{files.map(file => <details key={file.path}><summary>{file.path}</summary><pre>{file.content}</pre></details>)}</div>}
    {reports.length > 0 && <div className="handoff-test-reports"><h4>外部测试申报 · Nexus 未执行</h4>{reports.map((report, index) => <article key={`${report.name}-${index}`}><div><strong>{report.name}</strong><span>{claimLabel(report.status)}</span></div><pre>{report.output}</pre></article>)}</div>}
    {handoff?.phase === 'awaiting_review' && <form className="handoff-review" onSubmit={event => { event.preventDefault(); void review('approve'); }}>
      <h4><ShieldCheck size={15}/>记录人工审核</h4><label>审核人<input required aria-label="交接审核人" maxLength={100} value={reviewer} onChange={event => setReviewer(event.target.value)}/></label><label>审核说明<textarea required aria-label="交接审核说明" rows={3} maxLength={2000} value={note} onChange={event => setNote(event.target.value)}/></label>
      <label className="check-row"><input type="checkbox" aria-label="批准交接文件写入" checked={approved} onChange={event => setApproved(event.target.checked)}/><span>已核对文件内容，批准 Nexus 校验并写入此任务的 Worktree；外部测试申报不会转为已验证测试。</span></label>
      <div className="handoff-buttons"><button className="button primary" disabled={pending || !approved || !reviewer.trim() || !note.trim()}><CheckCheck size={14}/>批准并校验写入</button><button type="button" className="button secondary" disabled={pending || !reviewer.trim() || !note.trim()} onClick={() => void review('reject')}>拒绝本轮结果</button></div><small>审核人是本地填写的审计记录，未接入身份认证。没有明确批准时不会写入。</small>
    </form>}
    {handoff?.review && <div className="handoff-review-record"><strong>{handoff.review.decision === 'approve' ? '已记录批准' : '已记录拒绝'} · {handoff.review.reviewer}</strong><p>{handoff.review.note}</p></div>}
    {handoff?.phase === 'approved' && handoff.artifacts?.map(file => <div className="artifact-record" key={file.path}><strong>{file.path}</strong><small>{file.size} bytes · Nexus 校验并写入</small><code>SHA-256 {file.sha256}</code></div>)}
    {error && <p className="handoff-error" role="alert">{error}</p>}{message && <p className="handoff-message" role="status">{message}</p>}
  </section>;
}
