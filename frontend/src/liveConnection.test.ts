import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LiveConnection } from './liveConnection';

class LocalSocket {
  readyState = 0;
  onopen: WebSocket['onopen'] = null;
  onmessage: WebSocket['onmessage'] = null;
  onerror: WebSocket['onerror'] = null;
  onclose: WebSocket['onclose'] = null;
  send = vi.fn();
  close = vi.fn(() => { this.readyState = 3; });
  open() { this.readyState = 1; this.onopen?.call(this as unknown as WebSocket, new Event('open')); }
  message(data: unknown) { this.onmessage?.call(this as unknown as WebSocket, new MessageEvent('message', { data: JSON.stringify(data) })); }
  closed(code = 1006) { this.readyState = 3; this.onclose?.call(this as unknown as WebSocket, { code } as CloseEvent); }
}

function fixture(overrides: Partial<ConstructorParameters<typeof LiveConnection>[0]> = {}) {
  const sockets: LocalSocket[] = [];
  const options = {
    bootstrap: vi.fn(async () => ({ key: 'LOCAL_SESSION_FIXTURE', cursor: 10 })),
    socket: vi.fn(() => { const socket = new LocalSocket(); sockets.push(socket); return socket; }),
    state: vi.fn(), error: vi.fn(), event: vi.fn(), healthy: vi.fn(),
    random: () => 0.5, heartbeatMs: 100, timeoutMs: 50, ...overrides,
  };
  return { live: new LiveConnection(options), sockets, options };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

describe('local backend event connection', () => {
  it('single-flights repeated online events and disposes all timers on stop', async () => {
    const f = fixture();
    await Promise.all([f.live.connect(), f.live.connect(), f.live.connect()]);
    await f.live.connect();
    expect(f.options.bootstrap).toHaveBeenCalledTimes(1);
    expect(f.sockets).toHaveLength(1);
    f.sockets[0].open();
    f.live.stop();
    await vi.advanceTimersByTimeAsync(100000);
    await f.live.connect();
    expect(f.options.bootstrap).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('ignores delayed close and messages belonging to an older generation', async () => {
    const f = fixture(); await f.live.connect(); f.sockets[0].open();
    f.live.offline(); await f.live.connect(); f.sockets[1].open();
    f.sockets[0].closed(); f.sockets[0].message({ id: 999 });
    expect(f.options.state).toHaveBeenLastCalledWith('live');
    expect(f.options.event).not.toHaveBeenCalled();
    expect(f.sockets).toHaveLength(2);
    f.live.stop();
  });

  it('detects three missed pongs without depending on task traffic', async () => {
    const f = fixture(); await f.live.connect(); f.sockets[0].open();
    await vi.advanceTimersByTimeAsync(350);
    expect(f.sockets[0].send).toHaveBeenCalledTimes(3);
    expect(f.sockets[0].close).toHaveBeenCalledTimes(1);
    expect(f.options.state).toHaveBeenLastCalledWith('offline');
    expect(f.options.bootstrap).toHaveBeenCalledTimes(1);
    f.live.stop();
  });

  it('accepts pongs separately from replay IDs and deduplicates task events', async () => {
    const f = fixture(); await f.live.connect(); f.sockets[0].open();
    for (let i = 0; i < 4; ++i) {
      await vi.advanceTimersByTimeAsync(100);
      f.sockets[0].message({ type: 'pong' });
    }
    f.sockets[0].message({ id: 11 }); f.sockets[0].message({ id: 11 }); f.sockets[0].message({ id: 9 });
    expect(f.options.healthy).toHaveBeenCalledTimes(4);
    expect(f.options.event).toHaveBeenCalledTimes(1);
    expect(f.sockets[0].close).not.toHaveBeenCalled(); f.live.stop();
  });

  it('does not loop on a rejected session or policy close', async () => {
    const f = fixture(); await f.live.connect(); f.sockets[0].open(); f.sockets[0].closed(1008);
    await vi.advanceTimersByTimeAsync(100000);
    expect(f.options.bootstrap).toHaveBeenCalledTimes(1); f.live.stop();
    const g = fixture({ bootstrap: vi.fn(async () => { throw Object.assign(new Error('LOCAL AUTH FIXTURE'), { status: 403 }); }) });
    await g.live.connect(); await vi.advanceTimersByTimeAsync(100000);
    expect(g.options.bootstrap).toHaveBeenCalledTimes(1); g.live.stop();
  });

  it('backs off and enters low-frequency cooldown after repeated failures', async () => {
    const f = fixture({ bootstrap: vi.fn(async () => { throw new Error('LOCAL NETWORK FIXTURE'); }), backoffMs: [100, 200], cooldownMs: 1000 });
    await f.live.connect();
    await vi.advanceTimersByTimeAsync(100); await vi.advanceTimersByTimeAsync(200);
    expect(f.options.bootstrap).toHaveBeenCalledTimes(3);
    expect(f.options.error).toHaveBeenCalledWith(expect.stringContaining('冷却'));
    await vi.advanceTimersByTimeAsync(999); expect(f.options.bootstrap).toHaveBeenCalledTimes(3);
    await vi.advanceTimersByTimeAsync(1); expect(f.options.bootstrap).toHaveBeenCalledTimes(4);
    f.live.stop();
  });

  it('discards stale bootstrap results before they can overwrite the visible snapshot', async () => {
    let finish!: (result: { key: string; cursor: number; accept: () => void }) => void;
    const accept = vi.fn();
    const bootstrap = vi.fn(async () => ({ key: 'NEW', cursor: 20 }));
    bootstrap.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const f = fixture({ bootstrap });
    const pending = f.live.connect(); f.live.offline(); await f.live.connect(); f.sockets[0].open();
    finish({ key: 'OLD', cursor: 1, accept }); await pending;
    expect(accept).not.toHaveBeenCalled(); expect(f.sockets).toHaveLength(1); f.live.stop();
  });

  it('recovers a hung opening socket and stops retrying while offline', async () => {
    const f = fixture(); await f.live.connect();
    await vi.advanceTimersByTimeAsync(50);
    expect(f.sockets[0].close).toHaveBeenCalledTimes(1);
    f.live.offline(); await vi.advanceTimersByTimeAsync(100000);
    expect(f.sockets).toHaveLength(1); expect(vi.getTimerCount()).toBe(0); f.live.stop();
  });
});
