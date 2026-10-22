import type { Task, TaskStatus } from './types';

export function agentStatusLabel(agent?: { status: string; status_label?: string }): string {
  return agent?.status_label ?? (agent?.status === 'connected' ? '已连接' : agent?.status === 'manual_handoff' ? '人工交接' : '未连接');
}

export const statusLabel: Record<TaskStatus, string> = { backlog: '待执行', running: '运行中', blocked: '已受阻', done: '已完成', failed: '失败', cancelled: '已取消' };
export function selectTasks(tasks: Task[], query: string, filter: string): Task[] {
  const q = query.toLocaleLowerCase().trim();
  return tasks.filter(t => (!q || `${t.title} ${t.description} ${t.id} ${t.agent_id}`.toLocaleLowerCase().includes(q)) && (filter === 'all' || t.agent_id === filter));
}
export function missingDependencies(task: Task, tasks: Task[]): Task[] {
  return tasks.filter(t => task.dependencies.includes(t.id) && t.status !== 'done');
}
export function graphLayout(tasks: Task[]) {
  const byId = new Map(tasks.map(t => [t.id, t]));
  const levels = new Map<string, number>();
  const level = (id: string, seen = new Set<string>()): number => {
    if (levels.has(id)) return levels.get(id)!;
    if (seen.has(id)) return 0;
    const next = new Set(seen).add(id);
    const deps = byId.get(id)?.dependencies.filter(d => byId.has(d)) ?? [];
    const value = deps.length ? 1 + Math.max(...deps.map(d => level(d, next))) : 0;
    levels.set(id, value);
    return value;
  };
  const rows = new Map<number, number>();
  return tasks.map(task => {
    const col = level(task.id), row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    return { task, x: 28 + col * 290, y: 30 + row * 112 };
  });
}

