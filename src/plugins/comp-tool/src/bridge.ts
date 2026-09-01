/**
 * bridge.ts — 持久 Python worker 生命周期 + Content-Length 分帧解码
 * ============================================================================
 *
 * 规格来源：docs/ARCHITECTURE.md §5.2（worker 生命周期 8 项）+ §5.2.1（分帧纪律 P1–P4）。
 *
 * 为什么用持久 worker 而不是每次 spawn（架构 §5.2）：
 *   pandas/numpy/plotly 首次 import 约 2–3 秒。每次调用重启一次，
 *   一次诊断流程 8 个工具就是 20 秒纯等待，且**会话状态会丢**——
 *   用户已经做完的字段映射与中间结果会全部作废。
 *
 * 为什么用 Content-Length 而不是 NDJSON（架构 §5.2.1）：
 *   Python 侧的 traceback 自带空行，NDJSON 会把一次崩溃读成两条半截消息；
 *   Content-Length 以**字节数**定界，天然免疫消息体内的换行。
 */

import { spawn, type ChildProcess } from 'node:child_process';

// ---------------------------------------------------------------------------
// 分帧解码器（纯函数式、不依赖 Python，可脱离进程单测）
// ---------------------------------------------------------------------------

/** 单帧最大字节数默认值。超过即丢弃该帧并记诊断，**不缓冲**（架构 §5.2.1 边界表） */
export const MAX_PAYLOAD_BYTES = 32 * 1024 * 1024;

/**
 * 「找不到 header 分隔符时最多扫多少字节」的默认值。
 *
 * ★ 为什么需要它（2026-08-30 补，来自 frame.test.mjs 的 P4 用例）：
 *   若 Python 端往 stdout 吐了一段不含 `\r\n\r\n` 的非协议文本（例如某个库
 *   直接 print 到 stdout、或者 print 了超长单行日志），只靠 MAX_PAYLOAD_BYTES
 *   兜底的话，解码器要一直缓冲到 32MB 才肯丢弃 —— 这期间**一帧都解不出来**，
 *   外部表现就是「插件卡死」。
 *   合法的 Content-Length 头部不可能有 64KB 那么长，所以超过这个阈值仍无
 *   分隔符即可判定为垃圾，直接丢弃并重获同步。
 */
export const MAX_HEADER_SCAN_BYTES = 64 * 1024;

export interface FrameDecoderOptions {
  /** 单帧最大字节数，默认 32MB */
  maxPayloadBytes?: number;
  /** 无分隔符时最多扫描的字节数，默认 64KB */
  maxHeaderScanBytes?: number;
}

export interface DecodeResult {
  /** 成功解出的消息 */
  messages: any[];
  /** 丢弃的垃圾/超限帧的诊断信息（供日志，不影响主流程） */
  diagnostics: string[];
}

const CONTENT_LENGTH_RE = /Content-Length\s*:\s*(\d+)/i;

/**
 * Content-Length 分帧解码器。
 *
 * 处理四类边界（架构 §5.2.1 边界表，均已在 frame.test.mjs 实测覆盖）：
 *   1. 前导单行垃圾（如 `WARNING:xxx\n`）落在 header 区域内被正则忽略，帧正常解析
 *   2. 多行垃圾（traceback，自带 `\r\n\r\n`）→ 首个 headerEnd 落在垃圾内、
 *      检测不到合法 Content-Length → 丢弃该段 → 下一轮命中真帧
 *   3. 只有 Content-Length 但长度超限 → 丢弃该帧，记 payload-too-large，**不缓冲**
 *   4. 半包 / 粘包（一次 data 事件含 1.5 帧或 3 帧）→ 缓冲拼接后逐帧切出，
 *      跨 data 事件保持状态
 */
export class FrameDecoder {
  private buf: Buffer = Buffer.alloc(0);
  private maxPayloadBytes: number;
  private maxHeaderScanBytes: number;

  constructor(options: FrameDecoderOptions = {}) {
    this.maxPayloadBytes = options.maxPayloadBytes ?? MAX_PAYLOAD_BYTES;
    this.maxHeaderScanBytes = options.maxHeaderScanBytes ?? MAX_HEADER_SCAN_BYTES;
  }

  /** 喂入一段原始字节，返回解出的消息与诊断。 */
  feed(chunk: Buffer): DecodeResult {
    if (chunk && chunk.length) {
      this.buf = this.buf.length ? Buffer.concat([this.buf, chunk]) : chunk;
    }

    const messages: any[] = [];
    const diagnostics: string[] = [];

    // 循环切帧：一次 feed 可能包含多帧（粘包）
    for (;;) {
      const sep = this.buf.indexOf('\r\n\r\n');
      if (sep < 0) {
        // 连分隔符都还没到齐 —— 半包，等下一次 feed。
        // ★ 但合法头部不可能超过 maxHeaderScanBytes；超过仍无分隔符即判垃圾。
        //   不能等到 maxPayloadBytes(32MB) 才丢 —— 那期间一帧都解不出，表现为卡死。
        if (this.buf.length > this.maxHeaderScanBytes) {
          diagnostics.push(
            `discard-no-separator：无头部分隔符且已缓冲 ${this.buf.length} 字节 ` +
              `> 扫描上限 ${this.maxHeaderScanBytes}，判定为垃圾并丢弃以重获同步`,
          );
          this.buf = Buffer.alloc(0);
        }
        break;
      }

      const headerBlob = this.buf.subarray(0, sep);
      const m = CONTENT_LENGTH_RE.exec(headerBlob.toString('latin1'));

      if (!m) {
        // 边界 2：header 区内没有 Content-Length → 整段（含分隔符）都是垃圾
        diagnostics.push(
          `discard-header-segment：${headerBlob.subarray(0, 120).toString('latin1')}`,
        );
        this.buf = this.buf.subarray(sep + 4);
        continue;
      }

      const len = Number(m[1]);
      if (!Number.isFinite(len) || len < 0) {
        diagnostics.push('bad-content-length：长度非法，已丢弃该段');
        this.buf = this.buf.subarray(sep + 4);
        continue;
      }

      if (len > this.maxPayloadBytes) {
        // 边界 3：超限。不能按 len 去等——那会等一个永远不来的长度。
        // 直接把 header 段丢掉，让后续字节重新参与扫描。
        diagnostics.push(
          `payload-too-large：声明 ${len} 字节 > 上限 ${this.maxPayloadBytes}，已丢弃`,
        );
        this.buf = this.buf.subarray(sep + 4);
        continue;
      }

      const bodyStart = sep + 4;
      // 边界 4：半包——body 还没收齐，保留缓冲区等下一次 feed
      if (this.buf.length < bodyStart + len) break;

      // ★ P1：Content-Length 是 UTF-8 **字节数**，这里必须按字节切片后再解码，
      //   绝不能先 toString 再取长度（中文字符下两者差异可达 3 倍）。
      const body = this.buf.subarray(bodyStart, bodyStart + len);
      this.buf = this.buf.subarray(bodyStart + len);

      try {
        messages.push(JSON.parse(body.toString('utf8')));
      } catch {
        diagnostics.push(
          `invalid-json：${body.subarray(0, 120).toString('utf8')}`,
        );
      }
    }

    return { messages, diagnostics };
  }

  /** 把一条消息编码成 Content-Length 帧（P1：长度取 UTF-8 字节数）。 */
  static encode(payload: unknown): Buffer {
    const body = Buffer.from(JSON.stringify(payload), 'utf8');
    const header = Buffer.from(`Content-Length: ${body.length}\r\n\r\n`, 'ascii');
    return Buffer.concat([header, body]);
  }

  /**
   * 当前仍留在缓冲区里、等待后续字节的字节数（测试与诊断用）。
   *
   * 命名说明：叫 bufferedBytes 而不是 pendingBytes —— 后者容易和
   * 「在途请求数 pending.size」混淆（两者都在 bridge 里出现）。
   */
  get bufferedBytes(): number {
    return this.buf.length;
  }
}

// ---------------------------------------------------------------------------
// worker 生命周期
// ---------------------------------------------------------------------------

export interface BridgeOptions {
  /** Python 解释器绝对路径。★ 必须 `C:/...` 正斜杠（架构 §5.2 Windows 纪律） */
  pythonExe: string;
  /** worker 入口脚本绝对路径 */
  workerScript: string;
  /** 子进程工作目录（相对产出路径的基准） */
  projectRoot: string;
  /** 等待就绪帧的超时（毫秒），默认 30000 */
  readyTimeoutMs?: number;
  /** 心跳间隔（毫秒），默认 60000 */
  heartbeatIntervalMs?: number;
  /** 连续几次心跳无响应判定死亡，默认 2 */
  heartbeatMissLimit?: number;
  /** 30 秒窗口内最大重启次数，超过即放弃（防死循环），默认 3 */
  maxRestartPer30s?: number;
  /** 日志出口；缺省静默 */
  logger?: (...args: unknown[]) => void;
  /**
   * worker stderr 的专用出口。与 logger 分开是为了可测：
   * 端到端测试要断言「诊断信息走的是 stderr 而不是 stdout」，
   * 混进通用日志里就没法干净地断言了。缺省转发到 logger。
   */
  onStderr?: (chunk: string) => void;
}

/** 向上抛的内部错误码（与 Python 侧 errors.py 对齐） */
export const ERR_INTERNAL = 'INTERNAL_ERROR';
export const ERR_SESSION_EXPIRED = 'SESSION_EXPIRED';
export const ERR_SANDBOX_TIMEOUT = 'SANDBOX_TIMEOUT';

export type BridgeErrorCode =
  | typeof ERR_INTERNAL
  | typeof ERR_SESSION_EXPIRED
  | typeof ERR_SANDBOX_TIMEOUT;

/**
 * 结构化桥错误。
 *
 * ★ 为什么要有 code 字段，而不是把错误码拼进 message：
 *   早期实现是 `new Error(\`${ERR_SANDBOX_TIMEOUT}: xxx\`)`，调用方只能靠
 *   `message.startsWith('SANDBOX_TIMEOUT')` 这种字符串嗅探来分支 ——
 *   一旦文案改一个字（比如加个句号）分支就静默失效。
 *   错误码是**给程序读的**，文案是**给人读的**，两者必须分开。
 */
export class BridgeError extends Error {
  readonly code: BridgeErrorCode;

  constructor(code: BridgeErrorCode, message: string) {
    super(message);
    this.name = 'BridgeError';
    this.code = code;
  }
}

interface Pending {
  resolve: (v: any) => void;
  reject: (e: Error) => void;
  timer: NodeJS.Timeout;
  timeoutMs: number;
}

export class PythonBridge {
  private proc: ChildProcess | null = null;
  private decoder = new FrameDecoder();
  private seq = 0;
  private pending = new Map<number, Pending>();
  private queue: Array<() => void> = [];
  private ready: Promise<void> | null = null;
  private readyResolve: (() => void) | null = null;
  private readyReject: ((e: Error) => void) | null = null;
  private readyTimer: NodeJS.Timeout | null = null;
  private heartbeat: NodeJS.Timeout | null = null;
  private heartbeatMisses = 0;
  private restartTimes: number[] = [];
  private disposed = false;
  private starting = false;
  /**
   * logger / onStderr 是「可缺省的回调」，必须排除在 Required<> 之外 ——
   * 否则 `Required<>` 会把它们变成必填函数，与"缺省静默"的语义冲突。
   */
  private opts: Required<Omit<BridgeOptions, 'logger' | 'onStderr'>> &
    Pick<BridgeOptions, 'logger' | 'onStderr'>;

  constructor(options: BridgeOptions) {
    // ★ Windows 路径纪律：`/c/...` 会被 Git Bash 改写成 `C:\c\...` 导致
    //   MODULE_NOT_FOUND。这里做启动前断言，把问题暴露在报错里而不是超时里。
    if (!/^[A-Za-z]:\//.test(options.pythonExe)) {
      throw new Error(
        `pythonExe 必须是 C:/... 形式的正斜杠绝对路径，当前为：${options.pythonExe}`,
      );
    }
    this.opts = {
      readyTimeoutMs: 30_000,
      heartbeatIntervalMs: 60_000,
      heartbeatMissLimit: 2,
      maxRestartPer30s: 3,
      ...options,
    };
  }

  private log(...args: unknown[]): void {
    if (this.opts.logger) this.opts.logger('[comp-tool/bridge]', ...args);
  }

  /**
   * 确保 worker 已启动并收到就绪帧。并发调用共享同一次启动。
   *
   * 公开名为 `start()`（内部语义是「幂等启动」，但对外就该叫启动）。
   */
  async start(): Promise<void> {
    if (this.disposed) {
      throw new BridgeError(ERR_INTERNAL, 'bridge 已关闭，不可再启动。');
    }
    if (this.ready && this.proc && this.proc.exitCode === null) return this.ready;
    if (this.ready && this.starting) return this.ready;

    this.starting = true;
    this.ready = new Promise<void>((resolve, reject) => {
      this.readyResolve = resolve;
      this.readyReject = reject;
    });
    this.spawnLocked();
    return this.ready;
  }

  private spawnLocked(): void {
    const { pythonExe, workerScript, projectRoot, readyTimeoutMs } = this.opts;

    this.decoder = new FrameDecoder();
    this.proc = spawn(pythonExe, [workerScript], {
      cwd: projectRoot,
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUNBUFFERED: '1',
      },
    });

    this.proc.stdout?.on('data', (chunk: Buffer) => this.onStdout(chunk));
    this.proc.stderr?.on('data', (chunk: Buffer) => {
      const s = chunk.toString('utf8').trim();
      if (!s) return;
      if (this.opts.onStderr) this.opts.onStderr(s);
      else this.log('stderr:', s.slice(0, 500));
    });

    this.proc.on('exit', (code, signal) => {
      this.log(`worker 退出：code=${code} signal=${signal}`);
      this.stopHeartbeat();
      this.failAllPending(
        new BridgeError(
          ERR_SESSION_EXPIRED,
          `worker 退出（code=${code}），会话状态已丢失，请重新加载数据。`,
        ),
      );
      this.proc = null;
      this.ready = null;
      this.starting = false;
      // 崩溃恢复：清空就绪态，下次请求会重新拉起
      if (!this.disposed) this.drainQueue();
    });

    this.proc.on('error', (err: Error) => {
      this.log('worker spawn 失败：', err.message);
      this.readyReject?.(
        new BridgeError(ERR_INTERNAL, `无法启动 Python worker：${err.message}`),
      );
      this.readyReject = null;
    });

    this.readyTimer = setTimeout(() => {
      this.readyReject?.(
        new BridgeError(
          ERR_INTERNAL,
          `worker 在 ${readyTimeoutMs}ms 内未发出就绪帧。`,
        ),
      );
      this.readyReject = null;
    }, readyTimeoutMs);
  }

  private onStdout(chunk: Buffer): void {
    const { messages, diagnostics } = this.decoder.feed(chunk);
    for (const d of diagnostics) this.log('分帧诊断：', d);

    for (const msg of messages) {
      if (!msg || typeof msg !== 'object') continue;

      // worker 主动通知：{"jsonrpc":"2.0","method":"ready"|"log"}（无 id）
      if (typeof msg.method === 'string' && msg.id === undefined) {
        if (msg.method === 'ready') {
          if (this.readyTimer) clearTimeout(this.readyTimer);
          this.readyTimer = null;
          this.starting = false;
          this.startHeartbeat();
          this.readyResolve?.();
          this.readyResolve = null;
          this.drainQueue();
        } else {
          this.log('worker 通知：', msg.method, msg.params ?? '');
        }
        continue;
      }

      // 响应：配对 id
      if (typeof msg.id === 'number') {
        this.settle(msg.id, msg);
      }
    }
  }

  private settle(id: number, msg: any): void {
    const p = this.pending.get(id);
    if (!p) return;
    clearTimeout(p.timer);
    this.pending.delete(id);
    if (msg.error) {
      const e = msg.error as { code?: number; message?: string; data?: unknown };
      p.reject(new Error(`${e.message ?? 'worker 返回错误'}${e.data ? ` | ${JSON.stringify(e.data)}` : ''}`));
    } else {
      p.resolve(msg.result);
    }
  }

  private failAllPending(err: Error): void {
    for (const [, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(err);
    }
    this.pending.clear();
  }

  /** 队列排空：所有等待启动的请求在 worker 就绪后依次发出。 */
  private drainQueue(): void {
    const q = this.queue;
    this.queue = [];
    for (const task of q) {
      try {
        task();
      } catch (e) {
        this.log('队列任务异常：', (e as Error).message);
      }
    }
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatMisses = 0;
    this.heartbeat = setInterval(() => {
      if (!this.proc) return;
      this.request('ping', {}, 5_000)
        .then(() => {
          this.heartbeatMisses = 0;
        })
        .catch(() => {
          this.heartbeatMisses += 1;
          this.log(`心跳失败 ${this.heartbeatMisses}/${this.opts.heartbeatMissLimit}`);
          if (this.heartbeatMisses >= this.opts.heartbeatMissLimit) {
            this.log('心跳连续失败，判定 worker 死亡，准备重启。');
            this.stopHeartbeat();
            try {
              this.proc?.kill();
            } catch {
              /* 已退出 */
            }
          }
        });
    }, this.opts.heartbeatIntervalMs);
    // 心跳定时器不应阻止进程退出
    this.heartbeat.unref?.();
  }

  private stopHeartbeat(): void {
    if (this.heartbeat) {
      clearInterval(this.heartbeat);
      this.heartbeat = null;
    }
  }

  /** 重启频率闸门：30 秒内超过阈值即放弃，避免崩溃-重启死循环。 */
  private checkRestartBudget(): void {
    const now = Date.now();
    this.restartTimes = this.restartTimes.filter((t) => now - t < 30_000);
    this.restartTimes.push(now);
    if (this.restartTimes.length > this.opts.maxRestartPer30s) {
      throw new Error(
        `${ERR_INTERNAL}: 30 秒内 worker 重启超过 ${this.opts.maxRestartPer30s} 次，已停止重启。请检查 Python 环境与依赖。`,
      );
    }
  }

  /**
   * 发一次 JSON-RPC 请求。worker 未就绪时排队（worker 内单线程串行，插件侧不限制并发）。
   */
  async request(method: string, params: Record<string, unknown>, timeoutMs = 30_000): Promise<any> {
    if (this.disposed) throw new BridgeError(ERR_INTERNAL, 'bridge 已关闭。');

    // 未就绪 → 入队，等 ready 通知后统一放行
    if (!this.proc || this.starting || !this.ready) {
      return new Promise((resolve, reject) => {
        this.queue.push(() => {
          this.dispatch(method, params, timeoutMs).then(resolve, reject);
        });
        this.start().catch(reject);
      });
    }

    return this.dispatch(method, params, timeoutMs);
  }

  private dispatch(method: string, params: Record<string, unknown>, timeoutMs: number): Promise<any> {
    return new Promise((resolve, reject) => {
      if (!this.proc || !this.proc.stdin) {
        // worker 已死 → 触发一次重启预算检查后重排
        try {
          this.checkRestartBudget();
        } catch (e) {
          reject(e as Error);
          return;
        }
        this.queue.push(() => {
          this.dispatch(method, params, timeoutMs).then(resolve, reject);
        });
        this.start().catch(reject);
        return;
      }

      const id = ++this.seq;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        try {
          this.proc?.kill();
        } catch {
          /* 已退出 */
        }
        reject(
          new BridgeError(
            ERR_SANDBOX_TIMEOUT,
            `${method} 超过 ${timeoutMs}ms 未返回，已终止 worker。`,
          ),
        );
      }, timeoutMs);

      this.pending.set(id, { resolve, reject, timer, timeoutMs });
      const frame = FrameDecoder.encode({ jsonrpc: '2.0', id, method, params });
      this.proc.stdin.write(frame);
    });
  }

  /**
   * 优雅关闭：发 shutdown → 等 3s → kill。
   *
   * 公开名 `shutdown()`（对应 worker 侧的 shutdown 方法）；`dispose()` 是
   * cordis 的惯用钩子名，保留为同义转调，供 `ctx.on('dispose')` 使用。
   */
  async shutdown(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.stopHeartbeat();

    const proc = this.proc;
    if (!proc) return;

    try {
      proc.stdin?.write(FrameDecoder.encode({ jsonrpc: '2.0', id: ++this.seq, method: 'shutdown', params: {} }));
    } catch {
      /* stdin 已关闭 */
    }

    await new Promise<void>((resolve) => {
      const t = setTimeout(() => {
        try {
          proc.kill();
        } catch {
          /* 已退出 */
        }
        resolve();
      }, 3_000);
      proc.once('exit', () => {
        clearTimeout(t);
        resolve();
      });
    });

    this.failAllPending(new BridgeError(ERR_INTERNAL, 'bridge 已关闭。'));
  }

  /** cordis 惯用钩子（`ctx.on('dispose')` → `bridge.dispose()`）。 */
  async dispose(): Promise<void> {
    await this.shutdown();
  }

  /**
   * worker 是否处于「已启动且未退出、可接受请求」的状态。
   *
   * 与 `stats.alive` 的区别：这里额外排除「正在启动中（尚未收到 ready 帧）」，
   * 因此 `isReady === true` 意味着此刻发请求不会被排进队列等待冷启动。
   */
  get isReady(): boolean {
    return !!this.proc && this.proc.exitCode === null && !this.starting;
  }

  /** 诊断用：当前在途请求数 / 队列长度 / 存活标志 */
  get stats(): { pending: number; queued: number; alive: boolean } {
    return {
      pending: this.pending.size,
      queued: this.queue.length,
      alive: !!this.proc && this.proc.exitCode === null,
    };
  }
}
