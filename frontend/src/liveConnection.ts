/** UI-to-backend connection only. Never submits or retries an Agent task. */
export type LiveState = 'connecting' | 'live' | 'offline';
type Socket = Pick<WebSocket, 'readyState' | 'send' | 'close' | 'onopen' | 'onmessage' | 'onerror' | 'onclose'>;
type Options = {
  bootstrap: () => Promise<{ key: string; cursor: number; accept?: () => void }>;
  socket: (key: string, cursor: number) => Socket;
  state: (state: LiveState) => void;
  error: (message: string) => void;
  event: (message: { id: number }) => void;
  healthy: () => void;
  heartbeatMs?: number; timeoutMs?: number; failureLimit?: number;
  backoffMs?: number[]; cooldownMs?: number; random?: () => number;
};

export class LiveConnection {
  private generation = 0;
  private connecting = false;
  private enabled = true;
  private stopped = false;
  private authBlocked = false;
  private cursor = 0;
  private attempts = 0;
  private missed = 0;
  private current: Socket | null = null;
  private retry?: ReturnType<typeof setTimeout>;
  private heartbeat?: ReturnType<typeof setInterval>;
  private deadline?: ReturnType<typeof setTimeout>;
  constructor(private options: Options) {}

  private release() {
    ++this.generation;
    clearTimeout(this.retry); clearInterval(this.heartbeat); clearTimeout(this.deadline);
    this.retry = this.heartbeat = this.deadline = undefined;
    const old = this.current; this.current = null; this.connecting = false;
    old?.close();
  }
  private retryLater() {
    if (!this.enabled || this.stopped || this.authBlocked || this.retry) return;
    const delays = this.options.backoffMs ?? [1000, 2000, 4000, 8000, 16000, 30000];
    const cooldown = this.attempts >= delays.length;
    const delay = cooldown ? this.options.cooldownMs ?? 60000
      : Math.min(30000, delays[this.attempts] * (0.8 + 0.4 * (this.options.random ?? Math.random)()));
    this.attempts = cooldown ? 0 : this.attempts + 1;
    if (cooldown) this.options.error('连接多次失败，进入冷却；稍后进行低频恢复检查。');
    this.retry = setTimeout(() => { this.retry = undefined; void this.connect(); }, delay);
  }
  async connect() {
    if (this.stopped || this.authBlocked || this.connecting || (this.current && this.current.readyState <= 1)) return;
    this.enabled = true;
    clearTimeout(this.retry); this.retry = undefined;
    this.connecting = true;
    const generation = ++this.generation;
    const valid = () => generation === this.generation && !this.stopped && this.enabled;
    this.options.state('connecting');
    try {
      const session = await this.options.bootstrap();
      if (!valid()) return;
      session.accept?.();
      this.cursor = Math.max(this.cursor, session.cursor);
      const socket = this.options.socket(session.key, this.cursor);
      this.current = socket;
      const failed = () => {
        if (!valid()) return;
        this.release(); this.options.state('offline'); this.retryLater();
      };
      socket.onopen = () => {
        if (!valid()) return;
        clearTimeout(this.deadline); this.deadline = undefined;
        this.missed = 0; this.options.state('live'); this.options.error('');
        this.heartbeat = setInterval(() => {
          if (!valid()) return;
          if (socket.readyState !== 1) { failed(); return; }
          if (this.deadline) return;
          try { socket.send('ping'); } catch { failed(); return; }
          this.deadline = setTimeout(() => {
            this.deadline = undefined;
            if (valid() && ++this.missed >= (this.options.failureLimit ?? 3)) failed();
          }, this.options.timeoutMs ?? 10000);
        }, this.options.heartbeatMs ?? 15000);
      };
      socket.onmessage = event => {
        if (!valid()) return;
        let message;
        try { message = JSON.parse(String(event.data)); }
        catch { this.options.error('后端事件格式无效'); failed(); return; }
        if (!message || typeof message !== 'object') { this.options.error('后端事件格式无效'); failed(); return; }
        if (message.type === 'pong') {
          clearTimeout(this.deadline); this.deadline = undefined;
          this.missed = 0; this.attempts = 0; this.options.healthy(); return;
        }
        if (!Number.isSafeInteger(message.id) || message.id <= this.cursor) return;
        this.cursor = message.id; this.options.event(message);
      };
      socket.onerror = failed;
      socket.onclose = event => {
        if (!valid()) return;
        if (event.code === 1008) {
          this.authBlocked = true; this.options.error('本地会话被拒绝，请刷新页面重新建立会话。');
        }
        failed();
      };
      // Opening a WebSocket can itself hang indefinitely.
      this.deadline = setTimeout(() => { if (valid() && socket.readyState !== 1) failed(); }, this.options.timeoutMs ?? 10000);
    } catch (error) {
      if (!valid()) return;
      if ([401, 403].includes((error as { status?: number }).status ?? 0)) this.authBlocked = true;
      this.options.error((error as Error).message);
      this.release(); this.options.state('offline'); this.retryLater();
    } finally { if (valid()) this.connecting = false; }
  }
  offline() { this.enabled = false; this.release(); this.options.state('offline'); }
  stop() { this.stopped = true; this.enabled = false; this.release(); }
}
