import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, BarChart3, CheckCheck, CircleDollarSign, Coins, Database, Gauge, Layers3, Link2, Lock, RefreshCw, ShieldAlert, TrendingUp, Wallet, X } from 'lucide-react';
import { cost, percent, request, tokens } from './api';
import type { Agent, BudgetStatus, ProviderSettings, QuotaPayload, QuotaResult, UsageOverview } from './types';

const SPANS: [string, string][] = [['today', '今日'], ['week', '本周'], ['month', '本月'], ['all', '全部']];
const KIND_ORDER = ['official_account_balance', 'third_party_gateway_balance', 'nexus_budget', 'subscription_allowance'] as const;
const KIND_HINT: Record<string, string> = {
  official_account_balance: '服务商官方账户余额，仅在配置官方账号密钥后可查询。',
  third_party_gateway_balance: '第三方中转站账户余额，与官方余额互不相通。',
  nexus_budget: '你在 Nexus 中自行设置的预算上限，基于本地采集数据计算。',
  subscription_allowance: '订阅套餐额度（如 GLM Coding Plan credit），不是货币余额。',
};

function Empty2({ icon: Icon, title, description }: { icon: typeof Gauge; title: string; description: string }) {
  return <div className="empty"><span className="empty-icon"><Icon size={23}/></span><strong>{title}</strong><p>{description}</p></div>;
}

function BalanceRows({ item }: { item: QuotaResult }) {
  if (!item.queryable) {
    return <div className="quota-unavailable">
      <span className="quota-badge unavailable">余额不可查询</span>
      <p>{item.reason}</p>
      {item.status === 'credential_required' && <small>状态：需要该 Provider 的官方账号密钥（NEXUS 只使用你在本机显式提供的密钥）</small>}
      {item.status === 'unsupported' && <small>状态：服务商未提供文档化接口，Nexus 不猜测私有接口、不抓取登录态、不绕过权限校验</small>}
      {item.status === 'not_configured' && <small>状态：尚未配置</small>}
    </div>;
  }
  return <div className="quota-rows">
    {item.balances.map((row, index) => (
      <div className="quota-row" key={index}>
        {Object.entries(row).filter(([, value]) => value !== null && value !== undefined).map(([key, value]) => (
          <div key={key}><dt>{BALANCE_LABELS[key] ?? key}</dt><dd className="mono">{typeof value === 'number' ? value.toLocaleString('zh-CN', { maximumFractionDigits: 4 }) : String(value)}</dd></div>
        ))}
      </div>
    ))}
    <small className="quota-statement">
      {item.is_provider_statement ? '来源：服务商接口真实响应' : '来源：Nexus 依据官方公式与本地采集数据推算，不是服务商账单'}
      {item.note ? ` · ${item.note}` : ''}
    </small>
  </div>;
}

const BALANCE_LABELS: Record<string, string> = {
  currency: '币种', total_balance: '总余额', granted_balance: '赠送余额', topped_up_balance: '充值余额',
  window: '周期', plan: '套餐', weekly_credits: '每周额度', five_hour_credits: '5 小时额度',
  credits_used_estimate: '已用 credit（估算）', credits_remaining_estimate: '剩余 credit（估算）',
  limit: '上限', spent_estimate: '已用（估算）', remaining_estimate: '剩余（估算）', exhausted: '是否用尽',
  per_task_tokens: '单任务 Token 上限', per_task_budget: '单任务预算上限',
};

function QuotaPanel({ quota, agentNames }: { quota: QuotaPayload; agentNames: (id: string) => string }) {
  return <section className="panel quota-panel">
    <div className="section-heading"><h2><Wallet size={17}/>额度与余额</h2><span className="subtle">四类额度严格分开，不混用</span></div>
    {quota.note && <p className="form-note">{quota.note}</p>}
    <div className="quota-kinds">
      {KIND_ORDER.map(kind => {
        const items = quota.state?.kinds?.[kind] ?? [];
        const capability = quota.capability.filter(item => item.kind === kind);
        return <div className="quota-kind" key={kind}>
          <div className="quota-kind-head"><strong>{quota.kinds.find(k => k.id === kind)?.label ?? kind}</strong><span className="subtle">{KIND_HINT[kind]}</span></div>
          {items.length === 0
            /* Before a probe, state what is known from the declaration alone:
               an undocumented provider is already known to be unqueryable. */
            ? capability.length === 0
              ? <div className="quota-unavailable">
                <span className="quota-badge unavailable">余额不可查询</span>
                <p>该类别当前没有已声明的 Provider。</p>
              </div>
              : capability.map(item => <div className="quota-item" key={`${kind}-${item.provider_id}-cap`}>
                <div className="quota-item-head">
                  <span className={`quota-badge ${item.documented ? 'ok' : 'unavailable'}`}>
                    {item.documented ? '接口已文档化' : '余额不可查询'}</span>
                  <strong>{item.provider_id}</strong>
                  {!item.documented && <span className="quota-tag">无文档化接口</span>}
                </div>
                <p className="quota-capability-note">{item.reason ?? (item.endpoint ? `文档化接口：${item.endpoint}` : '依据官方文档化公式推算')}</p>
                {!item.documented && <small>Nexus 不猜测私有接口、不抓取登录 Cookie、不绕过服务商权限校验。点击上方「查询余额接口」可确认当前状态。</small>}
              </div>)
            : items.map(item => <div className="quota-item" key={`${kind}-${item.provider_id}`}>
              <div className="quota-item-head">
                <span className={`badge ${item.queryable ? 'done' : 'disconnected'}`}><i/>{item.queryable ? '可查询' : '不可查询'}</span>
                <strong>{agentNames(item.provider_id) !== item.provider_id ? agentNames(item.provider_id) : item.provider_id}</strong>
                {item.is_provider_statement && <span className="quota-tag">服务商数据</span>}
                {!item.is_provider_statement && item.queryable && <span className="quota-tag derived">Nexus 推算</span>}
              </div>
              <BalanceRows item={item}/>
            </div>)}
        </div>;
      })}
    </div>
  </section>;
}

export function UsageView({ agents, onOpenAgent }: { agents: Agent[]; onOpenAgent: (id: string) => void }) {
  const [span, setSpan] = useState('month');
  const [usage, setUsage] = useState<UsageOverview | null>(null);
  const [quota, setQuota] = useState<QuotaPayload | null>(null);
  const [budget, setBudget] = useState<BudgetStatus | null>(null);
  const [error, setError] = useState('');
  const [probing, setProbing] = useState(false);
  const [detail, setDetail] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});

  const load = useCallback(async (withProbe = false) => {
    try {
      const [u, q, b] = await Promise.all([
        request<UsageOverview>(`/usage?span=${span}`),
        request<QuotaPayload>(`/quota${withProbe ? '?probe=true' : ''}`),
        request<BudgetStatus>('/budget'),
      ]);
      setUsage(u); setQuota(q); setBudget(b); setError('');
    } catch (e) { setError((e as Error).message); }
  }, [span]);

  useEffect(() => { void load(); }, [load]);

  const providerName = (id: string) => quota?.capability.find(item => item.provider_id === id)?.provider_id ?? id;
  const totals = usage?.totals;
  const hasUsage = (totals?.requests ?? 0) > 0;

  return <>
    {error && <div className="alert error" role="alert"><AlertTriangle size={16}/><span>{error}</span><button aria-label="关闭提示" onClick={() => setError('')}><X size={15}/></button></div>}
    <div className="usage-toolbar">
      <div className="segmented">{SPANS.map(([id, label]) => <button key={id} className={span === id ? 'selected' : ''} onClick={() => setSpan(id)}>{label}</button>)}</div>
      <button className="button secondary" onClick={() => { setProbing(true); void load(true).finally(() => setProbing(false)); }} disabled={probing}>
        <RefreshCw size={14} className={probing ? 'spin' : ''}/>{probing ? '正在查询余额…' : '查询余额接口'}
      </button>
      <span className="subtle">带探测的查询会向你已配置官方密钥的 Provider 发起真实余额请求；没有文档化接口的 Provider 仍显示「余额不可查询」。</span>
    </div>

    {!usage ? <Empty2 icon={Gauge} title="正在读取用量数据" description="从本地 SQLite 读取真实调用记录。"/>
      : !hasUsage ? <section className="panel usage-empty">
        <Empty2 icon={BarChart3} title="尚无任何模型调用记录" description={`${usage.range.resolved[0]?.slice(0, 10) ?? ''} 至 ${usage.range.resolved[1]?.slice(0, 10) ?? ''} 期间没有调用。执行一次真实连接测试或任务后，Token 用量、成功率与费用估算会出现在这里。`}/>
        <p className="form-note">{usage.note}</p>
      </section> : <>
        <div className="stats-grid usage-stats">
          <div className="stat-card"><div className="stat-label">调用次数<Layers3 size={16}/></div><div className="stat-value lime">{totals!.requests}<span>次</span></div><div className="stat-detail"><i/>{totals!.succeeded} 成功 · {totals!.failed} 失败</div></div>
          <div className="stat-card"><div className="stat-label">成功率<CheckCheck size={16}/></div><div className="stat-value teal">{percent(totals!.requests ? totals!.succeeded / totals!.requests : null)}</div><div className="stat-detail"><i/>错误率 {percent(totals!.requests ? totals!.failed / totals!.requests : null)}</div></div>
          <div className="stat-card"><div className="stat-label">总 TOKEN<Coins size={16}/></div><div className="stat-value blue">{totals!.total_tokens.toLocaleString('zh-CN')}</div><div className="stat-detail"><i/>输入 {totals!.input_tokens.toLocaleString('zh-CN')} · 输出 {totals!.output_tokens.toLocaleString('zh-CN')}</div></div>
          <div className="stat-card"><div className="stat-label">费用估算<CircleDollarSign size={16}/></div><div className="stat-value amber">{cost(totals!.estimated_cost, totals!.currency)}</div><div className="stat-detail"><i/>不是服务商账单</div></div>
        </div>

        <div className="usage-note-strip">
          <Database size={15}/>
          <p>真实上报用量的请求 <strong>{totals!.usage_reported}</strong> 次；服务商未返回 usage 的请求 <strong>{totals!.usage_unavailable}</strong> 次。后者只计入调用次数，不计入 Token 与费用，也不以估算值填充。</p>
        </div>

        <div className="usage-grid">
          <section className="panel">
            <div className="section-heading"><h2><TrendingUp size={17}/>Token 消耗趋势</h2><span className="subtle">按 UTC 日聚合</span></div>
            {usage.series.length === 0 ? <Empty2 icon={TrendingUp} title="没有趋势数据" description="当前时间范围内没有调用记录。"/>
              : <UsageBars series={usage.series}/>}
          </section>

          <section className="panel">
            <div className="section-heading"><h2><Gauge size={17}/>Agent 与 Provider 分布</h2></div>
            <UsageTable title="按 Agent" rows={usage.by_agent.map(row => ({ ...row, onOpen: () => onOpenAgent(row.name) }))} agents={agents}/>
            <UsageTable title="按 Provider" rows={usage.by_provider}/>
            <UsageTable title="按模型" rows={usage.by_model}/>
          </section>
        </div>

        <section className="panel">
          <div className="section-heading"><h2><Coins size={17}/>任务成本</h2><span className="subtle">{usage.task_costs.length} 个任务</span></div>
          {usage.task_costs.length === 0 ? <Empty2 icon={Coins} title="还没有任务级用量" description="当任务产生真实调用后，这里会按任务汇总 Token 与费用估算。"/>
            : <div className="table-scroll"><table className="usage-table"><thead><tr><th>任务</th><th>Agent</th><th>模型</th><th>调用</th><th>Token</th><th>费用估算</th></tr></thead>
              <tbody>{usage.task_costs.map(row => <tr key={row.task_id}>
                <td className="mono">NX-{row.task_id.slice(0, 5).toUpperCase()}<small>{row.title ?? '（任务已不存在）'}</small></td>
                <td>{row.agents ?? '—'}</td><td className="mono">{row.models ?? '—'}</td>
                <td>{row.requests}</td><td>{row.total_tokens.toLocaleString('zh-CN')}</td>
                <td>{cost(row.estimated_cost, totals!.currency)}</td>
              </tr>)}</tbody></table></div>}
        </section>
      </>}

    {budget && <section className="panel budget-panel">
      <div className="section-heading"><h2><ShieldAlert size={17}/>预算与额度保护</h2>
        <span className={`badge ${budget.settings.stop_dispatch_when_exhausted ? 'done' : 'disconnected'}`}><i/>{budget.settings.stop_dispatch_when_exhausted ? '耗尽即停止派发' : '未启用停止派发'}</span>
      </div>
      <div className="budget-windows">{budget.windows.map(row => <div className={`budget-window ${row.exhausted ? 'exhausted' : ''}`} key={row.span}>
        <div><strong>{row.window}</strong>{row.exhausted && <span className="quota-badge unavailable">已用尽</span>}</div>
        <dl>
          <div><dt>预算上限</dt><dd className="mono">{row.budget_limit === null ? '未设置' : row.budget_limit}</dd></div>
          <div><dt>已用（估算）</dt><dd className="mono">{row.spent_estimate}</dd></div>
          <div><dt>剩余（估算）</dt><dd className="mono">{row.remaining_estimate === null ? '未设置上限' : row.remaining_estimate}</dd></div>
          <div><dt>调用次数</dt><dd className="mono">{row.call_limit === null ? `${row.calls}（无上限）` : `${row.calls} / ${row.call_limit}`}</dd></div>
          <div><dt>Token</dt><dd className="mono">{row.tokens.toLocaleString('zh-CN')}</dd></div>
        </dl>
      </div>)}</div>
      <div className="budget-form">
        {([['per_task_tokens', '单任务 Token 上限'], ['per_task_budget', '单任务预算上限'],
           ['daily_budget', '每日预算'], ['monthly_budget', '每月预算'],
           ['daily_calls', '每日调用上限'], ['monthly_calls', '每月调用上限'], ['codex_daily_calls', 'Codex 每日 Nexus 调用上限']] as [string, string][]).map(([key, label]) =>
          <label key={key}>{label}
            <input type="number" min={0} step="any" aria-label={label}
              placeholder="未设置"
              value={draft[key] ?? (budget.settings[key] === null || budget.settings[key] === undefined ? '' : String(budget.settings[key]))}
              onChange={e => setDraft({ ...draft, [key]: e.target.value })}/>
          </label>)}
        <button className="button primary" onClick={async () => {
          const payload: Record<string, number | null> = {};
          for (const [key] of [['per_task_tokens'], ['per_task_budget'], ['daily_budget'], ['monthly_budget'], ['daily_calls'], ['monthly_calls'], ['codex_daily_calls']]) {
            const raw = draft[key] ?? (budget.settings[key] === null || budget.settings[key] === undefined ? '' : String(budget.settings[key]));
            payload[key] = raw.trim() === '' ? null : Number(raw);
          }
          try { await request('/settings/budget', 'PUT', payload); setDraft({}); await load(); setError(''); }
          catch (e) { setError((e as Error).message); }
        }}>保存预算</button>
      </div>
      <p className="form-note">{budget.note} 自动回退到 Codex/GPT：{budget.gpt_auto_dispatch ? '已启用（异常）' : '禁止'}。</p>
      {budget.unenforceable.budget && <p className="amber-text">{budget.unenforceable.detail}（未公布价目表：{budget.unenforceable.providers.join('、')}）</p>}
    </section>}

    {quota && <QuotaPanel quota={quota} agentNames={providerName}/>}

    <section className="panel">
      <div className="section-heading"><h2><Database size={17}/>调用明细</h2><span className="subtle">最近 {usage?.records.length ?? 0} 条</span></div>
      {!usage || usage.records.length === 0 ? <Empty2 icon={Database} title="没有调用记录" description="真实调用发生后，每条记录的 Token 来源、模型、Provider 与状态都会出现在这里。"/>
        : <div className="table-scroll"><table className="usage-table"><thead><tr>
          <th>时间</th><th>Agent</th><th>Provider</th><th>模型</th><th>输入</th><th>输出</th><th>合计</th><th>缓存</th><th>推理</th><th>用量来源</th><th>状态</th><th>费用估算</th>
        </tr></thead><tbody>{usage.records.map(row => <tr key={row.id}>
          <td className="mono">{new Date(row.created_at).toLocaleString('zh-CN', { hour12: false })}</td>
          <td><button className="link-button" onClick={() => onOpenAgent(row.agent_id)}>{row.agent_id}</button></td>
          <td className="mono">{row.provider_id}</td>
          <td className="mono">{row.reported_model ?? row.model_key ?? row.requested_model ?? '—'}</td>
          <td>{tokens(row.input_tokens)}</td><td>{tokens(row.output_tokens)}</td><td>{tokens(row.total_tokens)}</td>
          <td>{tokens(row.cached_tokens)}</td><td>{tokens(row.reasoning_tokens)}</td>
          <td><span className={`usage-source ${row.usage_source}`}>{row.usage_source === 'provider-reported' ? '服务商上报' : row.usage_source === 'unavailable' ? '未上报' : row.usage_source}</span></td>
          <td><span className={`badge ${row.status === 'ok' ? 'done' : 'failed'}`}><i/>{row.status === 'ok' ? '成功' : (row.error_kind ?? '失败')}</span></td>
          <td>{cost(row.estimated_cost, row.currency)}</td>
        </tr>)}</tbody></table></div>}
    </section>

    <section className="panel provider-settings-panel">
      <div className="section-heading"><h2><Lock size={17}/>Provider 配置与密钥管理</h2><span className="subtle">独立的设置页</span></div>
      <ProviderKeySettings/>
    </section>

    {detail && <AgentUsageDetail agentId={detail} onClose={() => setDetail(null)}/>}
  </>;
}

function UsageBars({ series }: { series: UsageOverview['series'] }) {
  const peak = Math.max(1, ...series.map(point => point.total_tokens));
  return <div className="usage-bars">{series.map(point => <div className="usage-bar" key={point.bucket}>
    <div className="usage-bar-track"><span style={{ height: `${Math.max(2, (point.total_tokens / peak) * 100)}%` }}/></div>
    <small className="mono">{point.bucket.slice(5)}</small>
    <span className="usage-bar-value">{point.total_tokens.toLocaleString('zh-CN')}</span>
    {point.usage_unavailable > 0 && <em title={`${point.usage_unavailable} 次请求未上报用量`}>!</em>}
  </div>)}</div>;
}

function UsageTable({ title, rows }: { title: string; rows: (UsageOverview['by_agent'][number] & { onOpen?: () => void })[]; agents?: Agent[] }) {
  return <div className="usage-subtable">
    <h3>{title}</h3>
    {rows.length === 0 ? <p className="subtle">没有数据</p> : rows.map(row => <div className="usage-subrow" key={row.name}>
      <span className="usage-subrow-name">{row.onOpen ? <button className="link-button" onClick={row.onOpen}>{row.name || '（未标注）'}</button> : (row.name || '（未标注）')}</span>
      <span className="mono">{row.requests} 次</span>
      <span className="mono">{row.total_tokens.toLocaleString('zh-CN')} tokens</span>
      <span className="mono">{percent(row.requests ? row.succeeded / row.requests : null)}</span>
      {row.usage_unavailable > 0 && <span className="subtle">{row.usage_unavailable} 次未上报</span>}
    </div>)}
  </div>;
}

function ProviderKeySettings() {
  const [settings, setSettings] = useState<ProviderSettings | null>(null);
  const [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [open, setOpen] = useState<string | null>(null);
  const [key, setKey] = useState(''), [approved, setApproved] = useState(false);

  const load = async () => { try { setSettings(await request<ProviderSettings>('/settings/providers')); } catch (e) { setError((e as Error).message); } };
  useEffect(() => { void load(); }, []);

  if (!settings) return <p className="subtle">正在读取 Provider 配置…</p>;
  return <div className="provider-settings">
    <div className="vault-strip">
      <Lock size={14}/>
      <span>密钥后端：<strong>{settings.vault.backend === 'dpapi' ? 'Windows DPAPI 保护' : '仅内存'}</strong>
        {settings.vault.persists_across_restart ? ' · 重启后仍可用' : ' · 服务重启后需重新提供'}
      </span>
      {settings.vault.fallback_reason && <span className="amber-text">{settings.vault.fallback_reason}</span>}
    </div>
    {settings.vault.backend !== 'dpapi' && <button className="button secondary" onClick={async () => {
      try { await request('/settings/vault', 'PUT', { backend: 'dpapi', approve_persistence: true }); await load(); setNotice('已启用当前 Windows 用户 DPAPI 加密保存；不会调用模型。'); }
      catch (e) { setError((e as Error).message); }
    }}>启用 Windows DPAPI 加密保存</button>}
    <p className="form-note">{settings.note}</p>
    {error && <p role="alert" className="amber-text">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <div className="provider-rows">{settings.providers.map(provider => <div className="provider-row" key={provider.id}>
      <div className="provider-row-head">
        <div><strong>{provider.name}</strong><span className="mono">{provider.id}</span></div>
        <div className="provider-row-tags">
          <span className="quota-tag">{provider.kind}</span>
          <span className="quota-tag">{provider.billing}</span>
          {provider.credential.configured
            ? <span className="quota-badge ok">密钥已配置 · {provider.credential.fingerprint}{provider.credential.persisted ? ' · 已持久化' : ' · 仅内存'}</span>
            : <span className="quota-badge unavailable">未配置密钥</span>}
        </div>
      </div>
      <p className="provider-row-note">{provider.quota.capability === 'none' ? `余额查询：不支持自动采集（${provider.quota.reason}）` : `余额查询：${provider.quota.endpoint ?? '依据官方文档化公式推算'}`}</p>
      {provider.binding_agents.length > 0 && <p className="subtle">可服务的 Agent：{provider.binding_agents.join('、')}</p>}
      {provider.docs.length > 0 && <div className="adapter-sources">{provider.docs.map(doc => <a key={doc.url} href={doc.url} target="_blank" rel="noreferrer">{doc.title}</a>)}</div>}
      <div className="provider-row-actions">
        <button className="text-button" onClick={() => { setOpen(open === provider.id ? null : provider.id); setKey(''); setApproved(false); }}>{open === provider.id ? '收起' : '配置密钥'}</button>
        {provider.credential.configured && <button className="text-button" onClick={async () => {
          try { await request(`/settings/providers/${provider.id}/key`, 'DELETE'); await load(); setNotice(`${provider.name} 密钥已清除`); setError(''); }
          catch (e) { setError((e as Error).message); }
        }}>清除密钥</button>}
      </div>
      {open === provider.id && <form className="provider-key-form" autoComplete="off" onSubmit={async event => {
        event.preventDefault();
        try {
          await request(`/settings/providers/${provider.id}/key`, 'PUT', { api_key: key, approve_usage: approved });
          setKey(''); setApproved(false); setOpen(null); await load();
          setNotice('密钥已保存在后端；页面与日志不会显示密钥内容'); setError('');
        } catch (e) { setError((e as Error).message); }
      }}>
        <label>API Key<input type="password" required minLength={8} autoComplete="new-password" aria-label={`${provider.name} API Key`} value={key} onChange={e => setKey(e.target.value)}/></label>
        <label className="check-row"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/><span>批准该 Provider 的账号调用与可能的费用</span></label>
        <div><button className="button secondary" disabled={!approved || key.length < 8}>保存到后端</button></div>
        <small>密钥只保存于本机后端；接口与页面只返回指纹与长度，绝不返回密钥。日志不记录 Authorization。</small>
      </form>}
    </div>)}</div>
  </div>;
}

export function AgentUsageDetail({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const [data, setData] = useState<null | { summary: Agent['usage']; totals: UsageOverview['totals']; records: UsageOverview['records']; verifications: { id: number; kind: string; outcome: string; detail: string; created_at: string }[] }>(null);
  const [error, setError] = useState('');
  useEffect(() => { request<never>(`/usage/agents/${agentId}?span=month`).then(setData).catch(e => setError((e as Error).message)); }, [agentId]);
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="modal" role="dialog" aria-modal="true" aria-label={`${agentId} 使用记录`}>
      <div className="modal-header"><div><span className="eyebrow">AGENT USAGE</span><h2>{agentId} · 使用记录</h2></div><button aria-label="关闭使用记录" onClick={onClose}><X size={20}/></button></div>
      {error && <p className="amber-text">{error}</p>}
      {!data ? <p className="subtle">正在读取…</p> : <>
        <div className="detail-stats">
          <div><dt>本月调用</dt><dd className="mono">{data.summary.requests}</dd></div>
          <div><dt>成功率</dt><dd className="mono">{percent(data.summary.success_rate)}</dd></div>
          <div><dt>总 Token</dt><dd className="mono">{data.summary.total_tokens.toLocaleString('zh-CN')}</dd></div>
          <div><dt>未上报用量</dt><dd className="mono">{data.summary.usage_unavailable}</dd></div>
          <div><dt>费用估算</dt><dd className="mono">{cost(data.summary.estimated_cost, data.summary.currency)}</dd></div>
          <div><dt>额度类别</dt><dd>{data.summary.quota_label ?? '未声明'}</dd></div>
        </div>
        <h3>能力验证记录</h3>
        {data.verifications.length === 0 ? <p className="subtle">尚无通过真实请求产生的验证记录。</p>
          : data.verifications.map(item => <div className="verification-row" key={item.id}>
            <span className={`badge ${item.outcome === 'verified' ? 'done' : 'failed'}`}><i/>{item.kind}</span>
            <span>{item.detail}</span>
            <time className="mono">{new Date(item.created_at).toLocaleString('zh-CN', { hour12: false })}</time>
          </div>)}
        <h3>调用明细</h3>
        {data.records.length === 0 ? <p className="subtle">本月没有调用记录。</p>
          : <div className="table-scroll"><table className="usage-table"><thead><tr><th>时间</th><th>模型</th><th>Token</th><th>来源</th><th>状态</th><th>费用估算</th></tr></thead>
            <tbody>{data.records.map(row => <tr key={row.id}><td className="mono">{new Date(row.created_at).toLocaleString('zh-CN', { hour12: false })}</td>
              <td className="mono">{row.reported_model ?? row.model_key ?? '—'}</td><td>{tokens(row.total_tokens)}</td>
              <td><span className={`usage-source ${row.usage_source}`}>{row.usage_source === 'provider-reported' ? '服务商上报' : '未上报'}</span></td>
              <td>{row.status === 'ok' ? '成功' : (row.error_kind ?? '失败')}</td><td>{cost(row.estimated_cost, row.currency)}</td></tr>)}</tbody></table></div>}
      </>}
    </section>
  </div>;
}

export { Link2, Lock };
