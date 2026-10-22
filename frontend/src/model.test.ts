import { describe, expect, it } from 'vitest';
import { graphLayout, missingDependencies, selectTasks } from './model';
import type { Task } from './types';

const task = (id: string, dependencies: string[] = [], status: Task['status'] = 'backlog'): Task => ({ id, title: `任务 ${id}`, description: '', agent_id: 'codex', kind: 'agent', status, priority: 'normal', dependencies, requires_skills: [], capability_gaps: [], result: null, worktree: null, created_at: '', updated_at: '', input_files: [], last_attempt: null });
describe('task presentation model', () => {
  it('filters real tasks by query and executor without mutating source', () => {
    const tasks = [task('a'), { ...task('b'), agent_id: 'gemini' }];
    expect(selectTasks(tasks, '任务 B', 'all').map(t => t.id)).toEqual(['b']);
    expect(selectTasks(tasks, '', 'codex').map(t => t.id)).toEqual(['a']);
    expect(tasks).toHaveLength(2);
  });
  it('only completed dependencies are satisfied', () => {
    const tasks = [task('a', [], 'done'), task('b', [], 'blocked'), task('c', ['a', 'b'])];
    expect(missingDependencies(tasks[2], tasks).map(t => t.id)).toEqual(['b']);
  });
  it('places a dependency before all of its descendants', () => {
    const layout = graphLayout([task('c', ['b']), task('b', ['a']), task('a')]);
    expect(layout[0].x).toBeGreaterThan(layout[1].x);
    expect(layout[1].x).toBeGreaterThan(layout[2].x);
  });
  it('survives filtered missing dependencies and guards unexpected cycles', () => {
    expect(graphLayout([task('a', ['missing'])])[0].x).toBe(28);
    expect(graphLayout([task('a', ['b']), task('b', ['a'])])).toHaveLength(2);
  });
});
