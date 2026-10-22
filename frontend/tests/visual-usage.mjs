/**
 * Visual QA for the unified usage & quota console.
 *
 * The usage rows are labelled fixtures, not real model calls: this script only
 * captures layout, responsive behaviour and interaction, and writes down what it
 * actually observed for each check.
 */
import { chromium } from '@playwright/test';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';

const root = fileURLToPath(new URL('../../', import.meta.url));
const browserTemp = path.join(root, 'work/browser-temp');
await mkdir(browserTemp, { recursive: true });
process.env.TEMP = process.env.TMP = browserTemp;
const browserPaths = [process.env.NEXUS_BROWSER_PATH,
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1234/chrome-win64/chrome.exe'),
  path.join(process.env.LOCALAPPDATA ?? '', 'ms-playwright/chromium-1223/chrome-win64/chrome.exe'),
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].filter(Boolean);
const executablePath = browserPaths.find(p => existsSync(p));
if (!executablePath) throw new Error('没有可用浏览器');
const url = process.env.NEXUS_URL ?? 'http://127.0.0.1:5174';
const out = path.join(root, 'outputs');
await mkdir(out, { recursive: true });

const browser = await chromium.launch({ executablePath, headless: true });
const context = await browser.newContext({ viewport: { width: 1600, height: 1200 }, deviceScaleFactor: 1 });
const page = await context.newPage();
const failures = [], network = [], observations = {};
page.on('pageerror', e => failures.push(e.message));
page.on('console', e => { if (e.type() === 'error') failures.push(e.text()); });
page.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/favicon.ico')) network.push(`${r.status()} ${r.url()}`); });
const api = async endpoint => page.evaluate(async endpoint => {
  const { token } = await (await fetch('/api/session')).json();
  return (await fetch('/api' + endpoint, { headers: { 'X-Nexus-Token': token } })).json();
}, endpoint);

try {
  await page.goto(url);
  await page.getByText('实时已连接', { exact: true }).waitFor();

  // --- Agent network: model / provider / connection / dual capability flags ---
  await page.locator('nav').getByRole('button', { name: 'Agent 网络', exact: true }).click();
  await page.locator('.agent-detail-card').first().waitFor();
  observations.agentCards = await page.locator('.agent-detail-card').count();
  observations.capabilityPairs = await page.locator('.capability-pair').count();
  observations.bindingFacts = await page.locator('.binding-facts').count();
  observations.usageSummaries = await page.locator('.agent-usage-inline').count();
  // The DeepSeek gateway is a route inside the DeepSeek logical Agent card,
  // not a card of its own: the route name lives in .route-head strong.
  const gw = page.locator('.logical-card[data-agent="deepseek"] .route-block')
    .filter({ has: page.locator('.route-head strong', { hasText: 'DeepSeek 网关' }) });
  observations.gatewayHeadline = await gw.locator('.route-head strong').innerText();
  await page.screenshot({ path: path.join(out, 'phase4-agents-network.png'), fullPage: true });
  await gw.scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(out, 'phase4-agent-card.png'), fullPage: false });
  // The route's binding form is behind its own 配置与密钥 toggle.
  await gw.getByRole('button', { name: '配置与密钥', exact: true }).click();
  await gw.getByText('切换模型与 Provider', { exact: true }).click();
  await gw.locator('.adapter-config input, .adapter-config select').first().waitFor();
  await page.screenshot({ path: path.join(out, 'phase4-agent-binding.png'), fullPage: false });

  // --- Usage & quota page ---------------------------------------------------
  await page.locator('nav').getByRole('button', { name: '用量与额度', exact: true }).click();
  await page.getByRole('heading', { name: /用量与额度/ }).waitFor();
  await page.locator('.usage-stats').waitFor();
  observations.usageStats = await page.locator('.usage-stats .stat-card').count();
  observations.bars = await page.locator('.usage-bar').count();
  observations.quotaKinds = await page.locator('.quota-kind').count();
  observations.budgetWindows = await page.locator('.budget-window').count();
  observations.detailRows = await page.locator('.usage-table tbody tr').count();
  observations.unavailableText = await page.getByText('余额不可查询', { exact: true }).count();
  observations.statValues = await page.locator('.usage-stats .stat-value').allInnerTexts();
  observations.usageNote = await page.locator('.usage-note-strip').innerText();
  await page.screenshot({ path: path.join(out, 'phase4-usage-quota.png'), fullPage: true });

  // Provider key management is part of the same page's settings section.
  const settings = page.locator('.provider-settings-panel');
  await settings.scrollIntoViewIfNeeded();
  observations.providerRows = await settings.locator('.provider-row').count();
  observations.vaultStrip = await settings.locator('.vault-strip').innerText();
  await page.screenshot({ path: path.join(out, 'phase4-provider-settings.png'), fullPage: false });

  // --- Drill-down for a single Agent ---------------------------------------
  // The call-detail table links each row to its Agent; the task-cost table does not.
  const detailTable = page.locator('.usage-table').filter({ hasText: '用量来源' }).last();
  const gatewayLink = detailTable.locator('tbody tr .link-button').filter({ hasText: 'deepseek-gateway' }).first();
  if (await gatewayLink.count() > 0) {
    await gatewayLink.click();
    await page.getByRole('dialog', { name: /使用记录/ }).waitFor();
    observations.drilldownStats = await page.locator('.detail-stats div').count();
    await page.screenshot({ path: path.join(out, 'phase4-agent-usage-detail.png'), fullPage: false });
    await page.getByRole('button', { name: '关闭使用记录' }).click();
  } else {
    observations.drilldownStats = 0;
  }

  // --- Mobile width must not overflow --------------------------------------
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(250);
  observations.mobileOverflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  await page.screenshot({ path: path.join(out, 'phase4-usage-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1600, height: 1200 });

  // --- The fixtures must be labelled, and no fabricated zero is shown -------
  const usage = await api('/usage?span=month');
  assert.equal(usage.totals.requests, 5);
  assert.equal(usage.totals.usage_unavailable, 1, '未上报用量的请求必须单独计数');
  assert.equal(usage.totals.usage_reported, 4);
  assert.equal(usage.records.length, 5);
  const quota = await api('/quota?probe=true');
  const byProvider = Object.fromEntries(quota.state.results.map(item => [item.provider_id, item]));
  // The user's two gateway routes have no documented balance API and must say so.
  assert.equal(byProvider['gonode-gateway'].display, '余额不可查询');
  assert.equal(byProvider['hyai666-gateway'].display, '余额不可查询');
  assert.equal(byProvider['deepseek-official'].status, 'credential_required');
  observations.quotaStatuses = Object.fromEntries(
    quota.state.results.map(item => [item.provider_id, `${item.kind}:${item.status}`]));
  assert.deepEqual(failures, []);
  assert.deepEqual(network, []);
  assert.ok(observations.mobileOverflow <= 0, `移动端横向溢出 ${observations.mobileOverflow}px`);

  await writeFile(path.join(out, 'visual-report-phase4.json'), JSON.stringify({
    time: new Date().toISOString(), url, executablePath, observations,
    browserErrors: failures, failedResponses: network, passed: true }, null, 2));
  console.log(JSON.stringify(observations, null, 2));
  console.log('visual QA passed.');
} catch (error) {
  await page.screenshot({ path: path.join(root, 'work/visual-failure.png'), fullPage: true });
  await writeFile(path.join(root, 'work/visual-failure.json'), JSON.stringify({ observations, failures, network, error: error.stack }, null, 2));
  throw error;
} finally { await browser.close(); }
