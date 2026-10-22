import { useState } from 'react';
import { AlertTriangle, CheckCheck, ExternalLink, Gauge, History, KeyRound, Link2, Lock, RefreshCw, Route, Terminal, X } from 'lucide-react';
import { agentStatusLabel } from './model';
import { cost, percent, request } from './api';
import { AdapterControls, BindingControls } from './ExecutionEvidence';
import type { Agent, AgentRoute, LogicalAgent } from './types';

const SCOPE_LABEL: Record<string, string> = {
  'structured-files': '结构化文件方案（Nexus 校验写入）',
  'full-tools': '完整工具集（官方 CLI）',
  manual: '人工交接',
  none: '尚无可用执行接口',
};

const ROUTE_KIND_LABEL: Record<string, string> = {
  'http-adapter': '第三方 API 路由',
  'official-headless-cli': '本地官方运行时',
  'official-cli': '官方 CLI',
  'desktop-client': '桌面客户端',
  'human-handoff': '人工交接',
};

/**
 * One card per logical Agent. Its execution routes — a local CLI, a
 * third-party API — are shown as paths of this Agent, not as separate Agents.
 * Live state and historical verification are displayed as two different facts.
 */
export function LogicalAgentCard({ agent, busy, onChanged, onOpenUsage }: {
  agent: LogicalAgent; busy: boolean; onChanged: () => Promise<unknown>; onOpenUsage: () => void;
}) {
  const [reverifying, setReverifying] = useState(false);
  const [error, setError] = useState(''), [message, setMessage] = useState('');

  const reverify = async () => {
    setReverifying(true); setError(''); setMessage('');
    try {
      const result = await request<{ results: { runtime_id: string; accepted: boolean; reason?: string }[] }>(
        `/logical-agents/${agent.id}/reverify`, 'POST', {});
      await onChanged();
      const refused = result.results.filter(item => !item.accepted);
      setMessage(refused.length
        ? `已发起验证；${refused.length} 条路径未接受：${refused.map(item => item.reason).join('；')}`
        : '已发起真实连接验证；结果会写入历史记录。');
    } catch (e) { setError((e as Error).message); } finally { setReverifying(false); }
  };

  return <article className="agent-detail-card logical-card" data-agent={agent.id}>
    <div className="agent-detail-top">
      <span className={`agent-mark mark-${agent.id === 'deepseek' ? 'deepseek-gateway' : agent.id === 'zcode' ? 'zcode' : agent.id}`}>
        {({ deepseek: 'DS', zcode: 'Z', codex: 'CX', gemini: 'G', mimo: 'M', workbuddy: 'W' } as Record<string, string>)[agent.id] ?? agent.id.slice(0, 2).toUpperCase()}
      </span>
      <span className={`badge ${agent.status === 'manual_handoff' ? 'manual' : agent.status === 'connected' ? 'done' : 'disconnected'}`}>
        <i/>{agentStatusLabel(agent)}</span>
    </div>
    <h2>{agent.name}<span>{agent.selected_provider?.name ?? '未声明 Provider'} · {agent.selected_model_id ?? '未声明模型'}</span></h2>
    <p>{agent.description}</p>

    {/* Two capability facts, never merged. */}
    <div className="capability-pair">
      <div className={`capability-flag ${agent.model_api_callable ? 'on' : 'off'}`}><i/>模型 API {agent.model_api_callable ? '可调用' : '未验证'}</div>
      <div className={`capability-flag ${agent.task_execution_capable ? 'on' : 'off'}`}><i/>开发任务执行能力 {agent.task_execution_capable ? '已验证' : '未验证'}</div>
    </div>

    {/* Live state and history are separate statements. */}
    <dl className="binding-facts">
      <div><dt>当前连接状态</dt><dd>{agent.online ? `在线 · ${agent.online_runtime_ids.join('、')}`
        : agent.manual_handoff ? '人工交接（不参与自动调度）' : agentStatusLabel(agent)}<span className="subtle">仅表示本进程内的真实请求验证，未过期</span></dd></div>
      <div><dt>历史验证记录</dt><dd>{agent.verified_history
        ? <>曾经验证通过<span className="subtle">{new Date(agent.last_verified_at!).toLocaleString('zh-CN', { hour12: false })}</span></>
        : '暂无验证记录'}<span className="subtle">历史成功不代表此刻在线</span></dd></div>
      <div><dt>执行路径</dt><dd>{agent.route_count} 条<span className="subtle">{agent.routes.map(r => r.name).join(' · ')}</span></dd></div>
      <div><dt>首选路径</dt><dd className="mono">{agent.preferred_route}<span className="subtle">{agent.preferred_route_reason ?? '—'}</span></dd></div>
    </dl>

    <div className="agent-usage-inline">
      <div><dt>输入 Token</dt><dd className="mono">{agent.usage.input_tokens.toLocaleString('zh-CN')}</dd></div>
      <div><dt>输出 Token</dt><dd className="mono">{agent.usage.output_tokens.toLocaleString('zh-CN')}</dd></div>
      <div><dt>总 Token</dt><dd className="mono">{agent.usage.total_tokens.toLocaleString('zh-CN')}</dd></div>
      <div><dt>本月调用</dt><dd className="mono">{agent.usage.requests}</dd></div>
      <div><dt>成功率</dt><dd className="mono">{percent(agent.usage.success_rate)}</dd></div>
      <div><dt>费用估算</dt><dd className="mono">{cost(agent.usage.estimated_cost, agent.usage.currency)}</dd></div>
    </div>
    {agent.usage.usage_unavailable > 0 && <p className="form-note">{agent.usage.usage_unavailable} 次请求未返回 usage，只计入调用次数，未计入 Token 与费用。</p>}
    <p className="form-note">额度类别：{agent.usage.quota_label ?? '未声明'}；{agent.selected_provider?.quota.capability === 'none' ? '余额不可查询 · 可在用量与额度设置本地预算' : '余额与预算详情见用量与额度'}。</p>

    {(error || message) && <p className={error ? 'amber-text' : ''} role={error ? 'alert' : 'status'}>{error || message}</p>}

    <div className="card-actions">
      <button className="button secondary" disabled={busy || reverifying || agent.status === 'manual_handoff'} onClick={() => void reverify()}>
        <RefreshCw size={14} className={reverifying ? 'spin' : ''}/>{reverifying ? '正在验证…' : '重新验证连接'}
      </button>
      <button className="text-button" onClick={onOpenUsage}><Gauge size={13}/>使用记录</button>
    </div>

    <h3 className="route-heading"><Route size={14}/>执行路径与 Provider</h3>
    {agent.routes.map(route => <RouteBlock key={route.runtime_id} route={route} busy={busy}
      selected={route.runtime_id === agent.selected_runtime_id} onChanged={onChanged}
      onTest={async (runtimeId) => {
        setError(''); setMessage('');
        try {
          const result = await request<{ accepted: boolean; reason: string; task: { id: string } }>(
            `/agents/${runtimeId}/test`, 'POST');
          await onChanged();
          setMessage(result.accepted ? '已发起真实连接测试。' : result.reason);
        } catch (e) { setError((e as Error).message); }
      }}
      />)}
  </article>;
}

function RouteBlock({ route, busy, selected, onTest, onChanged }: {
  route: AgentRoute; busy: boolean; selected: boolean; onTest: (runtimeId: string) => void; onChanged: () => Promise<unknown>;
}) {
  const [open, setOpen] = useState(false);
  const state: Agent = route.state;
  const provider = route.providers[0];
  return <div className={`route-block ${selected ? 'selected' : ''}`}>
    <div className="route-head">
      <div>
        <strong>{route.name}</strong>
        {selected && <span className="quota-tag">首选路径</span>}
        <span className="quota-tag">{ROUTE_KIND_LABEL[route.runtime_kind] ?? route.runtime_kind}</span>
      </div>
      <span className={`badge ${state.status === 'manual_handoff' ? 'manual' : state.status === 'connected' ? 'done' : 'disconnected'}`}>
        <i/>{agentStatusLabel(state)}</span>
    </div>
    <dl className="route-facts">
      <div><dt>Provider</dt><dd>{provider ? `${provider.provider_name}（${provider.provider_kind}）` : '未声明'}</dd></div>
      <div><dt>连接方式</dt><dd className="mono">{route.transport}</dd></div>
      <div><dt>模型</dt><dd className="mono">{state.binding.model_id ?? '不适用'}</dd></div>
      <div><dt>当前执行任务</dt><dd>{state.current_task?.title ?? '无执行中任务'}</dd></div>
      <div><dt>执行范围</dt><dd>{SCOPE_LABEL[route.task_execution_scope] ?? route.task_execution_scope}</dd></div>
      <div><dt>密钥 / 凭据</dt><dd>{state.credential_configured ? '已配置（后端保管）' : '未配置'}</dd></div>
      <div><dt>真实请求验证</dt><dd>{(state.verified_at || state.last_verified_at) ? new Date((state.verified_at || state.last_verified_at)!).toLocaleString('zh-CN', { hour12: false }) : '尚未验证'}</dd></div>
      {state.last_task_error && <div><dt>最近任务异常</dt><dd className="amber-text">{state.last_task_error.state === 'BLOCKED_PERMISSION' ? '权限阻塞' : '任务失败'} · {state.last_task_error.message}<span className="subtle">任务 {state.last_task_error.task_id}；与连接状态分别记录</span></dd></div>}
      {state.cli_version ? <div><dt>CLI 版本</dt><dd className="mono">{state.cli_version}</dd></div> : null}
      {state.authenticated !== undefined && state.installed ? <div><dt>官方登录</dt><dd>{state.authenticated ? '已登录（Nexus 不管理凭证）' : '未登录'}</dd></div> : null}
    </dl>
    {/* A CLI version gate is a distinct blocker from a missing credential:
        state which gate failed and what it requires. */}
    {state.version_gate && !state.version_gate.matches && <div className="version-gate">
      <AlertTriangle size={13}/>
      <div>
        <strong>CLI 版本未通过审查闸门</strong>
        <p>已安装 <code>{state.version_gate.installed ?? '未知'}</code>，Nexus 当前只放行 <code>{state.version_gate.required}</code>。
          必要参数已核对{state.required_flags_present ? '齐全' : '不齐全'}。该闸门用于避免用未审查的参数契约执行真实开发任务；需人工确认后才能放行。</p>
        {state.binary_path ? <small className="mono">已发现 CLI：{state.binary_path}</small> : null}
      </div>
    </div>}
    <p className="form-note">{route.execution_note}</p>
    {state.verification && <p className="verification-line"><History size={12}/>历史：{state.verification.outcome === 'verified' ? '曾验证通过' : state.verification.outcome} · {new Date(state.verification.created_at).toLocaleString('zh-CN', { hour12: false })}</p>}
    {state.blockers?.map((b, i) => <p key={i} className="adapter-blocker">{typeof b === 'string' ? b : b.detail}</p>)}
    {state.sources?.length ? <div className="adapter-sources">{state.sources.map(source => <a key={source.url} href={source.url} target="_blank" rel="noreferrer">{source.title}<ExternalLink size={11}/></a>)}</div> : null}
    <div className="route-actions">
      <button className="text-button" onClick={() => setOpen(!open)}><Link2 size={13}/>{open ? '收起配置' : '配置与密钥'}</button>
      <button className="button secondary" disabled={busy || !state.ready || state.busy} onClick={() => onTest(route.runtime_id)}>
        <Terminal size={13}/>{state.busy ? '执行中' : '真实连接测试'}</button>
    </div>
    {open && <><BindingControls agent={state} busy={busy} onChanged={onChanged}/><AdapterControls agent={state} busy={busy} onTest={() => onTest(route.runtime_id)}/></>}
  </div>;
}

export { KeyRound, Lock, AlertTriangle, CheckCheck, X };
