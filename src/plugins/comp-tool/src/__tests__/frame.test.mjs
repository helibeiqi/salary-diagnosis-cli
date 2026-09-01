/**
 * frame.test.mjs — FrameDecoder 离线单测（不依赖 Python）
 * ============================================================================
 *
 * 覆盖架构文档 §5.2.1（ARCHITECTURE.md:944-951）表格里的**全部 4 类边界情况**：
 *
 *   C1 帧前单行垃圾（`WARNING:xxx\n`）     → 垃圾落在 header 区被忽略，帧正常解析
 *   C2 多行垃圾（traceback，自带空行）     → 检测不到 Content-Length → 丢弃 → 下轮命中真帧
 *   C3 声明长度超限（payload-too-large）   → 丢帧 + 诊断，不缓冲
 *   C4 半包 / 粘包（1.5 帧、3 帧、逐字节） → 缓冲拼接后逐帧切出，跨 feed 保持状态
 *
 * 另外单列两组：
 *   P1 中文字节数   Content-Length 必须是 UTF-8 字节数而非字符数
 *   P4 无分隔符垃圾 证明不会无限缓冲（否则插件表现为卡死）
 *
 * ★ 导入的是 `../../lib/bridge.js`（**编译产物**，即真正交付给 dsh 的东西），
 *   不是 `../bridge.ts`。Node 无法直接 import .ts，早先写 `from '../bridge.ts'`
 *   的测试从未真正执行过 —— 测试必须验你交付的东西，不是验你的源码。
 *   因此 `npm test` 会先跑 build。
 *
 * 运行：npm test（或 npm run build && node src/__tests__/frame.test.mjs）
 */

import { FrameDecoder } from '../../lib/bridge.js';

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

/** 按协议编码一帧。注意用 Buffer.byteLength（UTF-8 字节数），即纪律 P1。 */
function encodeFrame(text) {
  const body = Buffer.from(text, 'utf8');
  const header = Buffer.from(`Content-Length: ${body.byteLength}\r\n\r\n`, 'ascii');
  return Buffer.concat([header, body]);
}

/**
 * 在真实 API（`feed()` 一次返回全部消息）之上包一层 `push/next` 的外壳。
 *
 * 为什么这么包：feed() 是给流式 data 事件用的（一次可能吐多帧，返回数组最自然），
 * 但逐帧断言时用 next() 写出来最好读。外壳只做队列化，不改变被测逻辑。
 * diagnostics 跨多次 feed 累积 —— 断言"丢弃行为留下了痕迹"需要这个。
 */
function decoder(options = {}) {
  const diagnostics = [];
  const dec = new FrameDecoder(options);
  const queue = [];
  return {
    dec,
    diagnostics,
    push(chunk) {
      const r = dec.feed(chunk);
      diagnostics.push(...r.diagnostics);
      queue.push(...r.messages);
      return r;
    },
    /** 取下一条已解出的消息；没有则 undefined（表示"还差字节"） */
    next() {
      return queue.shift();
    },
    take() {
      const all = queue.splice(0, queue.length);
      return all;
    },
    get bufferedBytes() {
      return dec.bufferedBytes;
    },
  };
}

/** feed() 返回的是 JSON.parse 后的对象，比较时统一归一化。 */
function norm(v) {
  return typeof v === 'string' ? v : JSON.stringify(v);
}

// ============================================================================
console.log('\nC1 · 前导单行垃圾（WARNING 行出现在帧前）');
// ============================================================================
{
  const d = decoder();
  const payload = '{"jsonrpc":"2.0","id":1,"result":{"ok":true,"code":"OK"}}';
  d.push(Buffer.from('WARNING: some library printed this\n', 'utf8'));
  d.push(encodeFrame(payload));

  const msg = d.next();
  check(
    '单行垃圾被忽略，帧正常解析',
    msg !== undefined && JSON.stringify(msg) === JSON.stringify(JSON.parse(payload)),
    msg === undefined ? '未解出帧' : JSON.stringify(msg),
  );
  check('后续无多余帧', d.next() === undefined);
  check('解析完成后缓冲区已清空', d.bufferedBytes === 0, `剩 ${d.bufferedBytes} 字节`);
}

// ============================================================================
console.log('\nC2 · 多行垃圾（traceback，自带空行 \\r\\n\\r\\n）');
// ============================================================================
{
  const d = decoder();
  // 关键：垃圾内部自带空行，会让"第一个 headerEnd"落在垃圾里。
  const trash =
    'Traceback (most recent call last):\r\n' +
    '  File "salary.py", line 42, in load\r\n' +
    'RuntimeError: 薪资列缺失\r\n' +
    '\r\n' +
    'Some trailing garbage line\r\n';
  const payload = '{"jsonrpc":"2.0","id":2,"result":{"ok":true}}';

  d.push(Buffer.from(trash, 'utf8'));
  check('纯垃圾段不产出帧', d.next() === undefined);
  check(
    '垃圾被丢弃时留下诊断',
    d.diagnostics.some((x) => x.includes('discard-header-segment')),
    `诊断：${JSON.stringify(d.diagnostics)}`,
  );

  d.push(encodeFrame(payload));
  const msg = d.next();
  check(
    '丢弃垃圾后，下一轮命中真帧',
    msg !== undefined && JSON.stringify(msg) === JSON.stringify(JSON.parse(payload)),
    msg === undefined ? '未解出帧' : JSON.stringify(msg),
  );
}

// ============================================================================
console.log('\nC3 · 声明长度超限（payload-too-large，丢弃且不缓冲）');
// ============================================================================
{
  const diagnostics = [];
  const dec = new FrameDecoder({ maxPayloadBytes: 100 });
  const collect = (chunk) => {
    const r = dec.feed(chunk);
    diagnostics.push(...r.diagnostics);
    return r;
  };

  // 声明一个远超上限的长度，并真的把 payload 推进来 —— 验证"不缓冲"。
  const oversizeHeader = Buffer.from('Content-Length: 999999999\r\n\r\n', 'ascii');
  const oversizeBody = Buffer.alloc(4096, 0x78); // 4096 个 'x'，无换行
  const r1 = collect(Buffer.concat([oversizeHeader, oversizeBody]));

  check('超长帧不被解出', (r1.messages ?? []).length === 0);
  check(
    '记录 payload-too-large 诊断',
    diagnostics.some((x) => x.includes('payload-too-large')),
    `诊断：${JSON.stringify(diagnostics)}`,
  );

  // 超限帧的 payload 不应被长期保留在缓冲里（丢弃段之后仍可恢复，但不堆积）。
  const payload = '{"jsonrpc":"2.0","id":3}';
  const r2 = collect(encodeFrame(payload));
  check(
    '超限丢弃后，后续真帧仍可恢复（自愈）',
    (r2.messages ?? []).length === 1 &&
      JSON.stringify(r2.messages[0]) === JSON.stringify(JSON.parse(payload)),
    `得到 ${JSON.stringify(r2.messages ?? [])}`,
  );

  // 顺带确认默认上限确实生效（不传参时 32MB 也会拒）。
  const dDefault = decoder();
  dDefault.push(Buffer.from('Content-Length: 999999999999\r\n\r\n', 'ascii'));
  check('默认 32MB 上限同样拒帧', dDefault.next() === undefined);
  check(
    '默认上限也记诊断',
    dDefault.diagnostics.some((x) => x.includes('payload-too-large')),
    `诊断：${JSON.stringify(dDefault.diagnostics)}`,
  );
}

// ============================================================================
console.log('\nC4 · 半包与粘包');
// ============================================================================
{
  // --- C4a：一次 feed 含 1.5 帧 -------------------------------------------
  const d = decoder();
  const f1 = '{"id":1,"method":"ping"}';
  const f2 = '{"id":2,"method":"ping","params":{"note":"中文测试"}}';
  const bytes1 = encodeFrame(f1);
  const bytes2 = encodeFrame(f2);
  const cut = bytes2.byteLength - 10; // 第二帧只给到倒数第 10 字节

  d.push(Buffer.concat([bytes1, bytes2.subarray(0, cut)]));
  const got1 = d.next();
  check(
    '1.5 帧：第一帧完整解出',
    got1 !== undefined && JSON.stringify(got1) === JSON.stringify(JSON.parse(f1)),
    got1 === undefined ? '未解出' : JSON.stringify(got1),
  );
  check('1.5 帧：半包不产生伪帧', d.next() === undefined);
  check(
    '1.5 帧：半包字节仍留在缓冲里',
    d.bufferedBytes === cut,
    `缓冲 ${d.bufferedBytes}，期望 ${cut}`,
  );

  d.push(bytes2.subarray(cut)); // 补齐剩下的 10 字节
  const got2 = d.next();
  check(
    '1.5 帧：补齐后第二帧完整解出',
    got2 !== undefined && JSON.stringify(got2) === JSON.stringify(JSON.parse(f2)),
    got2 === undefined ? '未解出' : JSON.stringify(got2),
  );

  // --- C4b：一次 feed 含 3 帧（粘包） --------------------------------------
  const d3 = decoder();
  const p1 = '{"id":10}';
  const p2 = '{"id":11,"msg":"第二帧"}';
  const p3 = '{"id":12,"msg":"第三帧"}';
  d3.push(Buffer.concat([encodeFrame(p1), encodeFrame(p2), encodeFrame(p3)]));
  const out = d3.take().map((m) => JSON.stringify(m));
  check(
    '粘包 3 帧：全部按序解出',
    JSON.stringify(out) ===
      JSON.stringify([p1, p2, p3].map((s) => JSON.stringify(JSON.parse(s)))),
    `得到 ${JSON.stringify(out)}`,
  );
  check('粘包 3 帧：不产生第 4 帧', d3.next() === undefined);
  check('粘包 3 帧：缓冲区清空', d3.bufferedBytes === 0, `剩 ${d3.bufferedBytes}`);

  // --- C4c：逐字节喂（最严苛的半包） ----------------------------------------
  const dDrip = decoder();
  const drip1 = '{"id":20}';
  const drip2 = '{"id":21,"msg":"逐字节"}';
  const stream = Buffer.concat([encodeFrame(drip1), encodeFrame(drip2)]);
  const seen = [];
  for (let i = 0; i < stream.byteLength; i += 1) {
    dDrip.push(stream.subarray(i, i + 1));
    for (;;) {
      const f = dDrip.next();
      if (f === undefined) break;
      seen.push(JSON.stringify(f));
    }
  }
  check(
    '逐字节喂入：两帧都完整解出',
    JSON.stringify(seen) ===
      JSON.stringify([drip1, drip2].map((s) => JSON.stringify(JSON.parse(s)))),
    `得到 ${JSON.stringify(seen)}`,
  );
}

// ============================================================================
console.log('\nP1 · Content-Length 是 UTF-8 字节数，不是字符数');
// ============================================================================
{
  const chinese = '{"msg":"红圈绿圈渗透率带宽重叠度中位值级差"}';
  check(
    '测试数据确实让字符数 ≠ 字节数（否则这条纪律没被测到）',
    chinese.length !== Buffer.byteLength(chinese, 'utf8'),
    `字符数 ${chinese.length} == 字节数 ${Buffer.byteLength(chinese, 'utf8')}`,
  );

  const d = decoder();
  d.push(encodeFrame(chinese));
  const msg = d.next();
  check(
    '中文字节数帧正确解出（中文未乱码）',
    msg !== undefined && msg['msg'] === JSON.parse(chinese)['msg'],
    msg === undefined ? '未解出' : JSON.stringify(msg),
  );

  // 反例：若用字符数声明长度（即违反 P1），解码器必然读少 → JSON 残缺。
  const body = Buffer.from(chinese, 'utf8');
  const wrongHeader = Buffer.from(`Content-Length: ${chinese.length}\r\n\r\n`, 'ascii');
  const bad = decoder();
  bad.push(Buffer.concat([wrongHeader, body]));
  const badMsg = bad.next();
  check(
    '反例：用字符数声明长度会读少（证明 P1 不可违反）',
    badMsg === undefined || badMsg['msg'] !== JSON.parse(chinese)['msg'],
    '用字符数竟然也能读对 —— 测试数据需要更强的中文',
  );

  // 编码/解码往返：FrameDecoder.encode 自己也要用字节数。
  const roundTrip = decoder();
  const obj = { msg: '薪酬带宽重叠度', n: 3 };
  roundTrip.push(FrameDecoder.encode(obj));
  check(
    'encode/feed 往返一致（encode 侧同样用字节数）',
    JSON.stringify(roundTrip.next()) === JSON.stringify(obj),
  );
}

// ============================================================================
console.log('\nP4 · 无分隔符的超长垃圾会被丢弃（防缓冲无限增长 / 防假死）');
// ============================================================================
{
  const d = decoder({ maxHeaderScanBytes: 64 });
  d.push(Buffer.alloc(200, 0x41)); // 200 个 'A'，一个换行都没有
  check('无分隔符的大段垃圾不产出帧', d.next() === undefined);
  check(
    '触发 discard-no-separator 诊断',
    d.diagnostics.some((x) => x.includes('discard-no-separator')),
    `诊断：${JSON.stringify(d.diagnostics)}`,
  );
  check(
    '垃圾被丢弃而非无限堆积',
    d.bufferedBytes === 0,
    `剩 ${d.bufferedBytes} 字节`,
  );

  // 丢弃之后必须还能重获同步，否则等于把桥打死了。
  const payload = '{"id":99,"msg":"重获同步"}';
  d.push(encodeFrame(payload));
  const msg = d.next();
  check(
    '丢弃垃圾后能重获同步（不会把桥打死）',
    msg !== undefined && JSON.stringify(msg) === JSON.stringify(JSON.parse(payload)),
    msg === undefined ? '未解出' : JSON.stringify(msg),
  );
}

// ============================================================================
console.log(`\n${'='.repeat(60)}`);
console.log(`FrameDecoder 单测：${passed} 通过 / ${failed} 失败`);
console.log('='.repeat(60));
if (failed > 0) process.exitCode = 1;
