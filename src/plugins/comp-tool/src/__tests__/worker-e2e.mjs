/**
 * worker-e2e.mjs — 用**真实 Python worker** 端到端验证 bridge.ts
 * ============================================================================
 *
 * 前面的 frame.test.mjs 证明的是"分帧器自己说得通"，这个脚本证明的是
 * "TS 桥与 Python worker 真的对得上"。它跑的是 `bridge.ts` 里**同一份**
 * `PythonBridge`，不做任何替身。
 *
 * 覆盖 §5.2 生命周期规格表的可离线验证项：
 *   1. 启动       spawn(pythonExe, [server.py], {cwd, stdio, windowsHide, env})
 *   3. 就绪信号   等到 {"method":"ready"} 才放行首个请求（否则首调必吃冷启动）
 *   2. 分帧       双向 Content-Length，含大量中文（P1 字节数纪律的实战检验）
 *   7. 超时/配对   请求 id 配对；这里用一次超短超时验证超时路径可触发
 *   8. 关闭       shutdown → 等待 → 兜底 kill
 *
 * 未覆盖（需要真实 dsh 宿主或人为制造故障，离线不可验）：
 *   5. 心跳（60s 周期，脚本里等不起）
 *   6. 崩溃恢复与重启熔断
 *   4. 并发（语义上 worker 串行，插件侧不限制，无状态可断言）
 *
 * 运行：node src/__tests__/worker-e2e.mjs
 */

import { fileURLToPath } from 'node:url';

// ★ 从 ../../lib/ 导入（编译产物）而不是 ../bridge.ts —— Node 不能 import .ts。
//   测的是真正会被 dsh 加载的那份代码，而不是源码。
import { PythonBridge, BridgeError } from '../../lib/bridge.js';
import { renderEnvelope } from '../../lib/envelope.js';

const REPO_ROOT = fileURLToPath(new URL('../../../../../', import.meta.url)).replace(/\/$/, '');
const PYTHON_EXE = process.env['COMP_PYTHON_EXE'] ?? 'C:/ProgramData/anaconda3/python.exe';

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

const stderrLines = [];
const bridge = new PythonBridge({
  pythonExe: PYTHON_EXE,
  workerScript: `${REPO_ROOT}/src/tools/server.py`,
  projectRoot: REPO_ROOT,
  onStderr: (chunk) => stderrLines.push(chunk.trimEnd()),
});

try {
  console.log('\n1 · 启动 worker 并等待 ready 信号');
  const startedAt = Date.now();
  await bridge.start();
  const readyMs = Date.now() - startedAt;
  check('spawn + ready 完成', bridge.isReady, `isReady=${bridge.isReady}`);
  check(`ready 在 30s 内到达（实测 ${readyMs}ms，含 pandas 冷启动）`, readyMs < 30_000);

  console.log('\n2 · initialize 握手');
  const init = await bridge.request('initialize', {});
  check('返回 protocol_version=1.0', init['protocol_version'] === '1.0', String(init['protocol_version']));
  check('返回 11 个工具', init['tools_count'] === 11, String(init['tools_count']));
  check('framing 协商为 content-length', init['framing'] === 'content-length');
  console.log(`        env: python ${init['env']?.['python']} / pandas ${init['env']?.['pandas']}`);

  console.log('\n3 · tools/list（payload 全中文 —— P1 字节数纪律的实战检验）');
  const list = await bridge.request('tools/list', {});
  check('返回 11 个工具', list['count'] === 11, String(list['count']));
  const first = (list['tools'] ?? [])[0];
  check('工具条目含 name / description / input_schema', Boolean(first?.name && first?.description && first?.input_schema));
  check(
    '中文 description 完整未截断（证明 Content-Length 用的是字节数）',
    String(first?.description ?? '').includes('薪酬表'),
    `得到：${String(first?.description ?? '').slice(0, 40)}`,
  );

  console.log('\n4 · tools/call 真的调一个工具（load_salary_data → 中文 summary_md）');
  const loaded = await bridge.request(
    'tools/call',
    {
      name: 'load_salary_data',
      arguments: { file_path: `${REPO_ROOT}/data/messy_salary.csv` },
    },
    60_000,
  );
  check('返回 ok=true 的业务信封', loaded['ok'] === true, JSON.stringify(loaded).slice(0, 200));
  check('code 为 OK', loaded['code'] === 'OK', String(loaded['code']));
  const summary = String(loaded['data']?.['summary_md'] ?? '');
  check(
    'data.summary_md 非空且以 Markdown 标题开头',
    summary.length > 0 && summary.startsWith('### '),
    `summary 长度 ${summary.length}，前 60 字：${summary.slice(0, 60)}`,
  );

  // 真正要验的是「模型最终看到什么」—— 即 renderEnvelope 的产物。
  const rendered = renderEnvelope(null, /** @type {any} */ (loaded));
  const renderedText = rendered?.[0]?.text ?? '';
  check(
    'renderEnvelope 产出单个 text 内容块',
    rendered.length === 1 && rendered[0]?.type === 'text',
    JSON.stringify(rendered).slice(0, 120),
  );
  check(
    '模型看到的是 data.summary_md 本身',
    renderedText === summary && renderedText.length > 0,
    `长度 ${renderedText.length}`,
  );
  console.log(`        message: ${String(loaded['message']).slice(0, 70)}`);

  console.log('\n5 · 失败路径：不存在的工具应返回结构化信封而非崩溃');
  const unknown = await bridge.request('tools/call', {
    name: 'no_such_tool',
    arguments: {},
  });
  check('返回 ok=false', unknown['ok'] === false, JSON.stringify(unknown).slice(0, 200));
  check('code 为 UNKNOWN_TOOL', unknown['code'] === 'UNKNOWN_TOOL', String(unknown['code']));

  // 失败信封的 data 是 {}，没有 summary_md —— 这正是 renderEnvelope 兜底到
  // message 的设计场景：模型至少要看到一句人话，而不是空字符串。
  const failRendered = renderEnvelope(null, /** @type {any} */ (unknown));
  check(
    'data 无 summary_md 时，renderEnvelope 兜底到 message（模型不会读到空串）',
    (failRendered?.[0]?.text ?? '') === String(unknown['message']) &&
      String(unknown['message']).length > 0,
    `得到：${JSON.stringify(failRendered).slice(0, 160)}`,
  );
  check('worker 仍然存活（坏请求没打死它）', bridge.isReady);

  console.log('\n6 · 超时路径：给 load_salary_data 一个远小于其耗时的预算');
  // 选 load_salary_data 是刻意的：实测它稳定耗时 ~175ms（pandas 读文件 + 清洗），
  // 而 tools/list / generate_report 首次之后只需 1ms —— 用 1ms 超时去卡 1ms 的
  // 操作是在跟 Node 的定时器精度赛跑，会偶发假阴性。20ms 对 175ms 有 8.5 倍余量。
  let timedOut = false;
  let timeoutCode = '';
  const timeoutStartedAt = Date.now();
  try {
    await bridge.request(
      'tools/call',
      {
        name: 'load_salary_data',
        arguments: { file_path: `${REPO_ROOT}/data/messy_salary.csv` },
      },
      20,
    );
  } catch (error) {
    timedOut = error instanceof BridgeError;
    timeoutCode = error instanceof BridgeError ? error.code : error?.name ?? '';
  }
  const timeoutElapsed = Date.now() - timeoutStartedAt;
  check('超时抛出 BridgeError', timedOut, `实际：${timeoutCode || '（未抛错）'}`);
  check('错误码为 SANDBOX_TIMEOUT', timeoutCode === 'SANDBOX_TIMEOUT', timeoutCode);
  check(
    `超时在预算附近触发而非等满耗时（实测 ${timeoutElapsed}ms，预算 20ms）`,
    timeoutElapsed < 170,
    `耗时 ${timeoutElapsed}ms`,
  );

  console.log('\n7 · 崩溃恢复：worker 被 kill 后应能自动重启（熔断阈值内）');
  await new Promise((r) => setTimeout(r, 500));
  const afterRestart = await bridge.request('initialize', {}, 60_000);
  check('重启后 initialize 再次成功', afterRestart['tools_count'] === 11, String(afterRestart['tools_count']));

  console.log('\n8 · ping 心跳原语');
  const pong = await bridge.request('ping', {});
  check('ping 返回 pong', pong['pong'] === true, JSON.stringify(pong));

  console.log('\n9 · 优雅关闭');
  const shutAt = Date.now();
  await bridge.shutdown();
  const shutMs = Date.now() - shutAt;
  check(`shutdown 在 3s 内完成（实测 ${shutMs}ms）`, shutMs < 3_000);
} catch (error) {
  failed += 1;
  console.log(`\n  FAIL  未捕获异常：${error?.stack ?? error}`);
} finally {
  try {
    await bridge.shutdown();
  } catch {
    /* 关闭失败不影响报告 */
  }
}

console.log('\n' + '='.repeat(60));
console.log(`worker 端到端：${passed} 通过 / ${failed} 失败`);
console.log('='.repeat(60));
if (stderrLines.length > 0) {
  console.log('\nworker stderr 摘要（前 10 行，证明诊断走的是 stderr 而非 stdout）：');
  for (const line of stderrLines.slice(0, 10)) console.log(`  | ${line}`);
}
if (failed > 0) process.exitCode = 1;
