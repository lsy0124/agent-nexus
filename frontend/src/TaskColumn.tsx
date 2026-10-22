import { useState } from 'react';
import { ChevronDown, ChevronRight, Plus } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Task } from './types';

type Preference = { collapsed: boolean; expanded: boolean };
function read(key: string): Preference {
  try { const p = JSON.parse(localStorage.getItem(key) ?? '{}'); return { collapsed: p.collapsed === true, expanded: p.expanded === true }; }
  catch { return { collapsed: false, expanded: false }; }
}

export function TaskColumn({ column, title, items, workspace, searching, onCreate, renderTask }: { column: string; title: string; items: Task[]; workspace: string; searching: boolean; onCreate?: () => void; renderTask: (task: Task) => ReactNode }) {
  const key = `nexus.task-column.${workspace}.${column}`;
  const [preference, setPreference] = useState(() => read(key));
  const update = (patch: Partial<Preference>) => { const p = { ...preference, ...patch }; setPreference(p); try { localStorage.setItem(key, JSON.stringify(p)); } catch { /* storage may be unavailable */ } };
  const collapsed = !searching && preference.collapsed;
  const visible = searching || preference.expanded || column === 'running' ? items : items.slice(0, 5);
  return <div className={`kanban-column column-${column}`}>
    <div className="column-heading"><button className="column-toggle" disabled={searching} title={searching ? '搜索或筛选时显示全部匹配任务' : undefined} aria-label={`${collapsed ? '展开' : '折叠'}${title}任务`} aria-expanded={!collapsed} onClick={() => update({ collapsed: !collapsed })}>
      {collapsed ? <ChevronRight size={13}/> : <ChevronDown size={13}/>}<span className="column-dot"/><strong>{title}</strong><span>{items.length}</span>
    </button>{onCreate && <button aria-label="新增待执行任务" onClick={onCreate}><Plus size={14}/></button>}</div>
    {collapsed ? <button className="column-hidden" onClick={() => update({ collapsed: false })}>已折叠 {items.length} 项任务 · 点击展开</button> : <div className="column-body">
      {visible.map(renderTask)}
      {!items.length && <div className="column-empty"><span>{column === 'backlog' ? '从一个清晰的目标开始' : column === 'running' ? '等待真实执行' : column === 'blocked' ? '暂无受阻任务' : '交付将出现在这里'}</span>{onCreate && <button onClick={onCreate}><Plus size={13}/>创建第一项任务</button>}</div>}
      {!searching && column !== 'running' && items.length > 5 && <button className="column-more" onClick={() => update({ expanded: !preference.expanded })}>{preference.expanded ? '收起较早任务 · 仅显示最近 5 项' : `展开其余 ${items.length - 5} 项任务`}</button>}
    </div>}
  </div>;
}
