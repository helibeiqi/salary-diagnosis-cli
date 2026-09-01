/**
 * params-alignment.mjs — TS parameters DSL 与 Python registry.py 的逐字段对齐检查
 * ============================================================================
 *
 * 为什么需要这个脚本
 * ---------------------------------------------------------------------------
 * `src/tools/registry.py` 是 11 个工具参数的**权威源**，`src/schemas.ts` 是它的
 * TS 投影。两边靠人工同步，久了必然漂移 —— 而漂移的表现是"模型传了参数但工具
 * 收不到"，排查成本极高。这个脚本把"对齐"变成一条可重复执行的断言。
 *
 * 比较什么
 * ---------------------------------------------------------------------------
 * 1. 工具名集合（双向差集）
 * 2. 每个工具的**参数名集合**（双向差集）—— 这是核心
 * 3. `required` 标记（TS 侧 `required: true` vs Python 侧 input_schema.required）
 *
 * 刻意**不**比较 type/enum/description：见 schemas.ts 文件头，DSL 与原始 JSON
 * Schema 在 `additionalProperties` 形态、`required` 位置上存在被类型系统强制的
 * 差异，逐类型比较会产生大量噪音而非信号。
 *
 * 运行：node src/__tests__/params-alignment.mjs
 */

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { resolve as resolvePath } from 'node:path';

// ★ 从 ../../lib/ 导入（编译产物）而不是 ../schemas.ts —— Node 不能 import .ts。
//   早先写 '../schemas.ts' 的测试从未真正执行过。测试要验的是交付物。
import { TOOL_METAS } from '../../lib/schemas.js';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url));
const PYTHON_EXE = process.env['COMP_PYTHON_EXE'] ?? 'C:/ProgramData/anaconda3/python.exe';

// ---------------------------------------------------------------------------
// Python 侧：直接调 registry.list_tools()，不读缓存文件，保证比较的是活数据
// ---------------------------------------------------------------------------
const pythonSource = `
import json, sys
sys.path.insert(0, ${JSON.stringify(REPO_ROOT)})
from src.tools.registry import list_tools
out = []
for t in list_tools():
    out.append({
        "name": t["name"],
        "parameters": sorted(t["parameters"].keys()),
        "required": sorted(t["input_schema"].get("required", [])),
    })
print(json.dumps(out, ensure_ascii=False))
`;

let pyTools;
try {
  const raw = execFileSync(PYTHON_EXE, ['-c', pythonSource], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  pyTools = JSON.parse(raw.trim());
} catch (error) {
  console.error('无法从 registry.py 读取工具定义：', error.message);
  if (error.stderr) console.error(error.stderr);
  process.exitCode = 1;
  throw error;
}

// ---------------------------------------------------------------------------
// TS 侧：从 schemas.ts 的导出里按命名约定收集
// ---------------------------------------------------------------------------
// 直接取 TOOL_METAS —— 它是生成器产出的工具清单（name / parameters / timeoutMs /
// isConcurrencySafe），比按导出名约定去反推稳健得多：导出名改了也不会误伤这里。
const tsTools = (TOOL_METAS ?? []).map((meta) => {
  const keys = Object.keys(meta.parameters ?? {});
  return {
    name: meta.name,
    parameters: keys.slice().sort(),
    // DSL 的 required 写在**单个属性上**（per-property），不是顶层数组
    required: keys.filter((k) => meta.parameters?.[k]?.required === true).sort(),
  };
});

// ---------------------------------------------------------------------------
// 比较
// ---------------------------------------------------------------------------
const pyByName = new Map(pyTools.map((t) => [t.name, t]));
const tsByName = new Map(tsTools.map((t) => [t.name, t]));

let problems = 0;

function diffList(a, b) {
  const onlyA = a.filter((x) => !b.includes(x));
  const onlyB = b.filter((x) => !a.includes(x));
  return { onlyA, onlyB };
}

console.log(`Python registry.py：${pyByName.size} 个工具`);
console.log(`TS schemas.ts    ：${tsByName.size} 个工具`);
console.log('');

const toolDiff = diffList([...pyByName.keys()], [...tsByName.keys()]);
if (toolDiff.onlyA.length > 0) {
  console.log(`  ✗ 只在 Python 侧存在：${toolDiff.onlyA.join(', ')}`);
  problems += toolDiff.onlyA.length;
}
if (toolDiff.onlyB.length > 0) {
  console.log(`  ✗ 只在 TS 侧存在：${toolDiff.onlyB.join(', ')}`);
  problems += toolDiff.onlyB.length;
}
if (toolDiff.onlyA.length === 0 && toolDiff.onlyB.length === 0) {
  console.log('  ✓ 工具名集合一致');
}

console.log('\n逐工具参数名 diff（应全部为空）：');
for (const name of [...pyByName.keys()].sort()) {
  const py = pyByName.get(name);
  const ts = tsByName.get(name);
  if (ts === undefined) {
    console.log(`  ${name.padEnd(22)} SKIP（TS 侧缺失）`);
    continue;
  }
  const d = diffList(py.parameters, ts.parameters);
  const r = diffList(py.required, ts.required);

  const paramOk = d.onlyA.length === 0 && d.onlyB.length === 0;
  const reqOk = r.onlyA.length === 0 && r.onlyB.length === 0;

  if (paramOk && reqOk) {
    console.log(
      `  ${name.padEnd(22)} ✓ 参数 ${String(ts.parameters.length).padStart(2)} 个，` +
        `必填 ${ts.required.length} 个 — diff 为空`,
    );
  } else {
    problems += 1;
    if (!paramOk) {
      if (d.onlyA.length > 0) console.log(`  ${name.padEnd(22)} ✗ 仅 Python 有：${d.onlyA.join(', ')}`);
      if (d.onlyB.length > 0) console.log(`  ${name.padEnd(22)} ✗ 仅 TS 有：${d.onlyB.join(', ')}`);
    }
    if (!reqOk) {
      if (r.onlyA.length > 0) console.log(`  ${name.padEnd(22)} ✗ 仅 Python 标记必填：${r.onlyA.join(', ')}`);
      if (r.onlyB.length > 0) console.log(`  ${name.padEnd(22)} ✗ 仅 TS 标记必填：${r.onlyB.join(', ')}`);
    }
  }
}

console.log('\n' + '='.repeat(60));
console.log(
  problems === 0
    ? `参数对齐：通过（${tsByName.size} 个工具，参数名与 required 标记 diff 全为空）`
    : `参数对齐：失败（${problems} 处不一致）`,
);
console.log('='.repeat(60));
if (problems > 0) process.exitCode = 1;

// 供其他脚本使用
export { pyTools, tsTools };
void resolvePath;
