/**
 * definetool-probe.mjs — 用**真正的官方 defineTool**编译全部 11 个参数表
 * ============================================================================
 *
 * 为什么必须做这一步（而不是只跑 tsc）：
 *   TypeScript 只能证明「这是合法的 TS」，证明不了「dsh 的 defineTool 肯收」。
 *   dsh-tools 的 value-schema DSL 有一套**运行期**约束（object 必须带布尔
 *   additionalProperties、白名单外关键字一律拒绝、output.schema 不支持顶层
 *   required、开放节点必须 type:'json'……），违反时是在**插件加载那一刻**
 *   抛 JsonSchemaError —— 也就是 dsh 起不来。
 *
 *   所以：把 11 份真实参数表逐个喂进官方 defineTool，让它在加载前就报错。
 *   这是 T5.2「11 个工具注册」从"写完了"变成"证明能注册"的那一脚。
 *
 * 判据不是 require 成功 —— 是**每个工具都真的定义出一个可执行对象**。
 *
 * 运行：node src/__tests__/definetool-probe.mjs
 */

import { createRequire } from 'node:module';

import { TOOL_METAS } from '../../lib/schemas.js';
import { ENVELOPE_OUTPUT_SCHEMA, renderEnvelope, envelopeMeta } from '../../lib/envelope.js';
import { flatDslToJsonSchema } from '../../lib/service.js';

const require = createRequire(import.meta.url);

/** 稳定的深比较（键序无关）。 */
function stable(v) {
  if (Array.isArray(v)) return v.map(stable);
  if (v && typeof v === 'object') {
    return Object.keys(v)
      .sort()
      .reduce((acc, k) => {
        acc[k] = stable(v[k]);
        return acc;
      }, {});
  }
  return v;
}
function sameJson(a, b) {
  return JSON.stringify(stable(a)) === JSON.stringify(stable(b));
}

let passed = 0;
let failed = 0;

function check(name, condition, detail = '') {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL  ${name}${detail ? ` — ${detail}` : ''}`);
  }
}

// ---------------------------------------------------------------------------
console.log('\n0 · 加载官方 dsh-tools');
// ---------------------------------------------------------------------------
let defineTool = null;
let loadNote = '';
for (const spec of [
  '@deepseek-ai/dsh-tools',
  // 不再硬编码本机全局 dsh 的绝对路径；解析失败即视为不可用，走降级分支。
]) {
  try {
    const mod = require(spec);
    if (typeof mod.defineTool === 'function') {
      defineTool = mod.defineTool;
      loadNote = spec;
      break;
    }
  } catch (e) {
    /* 换下一个候选 */
  }
}

if (defineTool === null) {
  console.log('  SKIP  本机未装 @deepseek-ai/dsh-tools，无法做真实编译验证。');
  console.log('        （不是失败，但意味着这一关没被证明过 —— 见 run_skill 提示）');
  process.exitCode = 0;
} else {
  console.log(`  已加载：${loadNote}`);

  // -------------------------------------------------------------------------
  console.log(`\n1 · 逐个编译 ${TOOL_METAS.length} 个工具的参数表`);
  // -------------------------------------------------------------------------
  for (const meta of TOOL_METAS) {
    let err = null;
    let built = null;
    try {
      built = defineTool({
        name: meta.name,
        title: meta.title,
        description: meta.description,
        parameters: meta.parameters,
        output: {
          schema: ENVELOPE_OUTPUT_SCHEMA,
          render: (_a, v) => renderEnvelope(_a, v),
          presentationMeta: envelopeMeta(meta.name),
        },
        isConcurrencySafe: meta.isConcurrencySafe,
        timeoutMs: meta.timeoutMs,
        execute: async () => ({}),
      });
    } catch (e) {
      err = e;
    }

    const nParams = Object.keys(meta.parameters ?? {}).length;
    if (err === null && built !== null && typeof built === 'object') {
      check(
        `${meta.name.padEnd(20)} 编译通过（${String(nParams).padStart(2)} 参数，` +
          `${(meta.timeoutMs / 1000).toFixed(0)}s，并发安全=${meta.isConcurrencySafe()}）`,
        true,
      );
    } else {
      check(
        `${meta.name.padEnd(20)} 编译通过`,
        false,
        err ? `${err.name}: ${String(err.message).slice(0, 200)}` : 'defineTool 未返回对象',
      );
    }
  }

  // -------------------------------------------------------------------------
  console.log('\n1b · 降级路径：我的 flatDslToJsonSchema 必须与官方编译器等价');
  // -------------------------------------------------------------------------
  // 为什么这一节最关键：官方 defineTool 会自己把扁平 DSL 编译成顶层 JSON Schema
  // 存进 definition.parameters；而 ctx.tools.register **不编译 parameters**。
  // 所以本地降级路径必须自己转 —— 转得对不对，唯一可信的判据就是"和官方产物一致"。
  for (const meta of TOOL_METAS) {
    const official = defineTool({
      name: meta.name,
      description: meta.description,
      parameters: meta.parameters,
      output: { schema: ENVELOPE_OUTPUT_SCHEMA, render: () => [] },
      execute: async () => ({}),
    });
    const mine = flatDslToJsonSchema(meta.parameters);
    check(
      `${meta.name.padEnd(20)} 降级转换 == 官方编译产物`,
      sameJson(official.parameters, mine),
      `差异 → 官方 ${JSON.stringify(stable(official.parameters)).slice(0, 160)}` +
        ` / 我方 ${JSON.stringify(stable(mine)).slice(0, 160)}`,
    );
  }

  // -------------------------------------------------------------------------
  console.log('\n2 · 反例：违规 schema 必须被拒（证明这个闸门真的有牙齿）');
  // -------------------------------------------------------------------------
  // 若下面这些"明知违规"的写法也能编译通过，说明上面的 PASS 全是假阳性。
  const badCases = [
    {
      label: 'object 缺 additionalProperties',
      parameters: { mapping: { type: 'object', description: 'x' } },
      expect: /additionalProperties/i,
    },
    {
      label: "additionalProperties 传 schema 而非布尔",
      parameters: { mapping: { type: 'object', additionalProperties: { type: 'string' } } },
      expect: /additionalProperties/i,
    },
    {
      label: '白名单外关键字 minimum',
      parameters: { budget: { type: 'number', minimum: 0 } },
      expect: /minimum/i,
    },
    {
      label: '开放节点写成裸 {}',
      parameters: { any: { type: 'object', additionalProperties: true, properties: {} } },
      expect: null, // 这条不一定报错，只记录实际行为
    },
  ];

  for (const c of badCases) {
    let err = null;
    try {
      defineTool({
        name: '__bad__',
        description: 'bad',
        parameters: c.parameters,
        output: { schema: ENVELOPE_OUTPUT_SCHEMA, render: () => [] },
        execute: async () => ({}),
      });
    } catch (e) {
      err = e;
    }
    if (c.expect === null) {
      console.log(
        `  INFO  ${c.label} → ${err === null ? '未报错（可接受）' : `报错：${String(err.message).slice(0, 80)}`}`,
      );
    } else {
      check(
        `违规被拒：${c.label}`,
        err !== null && c.expect.test(String(err.message)),
        err === null ? '违规写法竟然通过了 —— 闸门无效' : `实际错误：${String(err.message).slice(0, 120)}`,
      );
    }
  }

  // -------------------------------------------------------------------------
  console.log('\n3 · output.schema 的两处实测约束（ARCHITECTURE §4.0 的修正项）');
  // -------------------------------------------------------------------------
  let requiredErr = null;
  try {
    defineTool({
      name: '__req__',
      description: 'x',
      parameters: { a: { type: 'string' } },
      output: {
        schema: { type: 'object', properties: { ok: { type: 'boolean' } }, required: ['ok'] },
        render: () => [],
      },
      execute: async () => ({}),
    });
  } catch (e) {
    requiredErr = e;
  }
  check(
    'output.schema 顶层 required 确实不被支持（证明文件头注释不是臆断）',
    requiredErr !== null && /required/.test(String(requiredErr.message)),
    requiredErr === null ? '顶层 required 竟然通过了 —— schemas 头部注释需更正' : '',
  );

  let openErr = null;
  try {
    defineTool({
      name: '__open__',
      description: 'x',
      parameters: { a: { type: 'string' } },
      output: { schema: { type: 'object', properties: { data: {} } }, render: () => [] },
      execute: async () => ({}),
    });
  } catch (e) {
    openErr = e;
  }
  check(
    '开放节点裸 {} 确实不被支持（必须写 type:"json"）',
    openErr !== null,
    openErr === null ? '裸 {} 竟然通过了 —— envelope.ts 注释需更正' : '',
  );
}

console.log(`\n${'='.repeat(60)}`);
console.log(`官方 defineTool 编译验证：${passed} 通过 / ${failed} 失败`);
console.log('='.repeat(60));
if (failed > 0) process.exitCode = 1;
