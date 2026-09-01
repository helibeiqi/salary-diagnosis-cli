/**
 * envelope.ts — 统一的返回信封 + output.schema + render + presentationMeta
 * ============================================================================
 *
 * 为什么 11 个工具共用**一个**泛型 schema（架构 §2.4 裁决）：
 *   `data` 的真实形态随数据完整度变化（有无市场数据、有无带宽、有无调薪结果），
 *   为每种形态写深层 schema 会撞上 dsh-tools 的关键字白名单，且必然与实际不符。
 *   泛型 + description 描述 = 更诚实。
 *
 * ⚠️ 本节所有约束均经 2026-08-30 对 dsh-tools v0.1.1-rc.2 实测确认，
 *    与 ARCHITECTURE.md §4.0 的原始提案**有两处冲突**，以实测为准：
 *
 * | 项 | 架构原提案 | 实测结论 |
 * |---|---|---|
 * | `required: ['ok','code','message']` | 支持（CONSTRAINT_KEYWORDS 含 required） | ❌ 抛 `schema.required is not supported by the value schema DSL`。原因：output.schema 走 `valueSchemaSpecToJsonSchema()`，不是 parameters 的 `parameterSchemaSpecToJsonSchema()`，前者不支持顶层 required |
 * | 开放节点 `data: {}` | 支持 | ❌ 抛 `schema.properties.data.type must be string/.../json`。开放节点必须写 `type:'json'`（编译后才变成 `{}`） |
 *
 * 另：`additionalProperties` 必须显式 true/false，缺失即报错。
 */

/** Python 侧 errors.py 返回的统一信封 */
export interface Envelope {
  ok: boolean;
  code: string;
  message: string;
  hint?: string;
  detail?: string;
  data?: Record<string, unknown>;
  artifacts?: unknown[];
  charts?: unknown[];
  warnings?: string[];
  meta?: Record<string, unknown>;
}

/**
 * 11 个工具共用的 output.schema。
 *
 * ★ 不得加顶层 `required` —— value schema DSL 不支持（见文件头）。
 * ★ 开放节点一律 `type:'json'` —— 裸 `{}` 不合法。
 */
export const ENVELOPE_OUTPUT_SCHEMA = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    code: { type: 'string' },
    message: { type: 'string' },
    hint: { type: 'string' },
    detail: { type: 'string' },
    data: { type: 'json' },
    artifacts: { type: 'array', items: { type: 'json' } },
    charts: { type: 'array', items: { type: 'json' } },
    warnings: { type: 'array', items: { type: 'string' } },
    meta: { type: 'json' },
  },
  additionalProperties: false,
} as const;

/** 工具中文名 → 用于 presentationMeta 的标题 */
export function toolTitle(toolName: string): string {
  return TOOL_TITLES[toolName] ?? toolName;
}

const TOOL_TITLES: Record<string, string> = {
  load_salary_data: '加载薪酬数据',
  confirm_mapping: '确认字段映射',
  desensitize_data: '数据脱敏',
  analyze_current_state: '薪酬现状诊断',
  generate_band: '薪酬带宽设计',
  market_benchmark: '市场对标',
  simulate_increase: '调薪模拟',
  simulate_pay_mix: '固浮比模拟',
  calc_job_score: '岗位价值评估',
  generate_report: '生成诊断报告',
  run_comp_code: '运行薪酬分析代码',
};

/**
 * render：把信封渲染成模型可见的文本块 —— **严格透传，不得加工**。
 *
 * 契约（ARCHITECTURE.md:413）：
 *     text = envelope.data?.summary_md ?? envelope.message
 *
 * ★ 为什么不做任何拼接（2026-08-30 修正，此前版本曾自行追加 hint/detail/warnings）：
 *   1. 两条链路一致性（ARCHITECTURE.md:1063）—— `summary_md` 由 **Python 侧**
 *      生成，正是为了让 `main.py` 本地路径与 dsh 路径**呈现完全一致**，
 *      从而「QA 一次验证覆盖两条链路」。TS 侧一旦追加任何内容，dsh 链路就
 *      比本地链路多出一截，这个保证立刻失效 —— 而这正是它被设计出来的目的。
 *   2. 架构边界纪律（ARCHITECTURE.md:880）—— TS 侧只允许做四件事：
 *      参数透传、调用 worker、把 summary_md 变成 content、错误兜底。
 *      拼接文案属于"渲染逻辑"，越界。
 *
 * ⇒ 因此：想让模型看到警告/修复建议，正确做法是让 **Python 侧**把它们写进
 *   `summary_md` 或 `message`，而不是在这里补。
 */
export function renderEnvelope(
  _args: Record<string, unknown>,
  value: unknown,
): Array<{ type: 'text'; text: string }> {
  const env = value as Partial<Envelope> | null | undefined;
  if (!env || typeof env !== 'object') {
    return [{ type: 'text', text: '工具未返回有效信封。' }];
  }

  const summary = (env.data as { summary_md?: unknown } | undefined)?.summary_md;
  if (typeof summary === 'string' && summary.trim()) {
    return [{ type: 'text', text: summary }];
  }
  // 失败信封的 data 通常是 {}，没有 summary_md —— 兜底到 message，
  // 保证模型至少读到一句人话而不是空串。
  if (typeof env.message === 'string' && env.message.trim()) {
    return [{ type: 'text', text: env.message }];
  }
  return [{ type: 'text', text: '（无输出）' }];
}

/** presentationMeta：UI 上显示的标题 */
export function envelopeMeta(
  toolName: string,
): (_args: Record<string, unknown>, value: unknown) => Record<string, unknown> {
  return (_args, value) => {
    const env = value as Partial<Envelope> | null | undefined;
    if (env && env.ok === false) {
      return { title: `${toolTitle(toolName)} · 失败（${env.code ?? 'UNKNOWN'}）` };
    }
    return { title: toolTitle(toolName) };
  };
}
