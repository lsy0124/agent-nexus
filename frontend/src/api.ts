let token = '';
export async function session(): Promise<string> {
  const response = await fetch('/api/session', { signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw Object.assign(new Error('本地后端未响应'), { status: response.status });
  token = (await response.json()).token;
  return token;
}
export async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  if (method === 'POST' && /^\/tasks\/[a-f0-9]{12}\/run$/.test(path)) {
    const fields = (body ?? {}) as { execution_key?: string };
    body = { ...fields, execution_key: fields.execution_key ?? crypto.randomUUID() };
  }
  const response = await fetch(`/api${path}`, { method, ...(method === 'GET' ? { signal: AbortSignal.timeout(15000) } : {}), headers: { 'Content-Type': 'application/json', 'X-Nexus-Token': token }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
  if (!response.ok) {
    const data = await response.json().catch(() => ({ detail: response.statusText }));
    throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : '输入不符合要求，请检查表单'), { status: response.status });
  }
  return response.json();
}

/** Format a token count for display; `null` means the provider reported nothing. */
export const tokens = (value: number | null | undefined): string =>
  value === null || value === undefined ? '未报告' : value.toLocaleString('zh-CN');

/** Costs are always shown as an estimate, never as a provider bill. */
export function cost(value: number | null | undefined, currency: string | null | undefined): string {
  if (value === null || value === undefined) return '不可估算';
  if (currency === 'credits') return `${value.toLocaleString('zh-CN', { maximumFractionDigits: 4 })} credits（估算）`;
  if (!currency) return '不可估算';
  return `${currency} ${value.toLocaleString('zh-CN', { maximumFractionDigits: 6 })}（估算）`;
}

export const percent = (value: number | null | undefined): string =>
  value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`;
