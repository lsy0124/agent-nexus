// Explicitly opt-in: this test calls the real installed Codex CLI and consumes account usage.
import { chromium } from '@playwright/test';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

if (process.env.NEXUS_LIVE_CODEX !== '1') throw new Error('真实验收需要显式设置 NEXUS_LIVE_CODEX=1；普通测试不会调用模型。');
const root = fileURLToPath(new URL('../../', import.meta.url));
const temp = path.join(root, 'work/live-browser-temp');
await mkdir(temp, { recursive: true }); process.env.TEMP = process.env.TMP = temp;
const executablePath = [process.env.NEXUS_BROWSER_PATH,
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1234/chrome-win64/chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1223/chrome-win64/chrome.exe'),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'].find(p => p && existsSync(p));
if (!executablePath) throw new Error('没有发现已有浏览器；测试不会安装浏览器。');
const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage({ viewport: { width: 1500, height: 1100 } });
const errors = [], events = [], checks = [];
let taskId = null;
const evidence = { mode: 'real-codex-cli', started_at: new Date().toISOString(), passed: false, checks };
page.on('pageerror', e => errors.push(e.message));
page.on('websocket', socket => socket.on('framereceived', frame => {
  try { const event = JSON.parse(String(frame.payload)); if (event.id) events.push({ id: event.id, type: event.type, task_id: event.payload?.task_id }); } catch { /* heartbeat/non-JSON */ }
}));
const api = async endpoint => page.evaluate(async endpoint => { const r = await fetch('/api' + endpoint); if (!r.ok) throw new Error(await r.text()); return r.json(); }, endpoint);
try {
  await page.goto('http://127.0.0.1:8765');
  await page.getByText('实时已连接', { exact: true }).waitFor();
  const agents = await api('/agents');
  const codex = agents.find(a => a.id === 'codex');
  assert.ok(codex.ready, JSON.stringify(codex));
  evidence.cli_version = codex.cli_version;
  checks.push('本机真实 CLI 与官方登录环境检测通过');
  await page.getByRole('button', { name: '创建任务', exact: true }).click();
  const form = page.getByRole('dialog', { name: '创建新任务' });
  await form.getByLabel('任务标题', { exact: true }).fill('创建一个 hello.txt 文件，内容为 Hello Agent Nexus');
  await form.getByLabel('目标与验收标准').fill('低风险真实验收：只创建 hello.txt，UTF-8 无 BOM，字节精确为 Hello Agent Nexus，不添加换行。不要创建其他文件，不要运行命令、安装、提交或推送。');
  assert.ok(await form.getByLabel('提交后执行 Codex', { exact: true }).isChecked());
  await form.getByRole('button', { name: '创建并执行', exact: true }).click();
  await page.getByRole('dialog', { name: '任务详情' }).waitFor();
  checks.push('通过控制台输入验收请求并提交真实执行');
  let task;
  for (let i = 0; i < 180; i++) {
    const state = await api('/state');
    task = state.tasks.find(t => t.title === '创建一个 hello.txt 文件，内容为 Hello Agent Nexus');
    if (task) { taskId = task.id; if (['done', 'failed', 'blocked', 'cancelled'].includes(task.status)) break; }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  evidence.task = task;
  assert.equal(task?.status, 'done', task?.result ?? '没有找到真实任务');
  const attempts = await api(`/tasks/${task.id}/attempts`);
  const attempt = attempts[0];
  evidence.attempt = attempt;
  assert.equal(attempt.exit_code, 0);
  assert.equal(attempt.status, 'done');
  assert.ok(attempt.thread_id && attempt.pid && attempt.cli_version);
  checks.push('真实会话、PID、turn.completed、退出码 0 与执行结果持久化');
  const expectedRoot = path.join(root, 'work/worktrees', task.id);
  assert.equal(path.resolve(task.worktree), path.resolve(expectedRoot));
  const actual = await readFile(path.join(task.worktree, 'hello.txt'));
  assert.equal(actual.toString('utf-8'), 'Hello Agent Nexus');
  assert.ok(existsSync(path.join(task.worktree, '.git')));
  await writeFile(path.join(root, 'outputs/codex-hello.txt'), actual);
  checks.push('真实 Git Worktree 中 hello.txt 的物理字节与验收内容一致');
  const response = await page.request.get(`http://127.0.0.1:8765/api/tasks/${task.id}/artifacts/hello.txt`);
  assert.equal(response.status(), 200);
  assert.equal((await response.body()).toString('utf-8'), 'Hello Agent Nexus');
  checks.push('已校验产物可通过控制台下载，内容一致');
  evidence.logs = await api(`/logs?task_id=${task.id}&limit=2000`);
  assert.ok(evidence.logs.some(log => log.source === 'codex'));
  assert.ok(evidence.logs.some(log => log.source === 'file-writer' && log.message.includes('hello.txt')));
  const after = (await api('/agents')).find(a => a.id === 'codex');
  assert.equal(after.status, 'connected'); assert.ok(after.verified_at);
  checks.push('只有真实请求成功后 Codex 才显示已连接');
  assert.ok(events.some(e => e.task_id === task.id && e.type.startsWith('codex.')));
  checks.push('实时 WebSocket 已收到该任务的真实 Codex 事件');
  await page.getByText('hello.txt', { exact: true }).waitFor();
  await page.screenshot({ path: path.join(root, 'outputs/phase2-codex-live.png'), fullPage: true });
  await page.reload(); await page.getByText('实时已连接', { exact: true }).waitFor();
  assert.equal((await api('/tasks')).find(t => t.id === task.id).status, 'done');
  checks.push('页面刷新后真实结果仍从 SQLite 读取');
  assert.deepEqual(errors, []);
  evidence.passed = true;
} catch (error) {
  evidence.error = error.stack;
  if (taskId) { evidence.attempts = await api(`/tasks/${taskId}/attempts`).catch(() => []); evidence.logs = await api(`/logs?task_id=${taskId}`).catch(() => []); }
  await page.screenshot({ path: path.join(root, 'work/codex-live-failure.png'), fullPage: true }).catch(() => {});
} finally {
  evidence.finished_at = new Date().toISOString(); evidence.events = events; evidence.browser_errors = errors;
  await writeFile(path.join(root, 'outputs/phase2-real-codex.json'), JSON.stringify(evidence, null, 2));
  await browser.close();
}
console.log(JSON.stringify({ passed: evidence.passed, taskId, checks, error: evidence.error }, null, 2));
if (!evidence.passed) process.exit(1);
