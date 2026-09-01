/**
 * service.ts — 11 个工具的 defineTool 注册
 * ============================================================================
 *
 * 双路径注册（架构 §2.2 裁决 + 2026-08-30 实测修正）：
 *
 *   方案 A（首选）：官方 `defineTool` + **扁平 DSL**
 *     ✅ 白送 validateArgs → 11 个工具的参数校验零自研
 *     ⚠️ 扁平 DSL 是**唯一可行解**：实测把顶层 JSON Schema
 *        `{type:'object',properties:{...}}` 传给官方 defineTool 会抛
 *        `parameters.type must be a value schema object`
 *        （type/properties/required 被当成三个参数名）。
 *
 *   方案 B（降级）：本地 identity + 顶层 JSON Schema
 *     ✅ dsh-excel-kit 金标准已验证可跑
 *     ❌ 无参数校验
 *     ⚠️ 实测：方案 B **不能**喂给官方 defineTool（同上报错）。
 *        因此降级路径必须整个换掉 defineTool 实现，不能只换 schema 形态。
 *
 * 探测时机：模块加载时同步探测一次，失败则整体切方案 B。
 */

import { createRequire } from 'node:module';
import * as path from 'node:path';
import { TOOL_METAS, type ToolMeta } from './schemas.js';
import { PythonBridge } from './bridge.js';
import {
  ENVELOPE_OUTPUT_SCHEMA,
  renderEnvelope,
  envelopeMeta,
  type Envelope,
} from './envelope.js';

/**
 * ★ ESM 下取 require：本包 package.json 是 `"type": "module"`，
 *   直接用全局 `require` 会在加载时抛 `ReferenceError: require is not defined`。
 *   宿主 dsh 的 @deepseek-ai/* 包同样是 ESM，因此不能改用 CommonJS
 *   （CJS 里 `require()` 一个纯 ESM 包会失败）。用 createRequire 是唯一正解。
 */
const nodeRequire = createRequire(import.meta.url);

// ---------------------------------------------------------------------------
// 宿主注入的最小类型面（与 dsh-excel-kit/service.ts 同构，避免依赖 dsh 内部类型）
// ---------------------------------------------------------------------------

export interface ContentBlock {
  type: 'text';
  text: string;
}

export interface ToolExec {
  callId?: string;
  name?: string;
  arguments?: Record<string, unknown>;
  signal?: AbortSignal;
  agent?: { sessionId?: string } | null;
}

export interface PluginContext {
  tools: { register: (tool: unknown) => void };
  /** cordis 可选服务访问（未声明 inject 的服务属性访问会抛错，必须用 get） */
  get?: (name: string) => unknown;
  session?: { sessionId?: string };
  on?: (event: string, cb: (...args: unknown[]) => void) => void;
}

export interface DefineToolInput {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  output: {
    schema: Record<string, unknown>;
    render: (args: Record<string, unknown>, value: unknown) => ContentBlock[];
    presentationMeta?: (args: Record<string, unknown>, value: unknown) => Record<string, unknown>;
  };
  isConcurrencySafe?: () => boolean;
  timeoutMs?: number;
  execute: (args: Record<string, unknown>, exec: ToolExec) => Promise<unknown>;
}

// ---------------------------------------------------------------------------
// 方案 A / 方案 B 探测
// ---------------------------------------------------------------------------

type DefineToolFn = (input: DefineToolInput) => unknown;

/**
 * 把「扁平 DSL」转成「顶层 JSON Schema」—— 复刻官方
 * `parameterSchemaSpecToJsonSchema()` 的语义（dsh-tools/lib/index.js:800）。
 *
 * ★ 为什么必须转（2026-08-30 回读源码确认，此前降级路径是错的）：
 *   `ctx.tools.register(definition)` 只校验 `output.schema`
 *   （`assertSupportedJsonSchema`），**不编译 `parameters`**，原样存入 layer
 *   （同文件 register() 分支）。而下游读 `definition.parameters` 时期望的是
 *   **顶层 JSON Schema** —— 证据是 `run_code` 自己的 definition 就用一个
 *   getter 返回 `parameterSchemaSpecToJsonSchema({扁平DSL})`。
 *
 *   也就是说：
 *     走官方 defineTool    → 传扁平 DSL（它内部会转）
 *     走本地 fallback      → 必须自己转成顶层 JSON Schema 再交给 register
 *   降级路径此前直接透传扁平 DSL，等于把 `{file_path: {...}}` 当成
 *   `{type,properties,required}` 交给运行时 —— 参数校验与展示都会错。
 *
 * 转换规则：per-property 的 `required: true` 提升到根 `required` 数组，
 * 并从属性自身移除；根节点包成 `{type:'object', properties, required?}`。
 */
export function flatDslToJsonSchema(spec: Record<string, unknown>): Record<string, unknown> {
  const properties: Record<string, unknown> = {};
  const required: string[] = [];

  const convertNode = (node: Record<string, unknown>): Record<string, unknown> => {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(node)) {
      if (k !== 'required') out[k] = v;
    }
    // 嵌套 object 的 properties 里也可能有 per-property required，同样提升
    if (node.properties && typeof node.properties === 'object' && node.properties !== null) {
      const nested = node.properties as Record<string, Record<string, unknown>>;
      const nestedProps: Record<string, unknown> = {};
      const nestedReq: string[] = [];
      for (const [pk, pv] of Object.entries(nested)) {
        if (pv && typeof pv === 'object') {
          if ((pv as { required?: unknown }).required === true) nestedReq.push(pk);
          nestedProps[pk] = convertNode(pv as Record<string, unknown>);
        } else {
          nestedProps[pk] = pv;
        }
      }
      out.properties = nestedProps;
      if (nestedReq.length) out.required = nestedReq;
    }
    return out;
  };

  for (const [name, node] of Object.entries(spec)) {
    if (node && typeof node === 'object') {
      const rec = node as Record<string, unknown>;
      if (rec.required === true) required.push(name);
      properties[name] = convertNode(rec);
    } else {
      properties[name] = node;
    }
  }

  return {
    type: 'object',
    properties,
    ...(required.length ? { required } : {}),
  };
}

/**
 * 方案 B：本地 identity 适配。
 *
 * 与官方 defineTool 的唯一差别就是不做参数校验（register 也不做），
 * 因此必须自己把扁平 DSL 转成顶层 JSON Schema（见 flatDslToJsonSchema）。
 */
const defineToolFallback: DefineToolFn = (input) => ({
  ...input,
  parameters: flatDslToJsonSchema(input.parameters ?? {}),
});

interface ProbeResult {
  defineTool: DefineToolFn;
  mode: 'official' | 'fallback';
  reason: string;
}

/**
 * 探测官方 defineTool 是否可用。
 *
 * 判据不是「能否 require 到模块」，而是**能否用真实扁平 DSL 编译通过** ——
 * 只 require 成功但 schema 形态不匹配，仍会在注册时抛 JsonSchemaError。
 */
function probeDefineTool(): ProbeResult {
  const candidates = [
    '@deepseek-ai/dsh-tools',
    // 不再硬编码本机全局 dsh 的绝对路径；若裸模块名解析失败，
    // 会整体降级到「方案 B：本地 identity（无参数校验）」，见文件头说明。
  ];

  for (const spec of candidates) {
    try {
      const mod = nodeRequire(spec) as { defineTool?: unknown };
      if (typeof mod?.defineTool !== 'function') continue;

      const official = mod.defineTool as DefineToolFn;
      // ★ 用与真实工具同构的扁平 DSL 做冒烟编译
      official({
        name: '__probe__',
        description: 'probe',
        parameters: { file_path: { type: 'string', required: true, description: 'p' } },
        output: {
          schema: ENVELOPE_OUTPUT_SCHEMA as unknown as Record<string, unknown>,
          render: () => [],
        },
        execute: async () => ({}),
      });
      return { defineTool: official, mode: 'official', reason: `已加载 ${spec}` };
    } catch (e) {
      continue;
    }
  }
  return {
    defineTool: defineToolFallback,
    mode: 'fallback',
    reason: '官方 dsh-tools 不可用或 schema 形态不兼容，已降级为本地 identity（无参数校验）',
  };
}

// ---------------------------------------------------------------------------
// Service
// ---------------------------------------------------------------------------

/**
 * 插件可配项。名字与 index.ts 的 `apply(ctx, config?)` 形参类型一致。
 * 刻意**不导出 `Config` 常量** —— 非 schemastery 对象会让 cordis loader
 * 启动即崩（架构 §2.1）。所有项都可选，缺省值见下方 DEFAULTS。
 */
export interface CompToolConfig {
  pythonExe?: string;
  workerScript?: string;
  projectRoot?: string;
  logger?: (...args: unknown[]) => void;
}

/** 默认值与 config.yaml 的 paths 段保持一致 */
const DEFAULTS = {
  pythonExe: 'C:/ProgramData/anaconda3/python.exe',
  // 默认项目根 = 本插件仓库根目录（相对 service.ts 向上 4 级：
  // src/plugins/comp-tool/src → repo root；编译到 lib/ 后同样是向上 4 级）。
  projectRoot: path.resolve(__dirname, '../../../..'),
  workerScript: 'src/tools/server.py',
};

export class CompToolService {
  private bridge: PythonBridge;
  private defineTool: DefineToolFn;
  private mode: ProbeResult['mode'];
  private reason: string;

  constructor(private ctx: PluginContext, config?: CompToolConfig) {
    const projectRoot = config?.projectRoot ?? DEFAULTS.projectRoot;
    const workerScript = config?.workerScript ?? `${projectRoot}/${DEFAULTS.workerScript}`;

    this.bridge = new PythonBridge({
      pythonExe: config?.pythonExe ?? DEFAULTS.pythonExe,
      workerScript,
      projectRoot,
      logger: config?.logger,
    });

    const probe = probeDefineTool();
    this.defineTool = probe.defineTool;
    this.mode = probe.mode;
    this.reason = probe.reason;
    config?.logger?.('[comp-tool] defineTool 模式：', probe.mode, '—', probe.reason);
  }

  /** 注册 11 个工具；返回注册数量（便于自测断言）。 */
  register(): number {
    for (const meta of TOOL_METAS) {
      this.ctx.tools.register(this.buildTool(meta));
    }

    // 卸载时优雅关闭 worker（架构 §5.2「关闭」项）
    this.ctx.on?.('dispose', () => {
      void this.bridge.dispose();
    });

    return TOOL_METAS.length;
  }

  private buildTool(meta: ToolMeta): unknown {
    const { bridge } = this;
    return this.defineTool({
      name: meta.name,
      description: meta.description,
      parameters: meta.parameters,
      output: {
        schema: ENVELOPE_OUTPUT_SCHEMA as unknown as Record<string, unknown>,
        render: (_args, value) => renderEnvelope(_args, value),
        presentationMeta: envelopeMeta(meta.name),
      },
      isConcurrencySafe: meta.isConcurrencySafe,
      // ★ 架构 §5.2：timeoutMs 仅作**文档性元数据**，实际超时由 bridge 与
      //   sandbox.py 强制执行（dsh-tools 官方不会强制执行它）。
      timeoutMs: meta.timeoutMs,
      async execute(args: Record<string, unknown>, exec: ToolExec): Promise<Envelope> {
        // session_id 取值优先级：工具显式传参 > agent 会话 ID
        const sessionId =
          (typeof args.session_id === 'string' && args.session_id) ||
          exec?.agent?.sessionId ||
          undefined;

        const result = (await bridge.request(
          'tools/call',
          {
            name: meta.name,
            arguments: args,
            ...(sessionId ? { session_id: sessionId } : {}),
          },
          meta.timeoutMs,
        )) as Envelope | undefined;

        // worker 侧已统一信封；若拿不到则兜底成一个合法失败信封，
        // 保证 output.schema 校验不会因为字段缺失而失败。
        if (!result || typeof result !== 'object') {
          return {
            ok: false,
            code: 'INTERNAL_ERROR',
            message: `${meta.name} 未返回有效结果。`,
            hint: '请检查 Python worker 是否正常运行（见插件日志）。',
          };
        }
        return result;
      },
    });
  }

  /**
   * 诊断信息：注册模式、worker 状态。
   *
   * ★ 类型写法教训：曾写成
   *     `ReturnType<PythonBridge['stats'] extends () => infer R ? () => R : never>`
   *   但 `stats` 是 **getter 属性**（返回对象）而非方法，条件类型走 false 分支
   *   塌成 `never`，`ReturnType<never>` 仍是 never —— 类型层面直接失守。
   *   直接用 `PythonBridge['stats']` 取属性类型才是正确写法。
   */
  getStatus(): {
    mode: ProbeResult['mode'];
    reason: string;
    bridge: PythonBridge['stats'];
    toolCount: number;
  } {
    return {
      mode: this.mode,
      reason: this.reason,
      bridge: this.bridge.stats,
      toolCount: TOOL_METAS.length,
    };
  }
}
