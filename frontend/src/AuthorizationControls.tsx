import { useState } from 'react';
import { request } from './api';
import type { Agent } from './types';

export function SubscriptionControls({ agent, busy }: { agent: Agent; busy: boolean }) {
  const [approved, setApproved] = useState(false), [saving, setSaving] = useState(false), [message, setMessage] = useState('');
  if (agent.id !== 'gemini') return null;
  const save = async () => {
    setSaving(true); setMessage('');
    try { await request('/agents/gemini/subscription', 'POST', { approve_subscription: approved, model: 'auto' }); setApproved(false); setMessage('会员 CLI 已选用；完成官方登录后，点击真实连接测试。'); }
    catch (e) { setMessage((e as Error).message); } finally { setSaving(false); }
  };
  return <div className="adapter-config"><strong>Google 订阅 · 官方 CLI</strong><p>优先使用你订阅账号的 Sign in with Google。API / Vertex 计费回退和额外 AI credits 关闭。当前：{agent.connection_mode === 'official-rest-api' ? 'API 模式' : '会员 CLI 模式'}。</p>
    <p>在项目根目录的 PowerShell 运行，按官方页面登录后退出 CLI：</p><code className="command-block">.\scripts\gemini-login.ps1</code>
    <label className="check-row"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/><span>批准本会话使用官方 Google 登录和订阅额度，并向 Google 提交任务内容</span></label>
    <button className="button secondary" disabled={!approved || saving || busy || agent.busy} onClick={() => void save()}>启用会员 CLI 会话</button>
    {message && <p role="status">{message}</p>}<small>登录文件由专用官方 CLI 保存；Nexus 不读取、复制或导出。安装检测和勾选授权均不代表已连接。</small>
  </div>;
}

export function WorkBuddyAuthorization({ agent, busy }: { agent: Agent; busy: boolean }) {
  const [clientId, setClientId] = useState(''), [secret, setSecret] = useState(''), [approved, setApproved] = useState(false), [enabled, setEnabled] = useState(false);
  const [saving, setSaving] = useState(false), [url, setUrl] = useState(''), [message, setMessage] = useState('');
  if (agent.id !== 'workbuddy') return null;
  if (agent.status === 'manual_handoff') return <div className="adapter-config"><strong>个人账户交接流程</strong><p>在任务工作台选择 WorkBuddy，准备任务包并确认后复制。导入执行结果、文件变更和测试报告后进入人工审核；未经批准不会写入文件。企业 OAuth 接入当前停用。</p></div>;
  const begin = async () => {
    setSaving(true); setMessage(''); setUrl('');
    try {
      const result = await request<{ authorization_url: string }>('/agents/workbuddy/oauth/begin', 'POST', { client_id: clientId, client_secret: secret, application_enabled: enabled, approve_read_scope: approved });
      setSecret(''); setApproved(false); setUrl(result.authorization_url);
    } catch (e) { setMessage((e as Error).message); } finally { setSaving(false); }
  };
  return <details className="adapter-config"><summary>WorkBuddy 官方应用与只读 OAuth 授权</summary>
    <p><a href="https://open.workbuddy.cn/" target="_blank" rel="noreferrer">打开官方开发平台创建应用</a>，完成审核启用并登记下方回调。平台是否接受 loopback HTTP 需在注册时确认。</p>
    <code className="command-block">http://127.0.0.1:8765/api/agents/workbuddy/oauth/callback</code>
    <p>只申请 user.localassistant.readable；该官方 scope 也允许读取历史，本项目不调用历史接口。OAuth 成功仍不会启用任务执行。</p>
    <form autoComplete="off" onSubmit={e => { e.preventDefault(); void begin(); }}>
      <label>官方应用 Client ID<input required value={clientId} onChange={e => setClientId(e.target.value)}/></label>
      <label>应用 Client Secret<input required type="password" autoComplete="new-password" value={secret} onChange={e => setSecret(e.target.value)}/></label>
      <label className="check-row"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)}/><span>应用已经官方平台审核启用，且回调地址一致</span></label>
      <label className="check-row"><input type="checkbox" checked={approved} onChange={e => setApproved(e.target.checked)}/><span>批准申请上述只读 scope；Secret 及令牌仅在本地后端内存中保留</span></label>
      <button className="button secondary" disabled={!approved || !enabled || saving || busy}>生成官方授权入口</button>
    </form>{url && <p><a href={url} target="_blank" rel="noreferrer">前往 WorkBuddy 官方页面授权（5 分钟内有效）</a></p>}
    {message && <p role="alert">{message}</p>}<p>OAuth 状态：{agent.oauth?.oauth_authorized ? '只读授权完成；任务执行未连接' : '尚未授权'}</p>
  </details>;
}
