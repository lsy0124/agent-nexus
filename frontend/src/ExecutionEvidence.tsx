import { useEffect, useState } from 'react';
import { ExternalLink, FileCode2, Link2, Terminal } from 'lucide-react';
import { cost, percent, request } from './api';
import { SubscriptionControls, WorkBuddyAuthorization } from './AuthorizationControls';
import type { Agent, Task } from './types';

export function ExecutionEvidence({ task }: { task: Task }) {
  const attempt = task.last_attempt;
  if (!attempt) return null;
  return <div className="drawer-section"><h3><Terminal size={15}/>真实 {attempt.agent_id ?? task.agent_id} 执行记录</h3><dl className="task-meta">
    <div><dt>调用方式</dt><dd>{attempt.transport ?? '官方 CLI'} · 结构化文件桥接</dd></div>
    <div><dt>CLI 版本</dt><dd className="mono">{attempt.cli_version ?? '不适用 / 未报告'}</dd></div>
    <div><dt>本地进程 PID</dt><dd className="mono">{attempt.pid ?? '不适用 / 未启动'}</dd></div>
    <div><dt>执行返回码</dt><dd className="mono">{attempt.exit_code ?? '尚未返回'}</dd></div>
    <div><dt>官方会话 / 响应</dt><dd className="mono evidence-thread">{attempt.thread_id ?? '接口未报告'}</dd></div>
    {attempt.request_profile && <div><dt>本次请求设置</dt><dd>{attempt.request_profile.stream ? 'SSE 流式请求' : '非流式请求'} · 最大输出 {attempt.request_profile.max_output_tokens} · 请求思考 {attempt.request_profile.reasoning ?? 'provider'}</dd></div>}
    {attempt.usage.reasoning_tokens != null && <div><dt>服务商报告推理 Token</dt><dd>{attempt.usage.reasoning_tokens}</dd></div>}
    <div><dt>Token 用量</dt><dd>{attempt.usage.input_tokens ?? '—'} 输入 / {attempt.usage.output_tokens ?? '—'} 输出</dd></div>
  </dl>{attempt.request_profile?.reasoning === 'none' && (attempt.usage.reasoning_tokens ?? 0) > 0 && <p className="amber-text">请求了关闭思考，但网关仍报告推理用量；不能确认该参数被完整遵从。</p>}{attempt.error_kind && <p className="amber-text">{attempt.error_kind} · {attempt.error_message}</p>}
  {attempt.artifacts.map(file => <div className="artifact-record" key={file.path}><a download href={`/api/tasks/${task.id}/artifacts/${file.path.split('/').map(encodeURIComponent).join('/')}`}><FileCode2 size={14}/>{file.path}<ExternalLink size={12}/></a><small>{file.size} bytes · {file.generated_by} 生成 / Nexus 验证写入</small><code>SHA-256 {file.sha256}</code></div>)}
  <p className="form-note">仅落盘已验证的文本文件计划；没有通用 Shell 入口。API 返回码 0 表示协议成功结束，不代表启动了本地 CLI。</p></div>;
}

/**
 * Model and Provider switching inside an Agent card, plus the two capability
 * facts kept visibly apart:
 *   「模型 API 可调用」   — a real request to this model succeeded.
 *   「Agent 具备完整任务执行能力」 — the Agent can take a task to a deliverable.
 * A route can satisfy the first and only partially satisfy the second.
 */
export function BindingControls({ agent, busy, onChanged }: { agent: Agent; busy: boolean; onChanged: () => Promise<unknown> }) {
  const binding = agent.binding;
  const [providerId, setProviderId] = useState(binding.provider?.id ?? '');
  const [modelKey, setModelKey] = useState(binding.model?.key ?? '');
  const [ack, setAck] = useState(false);
  const [saving, setSaving] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');

  useEffect(() => { setProviderId(binding.provider?.id ?? ''); setModelKey(binding.model?.key ?? ''); setAck(false); },
    [binding.provider?.id, binding.model?.key]);

  const entry = binding.bindings.find(item => item.provider_id === providerId);
  const models = entry?.models ?? [];

  const save = async () => {
    setSaving(true); setError(''); setMessage('');
    try {
      await request(`/agents/${agent.id}/binding`, 'PUT', {
        provider_id: providerId || null, model_key: modelKey || null, acknowledge_unconfigured: ack });
      await onChanged();
      setMessage('绑定已切换；需要重新执行真实连接测试才会显示为已连接。');
    } catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  };

  return <div className="binding-controls">
    <div className="capability-pair">
      <div className={`capability-flag ${agent.model_api_callable ? 'on' : 'off'}`}>
        <i/>{agent.model_api_callable ? '模型 API 可调用' : '模型 API 未验证'}</div>
      <div className={`capability-flag ${agent.task_execution_capable ? 'on' : 'off'}`}>
        <i/>{agent.task_execution_capable ? 'Agent 具备任务执行能力' : 'Agent 任务执行能力未验证'}</div>
    </div>
    <p className="form-note">{agent.execution_capability_detail}</p>
    <dl className="binding-facts">
      <div><dt>执行运行时</dt><dd className="mono">{binding.runtime_kind} · {agent.transport}</dd></div>
      <div><dt>当前 Provider</dt><dd>{binding.provider?.name ?? '—'}<span className="subtle">{binding.provider?.kind}{binding.provider?.billing ? ` · ${binding.provider.billing}` : ''}</span></dd></div>
      <div><dt>当前模型</dt><dd className="mono">{binding.model_id ?? '不适用'}{binding.model?.context_window ? <span className="subtle">上下文 {binding.model.context_window.toLocaleString('zh-CN')}</span> : null}</dd></div>
      <div><dt>任务执行范围</dt><dd className="mono">{binding.task_execution_scope}</dd></div>
      {binding.model?.declared?.length ? <div><dt>模型声明能力</dt><dd>{binding.model.declared.join(' / ')}</dd></div> : null}
    </dl>
    <details className="adapter-config">
      <summary><Link2 size={13}/>切换模型与 Provider</summary>
      <form onSubmit={event => { event.preventDefault(); void save(); }} autoComplete="off">
        <label>Provider<select aria-label={`${agent.name} Provider`} value={providerId} onChange={e => { setProviderId(e.target.value); const next = binding.bindings.find(item => item.provider_id === e.target.value); setModelKey(next?.models[0]?.key ?? ''); setAck(false); }}>
          {binding.bindings.map(item => <option key={item.provider_id} value={item.provider_id}>{item.provider_name}{item.verified ? '' : '（未验证）'}</option>)}
        </select></label>
        <label>模型<select aria-label={`${agent.name} 模型`} value={modelKey} onChange={e => setModelKey(e.target.value)} disabled={models.length === 0}>
          {models.length === 0 ? <option value="">该 Provider 未声明可用模型</option>
            : models.map(item => <option key={item.key} value={item.key}>{item.display_name}（{item.model_id}）</option>)}
        </select></label>
        {binding.model?.notes && <p className="form-note">{binding.model.notes}</p>}
        {entry && !entry.verified && <label className="check-row"><input type="checkbox" checked={ack} onChange={e => setAck(e.target.checked)}/><span>我确认该 Provider 的接入实现尚未验证可用，切换后不会显示为已连接</span></label>}
        {entry?.note && <p className="form-note">{entry.note}</p>}
        <div className="binding-actions">
          <button className="button secondary" disabled={saving || busy || !!agent.busy}>应用绑定</button>
          <small>切换会清除该 Agent 之前的连接验证；只有真实请求通过后才会重新显示已连接。Nexus 不会用未验证的组合冒充可用。</small>
        </div>
      </form>
    </details>
    <div className="agent-usage-inline">
      <div><dt>本月调用</dt><dd className="mono">{agent.usage.requests}</dd></div>
      <div><dt>成功率</dt><dd className="mono">{percent(agent.usage.success_rate)}</dd></div>
      <div><dt>Token</dt><dd className="mono">{agent.usage.total_tokens.toLocaleString('zh-CN')}</dd></div>
      <div><dt>费用估算</dt><dd className="mono">{cost(agent.usage.estimated_cost, agent.usage.currency)}</dd></div>
      <div><dt>额度类别</dt><dd>{agent.usage.quota_label ?? '未声明'}</dd></div>
    </div>
    {agent.usage.usage_unavailable > 0 && <p className="form-note">{agent.usage.usage_unavailable} 次请求未返回 usage，只计入调用次数，未计入 Token 与费用。</p>}
    {error && <p role="alert" className="amber-text">{error}</p>}{message && <p role="status">{message}</p>}
  </div>;
}

export function AdapterControls({ agent, busy, onTest }: { agent: Agent; busy: boolean; onTest: () => void }) {
  const [key, setKey] = useState(''), [model, setModel] = useState(''), [baseUrl, setBaseUrl] = useState(''), [approved, setApproved] = useState(false);
  const [saving, setSaving] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const api = agent.id === 'gemini' || agent.id === 'mimo';
  // User-authorized third-party, metered OpenAI-compatible routes. They are
  // deliberately separate from the first-party adapters above.
  const gateway = agent.id === 'zcode-gateway' || agent.id === 'deepseek-gateway';
  const configure = async (disconnect = false) => {
    setSaving(true); setError(''); setMessage('');
    try {
      const payload = { api_key: key, model, approve_usage: approved, ...(gateway && baseUrl.trim() ? { base_url: baseUrl.trim() } : {}) };
      await request(`/agents/${agent.id}/${disconnect ? 'disconnect' : 'configure'}`, 'POST', disconnect ? undefined : payload);
      setKey(''); setApproved(false); setMessage(disconnect ? '会话授权已清除' : '配置已保存；请执行真实连接测试。');
    } catch (e) { setError((e as Error).message); } finally { setSaving(false); }
  };
  return <div className="codex-controls">
    <SubscriptionControls agent={agent} busy={busy}/>
    <WorkBuddyAuthorization agent={agent} busy={busy}/>
    {agent.cli_version && <p className="mono">{agent.cli_version}</p>}
    {agent.blockers?.map((b, i) => <p key={i} className="adapter-blocker">{typeof b === 'string' ? b : b.detail}</p>)}
    {agent.sources?.length ? <div className="adapter-sources">{agent.sources.map(s => <a key={s.url} href={s.url} target="_blank" rel="noreferrer">{s.title}<ExternalLink size={11}/></a>)}</div> : null}
    {api && <details className="adapter-config"><summary>配置官方 API 会话授权</summary><form onSubmit={e => { e.preventDefault(); void configure(); }} autoComplete="off">
      <label>官方模型 ID<input aria-label={`${agent.name} 模型 ID`} required value={model} onChange={e => setModel(e.target.value)} placeholder={agent.id === 'gemini' ? '填写已获授权的 gemini-… 模型' : '填写已获授权的 mimo-… 模型'}/></label>
      <label>用户提供的 API Key<input type="password" aria-label={`${agent.name} API Key`} required minLength={8} autoComplete="new-password" value={key} onChange={e => setKey(e.target.value)}/></label>
      <label className="check-row"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/><span>批准本会话账号调用、可能的费用和所选文件发送至官方服务</span></label>
      <button className="button secondary" disabled={saving || busy || !approved || !!agent.busy}>保存会话授权</button>
      <button type="button" className="text-button" disabled={saving || busy || !!agent.busy} onClick={() => void configure(true)}>清除会话授权</button>
      <small>密钥由后端保管，保存方式见用量与额度中的密钥后端：DPAPI 加密持久化或仅内存。不读取其他客户端登录信息，保存不调用模型。</small>
    </form></details>}
    {gateway && <details className="adapter-config"><summary>配置第三方按量计费网关路由</summary><form onSubmit={e => { e.preventDefault(); void configure(); }} autoComplete="off">
      <p className="form-note">已核实：<span className="mono">{agent.endpoint}</span> 为 OpenAI 兼容的<b>第三方按量计费</b>网关，返回过真实 <span className="mono">/models</span> 与 nonce 对话结果。它不是官方 Agent 控制接口，也不使用 ZCode 桌面订阅额度；每次调用按量计费。</p>
      <label>模型 ID<input aria-label={`${agent.name} 模型 ID`} required value={model} onChange={e => setModel(e.target.value)} placeholder={gateway && agent.id === 'zcode-gateway' ? 'glm5.3' : 'deepseek-v4.1-flash'}/></label>
      <label>用户提供的 API Key<input type="password" aria-label={`${agent.name} API Key`} required minLength={8} autoComplete="new-password" value={key} onChange={e => setKey(e.target.value)}/></label>
      <label>接口地址（可选，留空使用默认路由）<input aria-label={`${agent.name} 接口地址`} value={baseUrl} onChange={e => setBaseUrl(e.target.value)} placeholder={agent.endpoint ?? 'https://gateway.example.com/v1'}/></label>
      <label className="check-row"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/><span>批准本会话向该第三方网关提交所选文件内容，并承担按量计费费用</span></label>
      <button className="button secondary" disabled={saving || busy || !approved || !!agent.busy}>保存会话授权</button>
      <button type="button" className="text-button" disabled={saving || busy || !!agent.busy} onClick={() => void configure(true)}>清除会话授权</button>
      <small>密钥保存方式见用量与额度中的密钥后端：DPAPI 加密持久化或仅内存；不写入普通数据库、日志或页面。接口必须为公网 HTTPS。保存本身不发起模型请求。</small>
    </form></details>}
    {error && <p role="alert" className="amber-text">{error}</p>}{message && <p role="status">{message}</p>}
    {(agent.id === 'codex' || api || gateway) && <><button className="button secondary" disabled={busy || saving || !agent.ready || agent.busy} onClick={onTest}><Terminal size={14}/>{agent.busy ? `${agent.name} 执行中` : '真实连接测试'}</button><small>发起最小真实请求，消耗账户用量/额度。API 取消仅关闭本地请求，不能保证远端停止或免除费用。</small></>}
  </div>;
}
