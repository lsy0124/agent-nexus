import { chromium } from '@playwright/test';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('../../', import.meta.url));
const outputDir = path.resolve(process.env.NEXUS_TEST_OUTPUT_DIR ?? path.join(root, 'work/e2e-evidence', String(Date.now())));
const outputRelative = path.relative(path.join(root, 'work'), outputDir);
if (!outputRelative || outputRelative.startsWith('..') || path.isAbsolute(outputRelative)) {
  throw new Error('Browser evidence must stay in a dedicated directory under this project work/.');
}
const browserTemp = path.join(root, 'work/browser-temp');
await mkdir(browserTemp, { recursive: true });
process.env.TEMP = process.env.TMP = browserTemp;
const browserPaths = [process.env.NEXUS_BROWSER_PATH,
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1234/chrome-win64/chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1223/chrome-win64/chrome.exe'),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean);
const executablePath = browserPaths.find(p => existsSync(p));
if (!executablePath) throw new Error('没有可用浏览器。设置 NEXUS_BROWSER_PATH 为已有 Chromium/Edge 可执行文件；测试不会安装浏览器。');
const url = process.env.NEXUS_URL ?? 'http://127.0.0.1:8765';
// These checks create, execute and review real tasks. Pointed at the normal
// console they would write into the user's own database, so running without an
// explicit isolated target is refused. `npm run test:e2e` starts a throwaway
// instance and sets NEXUS_URL for us.
if (!process.env.NEXUS_URL && process.env.NEXUS_ALLOW_SHARED_DB !== '1') {
  throw new Error('拒绝运行：未设置 NEXUS_URL，这些检查会写入真实任务数据库。请使用 `npm run test:e2e`（隔离实例），或在确认隔离后设置 NEXUS_URL。');
}
const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const failures = [], checks = [], network = [], expectedRefusals = [];
// A few checks deliberately provoke a refusal to prove the backend rejects it.
// Those expected 4xx responses (and the browser console noise they cause) are
// recorded separately from genuine failures.
let expectingRefusal = 0;
page.on('pageerror', e => failures.push(e.message));
page.on('console', e => {
  if (e.type() !== 'error') return;
  if (expectingRefusal > 0 && /Failed to load resource/.test(e.text())) { expectedRefusals.push(e.text()); return; }
  failures.push(`${e.text()} :: ${JSON.stringify(e.location?.() ?? {})}`);
});
page.on('response', r => {
  if (r.status() < 400 || r.url().endsWith('/favicon.ico')) return;
  if (expectingRefusal > 0) { expectedRefusals.push(`${r.status()} ${r.url()}`); return; }
  network.push(`${r.status()} ${r.url()}`);
});
const check = async (label, fn) => { await fn(); checks.push(label); console.log(`PASS ${label}`); };
/** Run deliberately-refused requests without polluting the failure lists.
 *  The flag is held briefly after the call because a console error can be
 *  delivered a tick later than the response. */
const withExpectedRefusal = async fn => {
  expectingRefusal += 1;
  try { return await fn(); }
  finally { await new Promise(resolve => setTimeout(resolve, 250)); expectingRefusal -= 1; }
};
const api = async (endpoint, body) => page.evaluate(async ({ endpoint, body }) => {
  const { token } = await (await fetch('/api/session')).json();
  const response = await fetch('/api' + endpoint, { method: body === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json', 'X-Nexus-Token': token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}, { endpoint, body });
const waitState = async (predicate) => {
  for (let i = 0; i < 100; i++) { const state = await api('/state'); if (predicate(state)) return state; await new Promise(resolve => setTimeout(resolve, 100)); }
  throw new Error('状态等待超时');
};
const closeDrawer = () => page.getByRole('button', { name: '关闭任务详情', exact: true }).click();
await mkdir(outputDir, { recursive: true });
try {
  await page.goto(url);
  await check('生产页面与 WebSocket 真实连接', async () => {
    await page.getByText('实时已连接', { exact: true }).waitFor();
    await page.getByRole('heading', { name: /协作总览/ }).waitFor();
    const state = await api('/state');
    assert.equal(state.agents.filter(a => a.status === 'connected').length, 0);
  });
  await check('对话框支持键盘退出和焦点恢复', async () => {
    const trigger = page.getByRole('button', { name: '创建任务', exact: true });
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: '创建新任务' });
    await dialog.waitFor();
    assert.ok(await dialog.getByLabel('任务标题', { exact: true }).evaluate(element => element === document.activeElement));
    await dialog.getByLabel('执行端', { exact: true }).selectOption('auto');
    await dialog.getByRole('note').filter({ hasText: '按量计费 API 需要在执行端中明确选择' }).waitFor();
    await dialog.getByLabel('执行端', { exact: true }).selectOption('mimo');
    const approval = dialog.getByRole('checkbox', { name: '批准本任务当前 Provider 的按量计费费用' });
    await approval.check();
    await dialog.getByLabel('执行端', { exact: true }).selectOption('zcode-gateway');
    assert.equal(await approval.isChecked(), false);
    await page.keyboard.press('Shift+Tab');
    assert.ok(await dialog.evaluate(element => element.contains(document.activeElement)));
    await page.keyboard.press('Escape');
    assert.equal(await dialog.count(), 0);
    assert.ok(await trigger.evaluate(element => element === document.activeElement));
  });
  await check('通过界面创建、执行真实本地环境检查', async () => {
    await page.getByRole('button', { name: '运行本地检查', exact: true }).click();
    await page.getByRole('dialog', { name: '任务详情' }).waitFor();
    await page.getByText('Python、Git、Node.js 的真实版本命令全部通过。未调用任何 AI Agent。', { exact: true }).first().waitFor();
    assert.match(await page.locator('.drawer-logs').innerText(), /git version/);
    assert.match(await page.locator('.drawer-logs').innerText(), /Python 3\./);
    await closeDrawer();
  });
  let blockedTask;
  await check('通过表单创建 Codex 任务并明确阻止未连接执行', async () => {
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '创建新任务' });
    await dialog.getByLabel('任务标题', { exact: true }).fill('接入 Codex 官方执行桥接');
    await dialog.getByLabel('目标与验收标准').fill('下一阶段通过受支持的官方 CLI 执行真实最小请求，完成权限、流式事件与取消验证。');
    await dialog.getByLabel('所需能力', { exact: false }).fill('');
    // "提交后执行" defaults to on whenever the Codex CLI is ready, which is an
    // environment fact rather than a UI fact. Clear it so this check is about
    // creating a task that dispatch must refuse, not about the default toggle.
    const autoRun = dialog.getByRole('checkbox', { name: '提交后执行 Agent', exact: true });
    if (await autoRun.isChecked()) await autoRun.uncheck();
    await dialog.getByRole('button', { name: '创建任务', exact: true }).click();
    await page.getByRole('button', { name: '执行 Codex', exact: true }).click();
    await page.getByRole('dialog', { name: '任务详情' }).getByText(/Adapter 尚未连接/).first().waitFor();
    const state = await api('/state');
    blockedTask = state.tasks.find(t => t.title === '接入 Codex 官方执行桥接');
    assert.equal(blockedTask.status, 'blocked');
    await closeDrawer();
    await page.getByRole('button', { name: '关闭提示', exact: true }).click();
  });
  await check('真实任务依赖、能力缺口与依赖图', async () => {
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '创建新任务' });
    await dialog.getByLabel('任务标题', { exact: true }).fill('验证 Agent 浏览器验收能力');
    await dialog.getByLabel('目标与验收标准').fill('等待 Codex 接入后，在任务 Worktree 内验证浏览器测试能力。当前不执行。');
    await dialog.getByLabel('所需能力', { exact: false }).fill('browser.test');
    await dialog.getByText('接入 Codex 官方执行桥接', { exact: true }).click();
    await dialog.getByRole('button', { name: '创建任务', exact: true }).click();
    await page.getByRole('dialog', { name: '任务详情' }).getByText('缺少可执行能力', { exact: true }).waitFor();
    await closeDrawer();
    await page.getByRole('button', { name: '依赖图', exact: true }).click();
    await page.getByRole('img', { name: '任务依赖图' }).waitFor();
    assert.ok(await page.locator('.dependency-graph path[marker-end]').count() >= 1);
    await page.getByRole('button', { name: '看板', exact: true }).click();
    const state = await api('/state');
    const dependent = state.tasks.find(t => t.title === '验证 Agent 浏览器验收能力');
    assert.ok(dependent.dependencies.includes(blockedTask.id));
    assert.deepEqual(dependent.capability_gaps, ['browser.test']);
  });
  await check('筛选、导航与全部 Adapter 真实状态', async () => {
    await page.getByLabel('搜索任务').fill('接入 Codex');
    assert.equal(await page.locator('.task-card').count(), 1);
    await page.getByLabel('搜索任务').fill('');
    await page.locator('nav').getByRole('button', { name: 'Agent 网络', exact: true }).click();
    // One logical Agent per card, with independently testable runtime routes.
    const reported = await api('/agents');
    const logical = await api('/logical-agents');
    assert.equal(await page.locator('.agent-detail-card').count(), logical.agents.length);
    assert.equal(logical.agents.length, 6);
    assert.equal(await page.locator('.route-block').count(), reported.length);
    for (const [id, gateway] of [['zcode', 'zcode-gateway'], ['deepseek', 'deepseek-gateway']]) {
      const card = page.locator(`.logical-card[data-agent="${id}"]`);
      assert.equal(await card.count(), 1);
      assert.ok(logical.agents.find(a => a.id === id).routes.some(r => r.runtime_id === gateway));
      assert.equal(await card.locator('.route-block').count(), 2);
    }
    // Every non-manual unit must read as unverified: this QA instance has no live
    // connection, and the console may not imply one. The single manual-handoff
    // unit reports its handoff route instead of a request verification.
    const manual = reported.filter(agent => agent.status === 'manual_handoff');
    assert.equal(manual.length, 1);
    assert.equal(await page.locator('.route-block').getByText('尚未验证', { exact: true }).count(), reported.length);
    assert.equal(reported.filter(agent => agent.status === 'connected').length, 0);
  });
  await check('调度策略可见：GPT 自动派发关闭，按量计费须显式授权', async () => {
    const policy = await api('/policy');
    assert.equal(policy.gpt_auto_dispatch, true);
    assert.equal(policy.paid_api_auto_switch, false);
    const codex = policy.agents.find(agent => agent.id === 'codex');
    assert.equal(codex.auto_dispatch, false);
    assert.equal(codex.explicit_allowed, true);
    for (const id of ['deepseek-gateway', 'zcode-gateway', 'mimo']) {
      const row = policy.agents.find(agent => agent.id === id);
      assert.equal(row.billing, 'metered-api');
      assert.equal(row.auto_dispatch, false);
      assert.ok(row.auto_blockers.some(item => item.code === 'billing_not_auto_approved'));
    }
    assert.ok(policy.priority.indexOf('deepseek-gateway') < policy.priority.indexOf('codex'));
  });
  await check('第三方网关与官方 Adapter 是两个独立单元', async () => {
    const agents = await api('/agents');
    const byId = Object.fromEntries(agents.map(agent => [agent.id, agent]));
    for (const id of ['zcode-gateway', 'deepseek-gateway']) {
      assert.equal(byId[id].billing, 'metered-api');
      assert.match(byId[id].transport, /user-authorized/);
      assert.equal(byId[id].credential_configured, false);
      assert.ok(byId[id].limitations.some(text => text.includes('按量计费')));
    }
    // The official ZCode desktop and DSH units stay distinct and unconnected.
    assert.equal(byId.zcode.transport, 'official-cli-pending');
    assert.notEqual(byId.zcode.id, byId['zcode-gateway'].id);
    assert.equal(byId.dsh.credential_configured, false);
    assert.ok(byId.dsh.restricted_runtime.ready);
    assert.equal(byId.dsh.restricted_runtime.home_scope, 'project-local');
  });
  await check('Adapter 官方来源、阻塞项和未授权 API 表单', async () => {
    assert.ok(await page.locator('.adapter-sources a').count() >= 12);
    const gemini = page.locator('.agent-detail-card').filter({ has: page.getByRole('heading', { name: /^Gemini/ }) });
    await gemini.getByRole('button', { name: '配置与密钥', exact: true }).click();
    await gemini.getByText('配置官方 API 会话授权', { exact: true }).click();
    const password = gemini.getByLabel('Gemini API Key', { exact: true });
    assert.equal(await password.getAttribute('type'), 'password');
    assert.equal(await gemini.getByRole('button', { name: '保存会话授权', exact: true }).isEnabled(), false);
    for (const checkbox of await gemini.getByRole('checkbox').all()) assert.equal(await checkbox.isChecked(), false);
    assert.ok(await gemini.getByText('Google 订阅 · 官方 CLI', { exact: true }).isVisible());
    const buddy = page.locator('.agent-detail-card').filter({ has: page.getByRole('heading', { name: /^WorkBuddy/ }) });
    await buddy.getByText('人工交接', { exact: true }).first().waitFor();
    await buddy.getByRole('button', { name: '配置与密钥', exact: true }).click();
    assert.equal(await buddy.getByLabel('应用 Client Secret', { exact: true }).count(), 0);
    assert.equal(await buddy.getByText('WorkBuddy 官方应用与只读 OAuth 授权', { exact: true }).count(), 0);
    assert.ok(await buddy.getByText('个人账户交接流程', { exact: true }).isVisible());
  });
  await check('自动编排阻止未验证连接，不回退到未授权 Agent', async () => {
    const task = await api('/tasks', { title: 'LOCAL BROWSER ROUTING CHECK', agent_id: 'auto', auto_run: true });
    assert.equal(task.status, 'blocked');
    assert.equal(task.last_attempt, null);
    // The refusal must name the real reason per agent instead of implying a
    // connection exists: policy-disabled and unverified agents are both listed.
    assert.match(task.result, /没有可自动派发的 Agent/);
    assert.match(task.result, /codex：调度策略停用自动派发/);
    assert.match(task.result, /mimo：调度策略停用自动派发/);
    assert.match(task.result, /dsh：调度策略停用自动派发/);
  });
  await check('阻塞的自动任务可直接打开执行确认，取消不提交调用', async () => {
    await page.locator('nav').getByRole('button', { name: '任务工作台', exact: true }).click();
    await page.getByLabel('搜索任务').fill('LOCAL BROWSER ROUTING CHECK');
    await page.locator('.task-card').first().click();
    await page.getByRole('button', { name: '单 Agent 执行', exact: true }).click();
    const confirm = page.getByRole('dialog', { name: '确认执行路径' });
    await confirm.waitFor();
    assert.equal(await confirm.getByRole('button', { name: '确认并执行', exact: true }).isEnabled(), false);
    await confirm.getByText('没有配置就绪且能力匹配的执行路径。', { exact: false }).waitFor();
    await confirm.getByRole('button', { name: '返回任务', exact: true }).click();
    await page.getByRole('dialog', { name: '任务详情' }).waitFor();
    const state = await api('/state');
    const task = state.tasks.find(t => t.title === 'LOCAL BROWSER ROUTING CHECK');
    assert.equal(task.agent_id, 'auto'); assert.equal(task.last_attempt, null);
    await closeDrawer();
    await page.getByLabel('搜索任务').fill('');
  });
  await check('新建自动任务先确认路径，不产生无意义的派发失败', async () => {
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    const form = page.getByRole('dialog', { name: '创建新任务' });
    await form.getByLabel('任务标题', { exact: true }).fill('LOCAL AUTO CONFIRM UI FIXTURE');
    await form.getByLabel('执行端', { exact: true }).selectOption('auto');
    await form.getByLabel('提交后进入协作拆分', { exact: true }).check();
    await form.getByRole('button', { name: '创建并拆分', exact: true }).click();
    await page.getByRole('dialog', { name: '多 Agent 协作' }).waitFor();
    const task = (await api('/state')).tasks.find(t => t.title === 'LOCAL AUTO CONFIRM UI FIXTURE');
    assert.equal(task.status, 'backlog'); assert.equal(task.last_attempt, null);
    await page.getByRole('button', { name: '关闭协作方案', exact: true }).click();
  });
  await check('协作拆分草案可编辑依赖和输出范围，未批准不创建执行子任务', async () => {
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    const form = page.getByRole('dialog', { name: '创建新任务' });
    await form.getByLabel('任务标题', { exact: true }).fill('LOCAL COLLABORATION DRAFT UI FIXTURE');
    await form.getByLabel('执行端', { exact: true }).selectOption('auto');
    await form.getByLabel('提交后进入协作拆分', { exact: true }).check();
    await form.getByRole('button', { name: '创建并拆分', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '多 Agent 协作' });
    await dialog.getByRole('region', { name: '项目 AI 规划' }).waitFor();
    assert.equal(await dialog.getByRole('button', { name: '准备 AI 规划任务', exact: true }).isEnabled(), false);
    await dialog.getByRole('button', { name: '生成拆分草案', exact: true }).click();
    await dialog.getByLabel('frontend 目标', { exact: true }).fill('LOCAL FIXTURE: editable frontend deliverable');
    await dialog.getByLabel('frontend 输出目录', { exact: true }).fill('fixture-ui/frontend');
    await dialog.getByLabel('frontend 交付文件', { exact: true }).fill('fixture-ui/frontend/view.tsx');
    await dialog.getByLabel('frontend 输入文件', { exact: true }).fill('');
    assert.equal(await dialog.getByRole('button', { name: '批准并启动协作', exact: true }).isEnabled(), false);
    assert.equal(await dialog.getByLabel('批准协作方案', { exact: true }).isEnabled(), false);
    assert.equal(await dialog.getByLabel('integration 依赖', { exact: true }).inputValue(), 'frontend,backend');
    const state = await api('/state');
    const task = state.tasks.find(t => t.title === 'LOCAL COLLABORATION DRAFT UI FIXTURE');
    const plan = await api(`/tasks/${task.id}/collaboration`);
    assert.equal(plan.state, 'draft'); assert.equal(plan.planner, 'local-template');
    assert.ok(plan.nodes.every(n => !n.task_id && !n.task));
    assert.equal(await dialog.getByLabel('frontend 交付文件', { exact: true }).inputValue(), 'fixture-ui/frontend/view.tsx');
    assert.equal(await dialog.getByLabel('frontend 输入文件', { exact: true }).inputValue(), '');
    await dialog.getByRole('button', { name: '关闭协作方案', exact: true }).click();
  });
  await check('本地 UI fixture：费用批准只提交所选路径，切换路径撤销批准', async () => {
    // Browser-only readiness fixture, never connectivity evidence. The run
    // request is intercepted before the backend so no model can be invoked.
    const task = await api('/tasks', { title: 'LOCAL PAID CONFIRM UI FIXTURE', agent_id: 'auto' });
    let submitted = null;
    const stateFixture = async route => {
      const response = await route.fetch();
      const state = await response.json();
      for (const a of state.agents.filter(a => ['mimo', 'zcode-gateway'].includes(a.id))) {
        Object.assign(a, { ready: true, busy: false, status: 'disconnected', billing: 'metered-api', capabilities: [], supported_capabilities: ['file.generate'] });
      }
      await route.fulfill({ response, json: state });
    };
    const runUrl = `**/api/tasks/${task.id}/run`;
    const runFixture = async route => { submitted = route.request().postDataJSON(); await route.fulfill({ json: { accepted: false, reason: 'LOCAL UI FIXTURE: no model request submitted' } }); };
    await page.route('**/api/state', stateFixture);
    await page.route(runUrl, runFixture);
    try {
      await page.reload();
      await page.locator('nav').getByRole('button', { name: '任务工作台', exact: true }).click();
      await page.getByLabel('搜索任务').fill(task.title);
      await page.locator('.task-card').first().click();
      await page.getByRole('button', { name: '单 Agent 执行', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '确认执行路径' });
      const button = dialog.getByRole('button', { name: '确认并执行', exact: true });
      await dialog.getByLabel('本次执行端', { exact: true }).selectOption('mimo');
      assert.equal(await button.isEnabled(), false);
      await dialog.getByRole('checkbox').check();
      assert.equal(await button.isEnabled(), true);
      await dialog.getByLabel('本次执行端', { exact: true }).selectOption('zcode-gateway');
      assert.equal(await dialog.getByRole('checkbox').isChecked(), false);
      assert.equal(await button.isEnabled(), false);
      await dialog.getByRole('checkbox').check();
      await button.click();
      await page.getByText('LOCAL UI FIXTURE: no model request submitted', { exact: true }).waitFor();
      // The client attaches a one-shot execution key so a browser retry cannot
      // resubmit the same intent; only the route and approval are asserted.
      assert.equal(submitted.agent_id, 'zcode-gateway');
      assert.equal(submitted.cost_approved, true);
      assert.match(submitted.execution_key, /^[A-Za-z0-9_-]{16,80}$/);
      assert.equal((await api(`/tasks/${task.id}/attempts`)).length, 0);
    } finally {
      await page.unroute('**/api/state', stateFixture);
      await page.unroute(runUrl, runFixture);
      await page.goto(url);
      await page.getByText('实时已连接', { exact: true }).waitFor();
    }
  });
  await check('Worktree 审查：真实差异、只读预检、人工拒绝不写入', async () => {
    // The review gate belongs to agent work, so the task is an agent task; it is
    // never dispatched, so no model is contacted.
    const task = await api('/tasks', { title: 'LOCAL WORKTREE REVIEW CHECK', agent_id: 'codex', kind: 'agent' });
    await api(`/tasks/${task.id}/worktree`, {});
    const review = await api(`/tasks/${task.id}/review`);
    assert.equal(review.status.branch, `nexus/task-${task.id}`);
    assert.equal(review.status.file_count, 0);
    assert.equal(review.status.has_changes, false);
    assert.equal(review.status.merge_identity.configured, true);
    // The read-only preflight must run Git and report a real verdict.
    const preflight = await api(`/tasks/${task.id}/review/preflight`, {}, 'POST');
    assert.equal(preflight.status, 'clean');

    await page.goto(url);
    await page.getByText('实时已连接', { exact: true }).waitFor();
    await page.locator('nav').getByRole('button', { name: '任务工作台', exact: true }).click();
    await page.getByLabel('搜索任务').fill('LOCAL WORKTREE REVIEW CHECK');
    await page.locator('.task-card').first().click();
    const drawer = page.getByRole('dialog', { name: '任务详情' });
    // The panel loads its real review status asynchronously, so the assertions
    // wait for the fetched branch name instead of racing the request.
    const branch = drawer.getByText(`nexus/task-${task.id}`, { exact: true });
    await branch.waitFor();
    assert.ok(await branch.isVisible());
    assert.ok(await drawer.getByRole('button', { name: '加载真实 Diff', exact: true }).isVisible());
    const approve = drawer.getByRole('button', { name: '批准并合并到当前分支', exact: true });
    // Merging is impossible before an explicit human decision, a clean preflight
    // and an actual change on the branch.
    assert.equal(await approve.isEnabled(), false);
    assert.ok(await drawer.getByText('该分支尚无提交：还没有可审查或合并的内容。', { exact: true }).isVisible());
    await drawer.getByLabel('审核人').fill('Local Browser Reviewer');
    await drawer.getByLabel('审核说明').fill('LOCAL FIXTURE: reviewed the diff, no files changed');
    await drawer.getByRole('button', { name: '合并预检（只读）', exact: true }).click();
    await drawer.getByText('合并预检通过：无冲突。', { exact: true }).waitFor();
    // Still disabled: an empty branch must never produce a merge commit.
    assert.equal(await approve.isEnabled(), false);
    await drawer.getByRole('button', { name: '拒绝', exact: true }).click();
    await drawer.getByText('已记录拒绝决定，未修改任何文件。', { exact: true }).waitFor();
    const after = await api(`/tasks/${task.id}/review`);
    // A rejected review writes nothing: the branch still has no commit of its
    // own and the project HEAD is unchanged.
    assert.equal(after.status.has_changes, false);
    assert.equal(after.status.already_merged, false);
    assert.equal(after.status.task_head, after.status.project.head);
    await drawer.getByRole('button', { name: '关闭任务详情', exact: true }).click();
  });
  await check('WorkBuddy 人工交接：明确复制、申报待审、批准后真实写入', async () => {
    // Exercise the user-triggered copy call without reading or overwriting the user's OS clipboard.
    await page.evaluate(() => {
      window.__manualClipboardFixture = null;
      Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
        writeText: async value => { window.__manualClipboardFixture = value; },
      } });
    });
    await page.getByRole('button', { name: '创建任务', exact: true }).click();
    const form = page.getByRole('dialog', { name: '创建新任务' });
    await form.getByLabel('任务标题', { exact: true }).fill('LOCAL MANUAL HANDOFF QA — not WorkBuddy execution');
    await form.getByLabel('目标与验收标准').fill('LOCAL TEST FIXTURE: exercise import and review in an isolated database. No real human approval or external agent execution.');
    await form.getByLabel('执行端', { exact: true }).selectOption('workbuddy');
    await form.getByLabel('提交后准备人工交接', { exact: true }).check();
    await form.getByRole('button', { name: '创建并准备交接', exact: true }).click();
    const manual = page.getByRole('region', { name: 'WorkBuddy 人工交接' });
    await manual.getByText('待你确认', { exact: true }).waitFor();
    assert.equal(await page.evaluate(() => window.__manualClipboardFixture), null);
    assert.equal(await page.getByRole('button', { name: '执行 WorkBuddy', exact: true }).count(), 0);
    let state = await api('/state');
    let task = state.tasks.find(value => value.title.startsWith('LOCAL MANUAL HANDOFF QA'));
    assert.equal(task.status, 'blocked'); assert.equal(task.last_attempt, null);
    assert.equal(task.latest_handoff.phase, 'prepared');
    assert.equal(state.agents.find(value => value.id === 'workbuddy').status, 'manual_handoff');
    assert.equal(state.agents.find(value => value.id === 'workbuddy').ready, false);
    await manual.getByRole('button', { name: '确认并复制任务', exact: true }).click();
    await manual.getByText('等待外部结果', { exact: true }).waitFor();
    const record = (await api(`/handoffs/${task.latest_handoff.id}`)).handoff;
    const copied = await page.evaluate(() => window.__manualClipboardFixture);
    assert.equal(typeof copied, 'string'); assert.ok(copied.includes(record.id));
    assert.ok(copied.includes(record.packet_sha256));
    await manual.getByLabel('结果提供者', { exact: true }).fill('LOCAL BROWSER FIXTURE — not authenticated WorkBuddy');
    const submission = { handoff_id: record.id, task_id: task.id, packet_sha256: record.packet_sha256,
      summary: 'LOCAL TEST FIXTURE ONLY; external execution was not performed.',
      files: [{ path: 'manual-qa.txt', content: 'LOCAL MANUAL QA FILE' }],
      test_reports: [{ name: 'LOCAL FIXTURE simulated pass claim', status: 'passed', output: 'LOCAL FIXTURE claim only; no real external test.' },
        { name: 'Actual external execution', status: 'not_run', output: 'No external Agent or real model was invoked.' }] };
    await manual.getByLabel('返回 JSON', { exact: true }).fill(JSON.stringify(submission));
    await manual.getByRole('button', { name: '导入并进入人工审查', exact: true }).click();
    await manual.getByText('待人工审查', { exact: true }).waitFor();
    await manual.getByText('外部测试申报 · Nexus 未执行', { exact: true }).waitFor();
    assert.ok(await manual.getByText('外部申报：通过', { exact: true }).isVisible());
    assert.ok(await manual.getByText('拟写入 1 份文件 · 尚未写入', { exact: true }).isVisible());
    state = await api('/state'); task = state.tasks.find(value => value.id === task.id);
    assert.equal(task.status, 'blocked'); assert.equal(task.last_attempt, null);
    assert.deepEqual(task.latest_handoff.artifacts, []);
    const relative = path.relative(path.join(root, 'work/browser-qa'), path.resolve(task.worktree));
    assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
    assert.equal(existsSync(path.join(task.worktree, 'manual-qa.txt')), false);
    assert.equal(await manual.getByRole('button', { name: '批准并校验写入', exact: true }).isEnabled(), false);
    await manual.getByLabel('交接审核人', { exact: true }).fill('LOCAL BROWSER FIXTURE — not human approval');
    await manual.getByLabel('交接审核说明', { exact: true }).fill('Isolated QA verifies the local file writer; external test claims remain unverified.');
    assert.equal(await manual.getByRole('button', { name: '批准并校验写入', exact: true }).isEnabled(), false);
    await manual.getByLabel('批准交接文件写入', { exact: true }).check();
    await manual.getByRole('button', { name: '批准并校验写入', exact: true }).click();
    await manual.getByText('已审核并写入', { exact: true }).waitFor();
    state = await api('/state'); task = state.tasks.find(value => value.id === task.id);
    assert.equal(task.status, 'done'); assert.equal(task.last_attempt, null);
    const bytes = await readFile(path.join(task.worktree, 'manual-qa.txt'));
    assert.equal(bytes.toString('utf8'), 'LOCAL MANUAL QA FILE');
    assert.equal(task.latest_handoff.artifacts[0].sha256, createHash('sha256').update(bytes).digest('hex'));
    assert.equal(state.agents.find(value => value.id === 'workbuddy').status, 'manual_handoff');
    assert.equal(state.agents.find(value => value.id === 'workbuddy').verified_at, null);
    await closeDrawer();
  });
  await check('WorkBuddy 人工交接：选择 JSON 文件并拒绝，不写入文件', async () => {
    const task = await api('/tasks', { title: 'LOCAL MANUAL REJECTION FIXTURE', agent_id: 'workbuddy', auto_run: true });
    await page.locator('nav').getByRole('button', { name: '任务工作台', exact: true }).click();
    await page.locator('.task-card').filter({ hasText: 'LOCAL MANUAL REJECTION FIXTURE' }).click();
    const manual = page.getByRole('region', { name: 'WorkBuddy 人工交接' });
    await manual.getByRole('button', { name: '确认并复制任务', exact: true }).click();
    await manual.getByText('等待外部结果', { exact: true }).waitFor();
    const record = (await api(`/handoffs/${task.latest_handoff.id}`)).handoff;
    const submission = { handoff_id: record.id, task_id: task.id, packet_sha256: record.packet_sha256,
      summary: 'LOCAL FILE IMPORT FIXTURE; no WorkBuddy execution.', files: [{ path: 'must-not-write.txt', content: 'REJECTED FIXTURE' }],
      test_reports: [{ name: 'No external execution', status: 'not_run', output: 'LOCAL FIXTURE ONLY' }] };
    await manual.getByLabel('结果提供者', { exact: true }).fill('LOCAL FILE FIXTURE');
    await manual.getByLabel('选择结果 JSON', { exact: true }).setInputFiles({ name: 'local-handoff-fixture.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(submission)) });
    await manual.getByRole('button', { name: '导入并进入人工审查', exact: true }).click();
    await manual.getByText('待人工审查', { exact: true }).waitFor();
    await manual.getByLabel('交接审核人', { exact: true }).fill('LOCAL QA rejection fixture');
    await manual.getByLabel('交接审核说明', { exact: true }).fill('Reject this isolated fixture; do not write the imported file.');
    await manual.getByRole('button', { name: '拒绝本轮结果', exact: true }).click();
    await manual.getByText('已拒绝', { exact: true }).waitFor();
    const state = await api('/state'), after = state.tasks.find(value => value.id === task.id);
    assert.equal(after.status, 'blocked'); assert.equal(after.latest_handoff.phase, 'rejected');
    assert.equal(after.last_attempt, null); assert.deepEqual(after.latest_handoff.artifacts, []);
    const relative = path.relative(path.join(root, 'work/browser-qa'), path.resolve(after.worktree));
    assert.ok(relative && !relative.startsWith('..') && !path.isAbsolute(relative));
    assert.equal(existsSync(path.join(after.worktree, 'must-not-write.txt')), false);
    assert.ok(await manual.getByRole('button', { name: '准备新一轮交接', exact: true }).isVisible());
    await closeDrawer();
  });
  await check('Skills 在线检索入口、安全审查、清单验证和拒绝审批', async () => {
    await page.locator('nav').getByRole('button', { name: '能力与审批', exact: true }).click();
    await page.getByLabel('检索 Skill').fill('browser test');
    await page.getByRole('button', { name: '生成在线检索入口' }).click();
    await page.getByRole('link', { name: /GitHub · 查找/ }).waitFor();
    await page.getByRole('button', { name: '提交候选清单', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '提交 Skill 候选' });
    await dialog.getByLabel('能力名称', { exact: true }).fill('验收测试清单（非第三方 Skill）');
    await dialog.getByLabel('来源 URL').fill('https://example.com/nexus-acceptance');
    await dialog.getByLabel('完整清单内容').fill('Read project documentation inside the current workspace. This is a local acceptance test manifest.');
    await dialog.getByRole('button', { name: '提交审查队列' }).click();
    const card = page.locator('.skill-card').filter({ hasText: '验收测试清单（非第三方 Skill）' });
    await card.getByRole('button', { name: '安全审查', exact: true }).click();
    await card.getByRole('button', { name: '清单试运行', exact: true }).click();
    await card.getByText('清单试运行通过 · executed=false', { exact: true }).waitFor();
    await card.getByRole('button', { name: '人工决策', exact: true }).click();
    const approval = page.getByRole('dialog', { name: '记录人工决策' });
    await approval.getByLabel('决策', { exact: true }).selectOption('reject');
    await approval.getByLabel('审批人', { exact: true }).fill('自动验收脚本（非人工批准）');
    await approval.getByLabel('审批说明', { exact: true }).fill('验证拒绝审批可持久化，不授权安装或执行。');
    await approval.getByRole('button', { name: '保存人工决定' }).click();
    await card.getByText('已拒绝', { exact: true }).waitFor();
  });
  await check('Agent 卡片显示模型 / Provider / 连接方式与真实状态', async () => {
    await page.locator('nav').getByRole('button', { name: 'Agent 网络', exact: true }).click();
    const agents = await api('/agents');
    for (const agent of agents) {
      assert.ok(agent.binding && agent.binding.provider, `${agent.id} 必须声明 Provider`);
      assert.ok(agent.binding.runtime_kind, `${agent.id} 必须声明执行运行时`);
      assert.equal(typeof agent.model_api_callable, 'boolean');
      assert.equal(typeof agent.task_execution_capable, 'boolean');
    }
    // Every card states its runtime, provider and current model.
    assert.equal(await page.locator('.logical-card > .binding-facts').count(), 6);
    assert.equal(await page.locator('.logical-card > .capability-pair').count(), 6);
    // Nothing is verified in this isolated instance, so both facts read "未验证".
    assert.ok(await page.getByText('模型 API 未验证', { exact: true }).count() >= 1);
    assert.ok(await page.getByText('开发任务执行能力 未验证', { exact: true }).count() >= 1);
    // The usage summary is present on the card rather than fabricated.
    assert.equal(await page.locator('.logical-card > .agent-usage-inline').count(), 6);
  });
  await check('卡片内可切换模型与 Provider，且拒绝跨 Provider 组合', async () => {
    const card = page.locator('.logical-card[data-agent="deepseek"] .route-block').filter({ has: page.locator('.route-head strong', { hasText: 'DeepSeek 网关' }) });
    await card.getByRole('button', { name: '配置与密钥', exact: true }).click();
    await card.getByText('切换模型与 Provider', { exact: true }).click();
    const providerSelect = card.getByLabel('DeepSeek 网关 Provider', { exact: true });
    const options = await providerSelect.locator('option').evaluateAll(items => items.map(item => item.value));
    assert.deepEqual(options, ['gonode-gateway', 'deepseek-official']);
    // Choosing an unverified provider requires explicit acknowledgement.
    await providerSelect.selectOption('deepseek-official');
    const modelSelect = card.getByLabel('DeepSeek 网关 模型', { exact: true });
    assert.equal(await modelSelect.locator('option').count(), 2);
    assert.equal(await card.getByRole('button', { name: '应用绑定' }).isDisabled(), false);
    // Applying an unverified provider without acknowledgement must be refused,
    // both by the backend and visibly in the card.
    await withExpectedRefusal(async () => {
      await card.getByRole('button', { name: '应用绑定' }).click();
      await card.getByText(/需要显式确认后才会切换/).waitFor();
    });
    // Cross-provider pairing is refused by the backend, not just hidden in the UI.
    const refused = await withExpectedRefusal(() => page.evaluate(async () => {
      const { token } = await (await fetch('/api/session')).json();
      const response = await fetch('/api/agents/deepseek-gateway/binding', {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Nexus-Token': token },
        body: JSON.stringify({ provider_id: 'zai-official', model_key: 'zai-official/glm-5.3' }) });
      return response.status;
    }));
    assert.equal(refused, 422);
  });
  await check('合并卡片的使用明细读取逻辑 Agent，不再返回 404', async () => {
    await page.locator('.logical-card[data-agent="deepseek"]').getByRole('button', { name: '使用记录', exact: true }).click();
    const detail = page.getByRole('dialog', { name: 'deepseek 使用记录', exact: true });
    await detail.getByText('本月没有调用记录。', { exact: true }).waitFor();
    assert.equal((await api('/usage/agents/deepseek')).totals.requests, 0);
    await detail.getByRole('button', { name: '关闭使用记录' }).click();
  });
  await check('「用量与额度」页面：真实空状态而非虚假实时数据', async () => {
    await page.locator('nav').getByRole('button', { name: '用量与额度', exact: true }).click();
    await page.getByRole('heading', { name: /用量与额度/ }).waitFor();
    const usage = await api('/usage?span=month');
    assert.equal(usage.totals.requests, 0);
    assert.equal(usage.records.length, 0);
    // With no real calls the page must say so explicitly.
    await page.getByText('尚无任何模型调用记录', { exact: true }).waitFor();
    // All four budget windows render with unset limits, never a fake zero.
    assert.equal(await page.locator('.budget-window').count(), 2);
    await page.getByText('未设置', { exact: true }).first().waitFor();
    // The four quota kinds are separate panels.
    assert.equal(await page.locator('.quota-kind').count(), 4);
    for (const label of ['官方账户余额', '第三方中转站账户余额', 'Nexus 内部预算', '订阅套餐额度']) {
      assert.ok(await page.getByText(label, { exact: true }).count() >= 1, `${label} 必须独立显示`);
    }
  });
  await check('额度与余额：无文档化接口时显示「余额不可查询」', async () => {
    const quota = await api('/quota?probe=true');
    const byProvider = Object.fromEntries(quota.state.results.map(item => [item.provider_id, item]));
    assert.equal(byProvider['gonode-gateway'].queryable, false);
    assert.equal(byProvider['gonode-gateway'].status, 'unsupported');
    assert.equal(byProvider['hyai666-gateway'].display, '余额不可查询');
    // The official balance endpoint is documented, but has no key here, so the
    // console must ask for a credential rather than invent a number.
    assert.equal(byProvider['deepseek-official'].status, 'credential_required');
    assert.equal(byProvider['deepseek-official'].queryable, false);
    assert.equal(byProvider['deepseek-official'].balances.length, 0);
    // Gateway balance is never presented as the official account balance.
    assert.notEqual(byProvider['gonode-gateway'].kind, byProvider['deepseek-official'].kind);
    await page.getByText('余额不可查询', { exact: true }).first().waitFor();
  });
  await check('预算与额度保护可见，且禁止自动回退到 Codex', async () => {
    const budget = await api('/budget');
    assert.equal(budget.gpt_auto_dispatch, false);
    assert.equal(budget.settings.require_metered_approval, true);
    assert.equal(budget.settings.stop_dispatch_when_exhausted, true);
    for (const key of ['daily_budget', 'monthly_budget', 'per_task_tokens', 'per_task_budget']) {
      assert.equal(budget.settings[key], null, `${key} 默认必须为未设置而不是无限额`);
    }
    await page.getByText(/自动回退到 Codex\/GPT：禁止/).waitFor();
    // A zero budget must stop dispatch and report the reason in the UI.
    const blocked = await withExpectedRefusal(() => page.evaluate(async () => {
      const { token } = await (await fetch('/api/session')).json();
      const headers = { 'Content-Type': 'application/json', 'X-Nexus-Token': token };
      await fetch('/api/settings/budget', { method: 'PUT', headers, body: JSON.stringify({ daily_budget: 0 }) });
      const task = await (await fetch('/api/tasks', { method: 'POST', headers, body: JSON.stringify({ title: '预算保护浏览器验收', description: 'x', agent_id: 'deepseek-gateway' }) })).json();
      const run = await (await fetch(`/api/tasks/${task.id}/run`, { method: 'POST', headers })).json();
      await fetch('/api/settings/budget', { method: 'PUT', headers, body: JSON.stringify({ daily_budget: null }) });
      return run;
    }));
    assert.equal(blocked.accepted, false);
    assert.ok(blocked.reason.length > 0);
  });
  await check('Provider 配置与密钥管理页：只显示指纹，不显示密钥', async () => {
    const settings = await api('/settings/providers');
    assert.ok(settings.providers.length >= 10);
    assert.ok(settings.vault.backend === 'memory' || settings.vault.backend === 'dpapi');
    for (const provider of settings.providers) {
      assert.equal(provider.credential.configured, false);
      assert.equal('api_key' in provider.credential, false);
      assert.equal('secret' in provider.credential, false);
    }
    // The page renders the credential form but never a stored key value.
    await page.getByText('Provider 配置与密钥管理', { exact: true }).waitFor();
    assert.equal(await page.locator('.provider-key-form input[type=password]').count(), 0);
    // Opening the form must reveal a password-typed field that never renders a value.
    const target = page.locator('.provider-row').filter({ hasText: 'DeepSeek 官方 API' }).first();
    await target.getByRole('button', { name: '配置密钥' }).click();
    const password = target.locator('input[type=password]');
    assert.equal(await password.count(), 1);
    assert.equal(await password.inputValue(), '');
    // On this QA instance credential writes are disabled, and the refusal is
    // shown to the user rather than silently swallowed.
    await withExpectedRefusal(async () => {
      await password.fill('qa-should-not-be-stored');
      await target.locator('.provider-key-form input[type=checkbox]').check();
      await target.getByRole('button', { name: '保存到后端' }).click();
      await page.locator('.provider-settings [role=alert]').waitFor();
    });
    // The typed value is cleared from the DOM after the refusal, and it is not
    // present anywhere in the rendered page.
    assert.equal((await page.locator('body').innerText()).includes('qa-should-not-be-stored'), false);
  });
  await check('日志真实可见、筛选与数据库刷新持久化', async () => {
    await page.locator('nav').getByRole('button', { name: '执行日志', exact: true }).click();
    await page.getByLabel('搜索日志').fill('git version');
    assert.ok(await page.locator('.log-row').count() >= 1);
    assert.match(await page.locator('.terminal-log').innerText(), /git version/i);
    await page.reload();
    await page.getByText('实时已连接', { exact: true }).waitFor();
    const state = await api('/state');
    assert.ok(state.tasks.some(t => t.status === 'done'));
    assert.ok(state.skills.some(s => s.status === 'rejected'));
  });
  await check('WebSocket 断线后自动恢复与事件重同步', async () => {
    await context.setOffline(true);
    await page.getByText('实时连接中断', { exact: true }).waitFor({ timeout: 15000 });
    await context.setOffline(false);
    await page.getByText('实时已连接', { exact: true }).waitFor({ timeout: 20000 });
  });
  await page.locator('nav').getByRole('button', { name: '协作总览', exact: true }).click();
  await check('任务列折叠与更多任务持久化，搜索不会遗漏隐藏任务', async () => {
    for (let i = 0; i < 7; i++) await api('/tasks', { title: `LOCAL FOLDING FIXTURE ${i}`, agent_id: 'auto' });
    await page.reload();
    await page.getByText('实时已连接', { exact: true }).waitFor();
    const column = page.locator('.column-backlog');
    assert.equal(await column.locator('.task-card').count(), 5);
    await column.getByRole('button', { name: /展开其余/ }).click();
    assert.ok(await column.locator('.task-card').count() >= 7);
    await column.getByRole('button', { name: '折叠待执行任务', exact: true }).click();
    assert.equal(await column.locator('.task-card').count(), 0);
    await page.reload();
    await page.getByText('实时已连接', { exact: true }).waitFor();
    await column.locator('.column-hidden').waitFor();
    await page.locator('nav').getByRole('button', { name: '任务工作台', exact: true }).click();
    await column.locator('.column-hidden').waitFor();
    await page.getByLabel('搜索任务').fill('LOCAL FOLDING FIXTURE 0');
    await column.getByText('LOCAL FOLDING FIXTURE 0', { exact: true }).waitFor();
    await page.getByLabel('搜索任务').fill('');
    await column.locator('.column-hidden').waitFor();
    await column.getByRole('button', { name: '展开待执行任务', exact: true }).click();
    await column.getByRole('button', { name: /收起较早任务/ }).click();
    assert.equal(await column.locator('.task-card').count(), 5);
  });
  await check('一键连接验证先预览并批准，取消不会产生任务', async () => {
    await page.locator('nav').getByRole('button', { name: 'Agent 网络', exact: true }).click();
    const before = (await api('/tasks')).length;
    await page.getByRole('button', { name: '一键验证连接', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '一键验证连接确认', exact: true });
    await dialog.waitFor();
    assert.equal(await dialog.getByRole('button', { name: '确认并验证', exact: true }).isEnabled(), false);
    assert.equal(await dialog.getByLabel('验证 WorkBuddy', { exact: true }).isEnabled(), false);
    await dialog.getByRole('button', { name: '取消', exact: true }).click();
    assert.equal((await api('/tasks')).length, before);
  });
  await check('本地 UI fixture：批量验证携带明确批准，拒绝不冒充连接成功', async () => {
    // Intercept both preview and POST. This exercises the UI without any model.
    const endpoint = '**/api/agents/verification-batch';
    let body = null;
    const fixture = async route => {
      if (route.request().method() === 'GET') return route.fulfill({ json: { revision: 'LOCAL_UI_FIXTURE', note: 'LOCAL UI FIXTURE ONLY', routes: [{ agent_id: 'deepseek', name: 'LOCAL BATCH FIXTURE', runtime_id: 'deepseek-gateway', provider_id: 'LOCAL_PROVIDER_FIXTURE', model: 'LOCAL_MODEL_FIXTURE', eligible: true, reason: null, billing: 'metered-api' }] } });
      body = route.request().postDataJSON();
      return route.fulfill({ json: { results: [{ runtime_id: 'deepseek-gateway', accepted: false, reason: 'LOCAL UI FIXTURE: no request sent' }] } });
    };
    await page.route(endpoint, fixture);
    try {
      await page.getByRole('button', { name: '一键验证连接', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '一键验证连接确认', exact: true });
      const submit = dialog.getByRole('button', { name: '确认并验证', exact: true });
      await dialog.getByLabel('批准本次批量验证', { exact: true }).check();
      assert.equal(await submit.isEnabled(), true);
      await dialog.getByLabel('验证 LOCAL BATCH FIXTURE', { exact: true }).uncheck();
      assert.equal(await submit.isEnabled(), false);
      await dialog.getByLabel('验证 LOCAL BATCH FIXTURE', { exact: true }).check();
      assert.equal(await dialog.getByLabel('批准本次批量验证', { exact: true }).isChecked(), false);
      await dialog.getByLabel('批准本次批量验证', { exact: true }).check();
      await submit.click();
      await page.getByText('· LOCAL UI FIXTURE: no request sent', { exact: false }).waitFor();
      assert.deepEqual(body, { revision: 'LOCAL_UI_FIXTURE', runtime_ids: ['deepseek-gateway'], approve_usage: true });
    } finally { await page.unroute(endpoint, fixture); }
    await page.locator('nav').getByRole('button', { name: '协作总览', exact: true }).click();
  });
  await check('一键执行只预览目标，取消不产生请求或任务', async () => {
    const before = (await api('/tasks')).length;
    await page.getByRole('button', { name: '一键执行', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '一键执行任务', exact: true });
    await dialog.getByLabel('本次执行目标', { exact: true }).waitFor();
    assert.equal(await dialog.getByRole('button', { name: '确认并一键执行', exact: true }).isEnabled(), false);
    await dialog.getByRole('button', { name: '返回', exact: true }).click();
    assert.equal((await api('/tasks')).length, before);
  });
  await check('本地 UI fixture：协作修订和额度批准均不可省略，拒绝不报成功', async () => {
    const endpoint = '**/api/execution/**'; let body = null;
    const fixture = async route => {
      if (route.request().method() === 'GET') return route.fulfill({ json: {
        revision: 'LOCAL_EXECUTION_FIXTURE', eligible: true, blockers: [],
        items: [{ id: 'LOCAL_GOAL', title: 'LOCAL EXECUTION FIXTURE', mode: 'collaboration' }],
        selected: { id: 'LOCAL_GOAL', title: 'LOCAL EXECUTION FIXTURE', mode: 'collaboration', agent_id: 'auto', repair_keys: ['frontend'] },
        scopes: [{ key: 'frontend', title: 'LOCAL CHILD FIXTURE', agent_id: 'codex', provider: 'LOCAL_PROVIDER', model: 'LOCAL_MODEL', repair: true, dependencies: [] }]
      } });
      body = route.request().postDataJSON();
      return route.fulfill({ json: { accepted: false, reason: 'LOCAL FIXTURE: no model invoked' } });
    };
    await page.route(endpoint, fixture);
    try {
      await page.getByRole('button', { name: '一键执行', exact: true }).click();
      const dialog = page.getByRole('dialog', { name: '一键执行任务', exact: true });
      const submit = dialog.getByRole('button', { name: '确认并一键执行', exact: true });
      await dialog.getByLabel('批准本次任务执行', { exact: true }).check();
      assert.equal(await submit.isEnabled(), false);
      await dialog.getByLabel('批准失败节点修订', { exact: true }).check();
      assert.equal(await submit.isEnabled(), false);
      await dialog.getByLabel('单独批准 Codex', { exact: true }).check();
      assert.equal(await submit.isEnabled(), true);
      await submit.click();
      await dialog.getByRole('alert').filter({ hasText: 'LOCAL FIXTURE: no model invoked' }).waitFor();
      assert.deepEqual(body, { task_id: 'LOCAL_GOAL', agent_id: null, revision: 'LOCAL_EXECUTION_FIXTURE', approve_usage: true, approve_revisions: true, approve_codex: true });
      await dialog.getByRole('button', { name: '返回', exact: true }).click();
    } finally { await page.unroute(endpoint, fixture); }
  });
  await check('一键执行真实本地诊断：提交、实时日志与结果落库，不调用模型', async () => {
    const task = await api('/tasks', { title: 'LOCAL ONE CLICK DIAGNOSTIC', kind: 'diagnostic', agent_id: 'local-check' });
    await page.getByRole('button', { name: '一键执行', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '一键执行任务', exact: true });
    await dialog.getByLabel('本次执行目标', { exact: true }).selectOption(task.id);
    await dialog.getByText('local-check · local · 由执行器决定，结果中核对', { exact: true }).waitFor();
    await dialog.getByLabel('批准本次任务执行', { exact: true }).check();
    await dialog.getByRole('button', { name: '确认并一键执行', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    const state = await waitState(s => s.tasks.some(t => t.id === task.id && t.status === 'done'));
    const actual = state.tasks.find(t => t.id === task.id);
    assert.match(actual.result, /真实版本命令全部通过/);
    assert.equal(actual.last_attempt, null);
    const logs = await api('/logs?task_id=' + task.id + '&limit=2000');
    assert.ok(logs.some(l => /git version/.test(l.message)));
  });
  await check('网关输出上限必须明确批准，保存设置不执行任务', async () => {
    const target = await api('/tasks', { title: 'LOCAL REQUEST SETTINGS ONLY', agent_id: 'deepseek-gateway' });
    const before = (await api('/tasks')).length;
    await page.getByRole('button', { name: '一键执行', exact: true }).click();
    const dialog = page.getByRole('dialog', { name: '一键执行任务', exact: true });
    await dialog.getByLabel('本次执行目标', { exact: true }).selectOption(target.id);
    await dialog.getByText('网关请求设置：流式与输出上限', { exact: true }).click();
    await dialog.getByLabel('deepseek-gateway 使用 SSE', { exact: true }).check();
    await dialog.getByLabel('deepseek-gateway 输出上限', { exact: true }).selectOption('16384');
    const save = dialog.getByRole('button', { name: '保存请求设置（不执行）', exact: true });
    assert.equal(await save.isEnabled(), false);
    await dialog.getByLabel('deepseek-gateway 批准提高上限', { exact: true }).check();
    await save.click();
    await dialog.getByText(/SSE 流式 · 最大输出 16384/).waitFor();
    assert.equal(await dialog.getByLabel('批准本次任务执行', { exact: true }).isChecked(), false);
    assert.equal(await dialog.getByRole('button', { name: '确认并一键执行', exact: true }).isEnabled(), false);
    await dialog.getByRole('button', { name: '返回', exact: true }).click();
    assert.equal((await api('/tasks')).length, before);
    const state = await api('/state');
    assert.equal(state.tasks.find(t => t.id === target.id).last_attempt, null);
  });
  await check('桌面与手机宽度无横向溢出', async () => {
    await page.screenshot({ path: path.join(outputDir, 'agent-nexus-desktop.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(outputDir, 'agent-nexus-mobile.png'), fullPage: true });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth));
    for (const label of ['任务工作台', 'Agent 网络', '用量与额度', '能力与审批', '执行日志']) {
      await page.locator('nav').getByRole('button', { name: label, exact: true }).click();
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${label} mobile overflow`);
    }
    await page.locator('nav').getByRole('button', { name: '协作总览', exact: true }).click();
    await page.setViewportSize({ width: 1600, height: 1100 });
  });
  await check('浏览器无运行错误或失败接口', async () => {
    assert.deepEqual(failures, []);
    assert.deepEqual(network, []);
    assert.ok(expectedRefusals.length >= 1, '至少应有被明确断言的拒绝路径');
  });  await writeFile(path.join(outputDir, 'browser-test-results.json'), JSON.stringify({ time: new Date().toISOString(), url, executablePath, checks, browserErrors: failures, failedResponses: network, expectedRefusals, passed: true, dataSource: 'isolated local fixtures; no real model calls' }, null, 2));
  console.log(`${checks.length} browser acceptance checks passed.`);
} catch (error) {
  await page.screenshot({ path: path.join(root, 'work/e2e-failure.png'), fullPage: true });
  await writeFile(path.join(root, 'work/e2e-failure.json'), JSON.stringify({ checks, failures, network, error: error.stack }, null, 2));
  throw error;
} finally { await browser.close(); }
