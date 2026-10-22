// Read-only recheck of an existing real task. This script never invokes an AI model.
import { chromium } from '@playwright/test';
import { existsSync } from 'node:fs';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('../../', import.meta.url));
const previous = JSON.parse(await readFile(path.join(root, 'outputs/phase2-real-codex.json'), 'utf8'));
assert.ok(previous.passed && previous.mode === 'real-codex-cli');
const temp = path.join(root, 'work/evidence-browser-temp');
await mkdir(temp, { recursive: true }); process.env.TEMP = process.env.TMP = temp;
const executablePath = [process.env.NEXUS_BROWSER_PATH,
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1234/chrome-win64/chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1223/chrome-win64/chrome.exe')].find(p => p && existsSync(p));
if (!executablePath) throw new Error('需要已有 Chromium，或设置 NEXUS_BROWSER_PATH。');
const browser = await chromium.launch({ executablePath, headless: true });
const page = await browser.newPage({ viewport: { width: 1550, height: 1050 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto('http://127.0.0.1:8765');
  await page.getByText('实时已连接', { exact: true }).waitFor();
  const state = await page.evaluate(async () => (await fetch('/api/state')).json());
  const task = state.tasks.find(t => t.id === previous.task.id);
  assert.equal(task.status, 'done');
  assert.equal(task.last_attempt.exit_code, 0);
  assert.equal(task.last_attempt.thread_id, previous.attempt.thread_id);
  assert.equal((await readFile(path.join(task.worktree, 'hello.txt'))).toString('utf8'), 'Hello Agent Nexus');
  await page.locator('.task-card').filter({ hasText: task.title }).click();
  await page.getByRole('dialog', { name: '任务详情' }).waitFor();
  await page.getByText('退出码', { exact: true }).waitFor();
  await page.screenshot({ path: path.join(root, 'outputs/phase2-codex-details.png'), fullPage: true });
  await page.locator('.artifact-record').scrollIntoViewIfNeeded();
  await page.locator('.drawer-logs').scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(root, 'outputs/phase2-codex-logs.png'), fullPage: true });
  assert.deepEqual(errors, []);
  await writeFile(path.join(root, 'outputs/phase2-persistence.json'), JSON.stringify({
    passed: true, time: new Date().toISOString(), mode: 'read-only-no-model-call',
    task_id: task.id, persisted_thread_id: task.last_attempt.thread_id, exit_code: task.last_attempt.exit_code,
    file_content: 'Hello Agent Nexus', artifact: task.last_attempt.artifacts[0],
    agent: state.agents.find(a => a.id === 'codex'), browser_errors: errors,
  }, null, 2));
  console.log('PASS persisted real Codex task, physical file, exit code and screenshots (no model call)');
} finally { await browser.close(); }
