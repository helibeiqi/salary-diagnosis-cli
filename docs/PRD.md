# PRD — 薪酬诊断 Agent 插件（comp-agent-harness）

| 项 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 撰写角色 | 产品经理（团队代号 `pm` / 人设 许清楚） |
| 日期 | 2026-08-30 |
| 状态 | 已发布给工程/QA；标注「⚠️待架构确认」的 4 处口径待 `software-architect-2` 回签后定稿 |
| 上游依据 | `BRIEFING.md`（需求全文 + dsh 契约实测）、`src/tools/schemas.py`（字段与参数**单一真理源**）、`mock_data.py`（造数逻辑） |
| 适用读者 | 工程师 A/B/C、QA、架构师 |

> **本文档的所有数值均来自对 `data/sample_salary.csv`（150 条，seed=20260830）的实跑测算，不是拍脑袋给的。**
> 测算过程见 `docs/PRD.md` 附录 C。QA 可逐条复算对账。

---

## 0. 文档说明与阅读顺序

| 章节 | 作用 | 谁必读 |
|---|---|---|
| §1 问题定义与目标用户 | 回答"为什么做、给谁用" | 全员 |
| §2 术语与口径词典 | **所有字段/公式的唯一口径**，与 `schemas.py` 严格一致 | 工程师 A/B、QA |
| §3 功能清单 FR-01 ~ FR-16 | 做什么 | 工程师、QA |
| §4 验收标准 AC-01 ~ AC-41 | **可机检**的完成定义 | QA（逐条跑）、工程师（自测） |
| §5 非功能需求 NFR-01 ~ NFR-10 | 安全/健壮/可讲解/可复现 | 全员 |
| §6 模块依赖顺序 | 谁先做、谁能并行 | 工程师、team-lead |
| §7 金标准数值样例（6 组） | **手算可校验**的对账基准 | QA |
| §8 Out of Scope | 明确不做，防范围蔓延 | 全员 |

---

## 1. 问题定义与目标用户

### 1.1 目标用户画像

| 维度 | 描述 |
|---|---|
| 主用户 | 在职 HR（薪酬绩效方向），正在做 **AI+HR 求职作品集**，需要向面试官展示可讲解、可复现的专业产出 |
| 次用户 | 中小企业 HR 负责人 / HRBP，公司无薪酬带宽体系、无专业薪酬系统，靠 Excel 管薪 |
| 使用场景 | 年度调薪季（11 月-次年 2 月）、薪酬体系从 0 到 1 搭建、并购后薪酬套改、核心人才保留专项 |
| 现有工具 | Excel（透视表 + VLOOKUP）、外部咨询公司的 PDF 报告（贵且无数据沉淀）、付费薪酬调研数据库（只有分位值，没有本司套改方案） |
| 技术水平 | 会用 Excel 公式，不会写 Python；能接受"装一次环境、之后对话式使用" |

### 1.2 五类高频痛点（每一条都对应下文的具体 FR）

| # | 痛点（HR 的原话） | 现状代价 | 对应 FR |
|---|---|---|---|
| P1 | **"每次看薪酬分布都要手搓透视表，换个维度就重做一遍"** | 150 人的表做一次分布分析 20 分钟；部门/职级/绩效三个维度排列组合，半天没了 | FR-03 |
| P2 | **"带宽上下限都是拍脑袋定的，业务一问'凭什么这个职级上限是 2 万'我就答不上来"** | 缺少方法论锚点，带宽靠经验值，谈判中被动 | FR-04 |
| P3 | **"谁红圈谁绿圈全靠肉眼扫，扫完还得手动标色"** | 150 人尚可，1000 人就不可行；且容易漏掉"薪资在上限之上但看着不算高"的人 | FR-03 |
| P4 | **"调薪方案改一版就要重算一小时，老板问'如果预算从 5% 降到 3% 会怎样'我当场算不出来"** | 方案迭代成本极高，只能给一版方案，无法做多策略对比 | FR-06 |
| P5 | **"固浮比拍不准，销售说 40:60 太低、研发说 30% 浮动太高，我被两边怼"** | 缺少"浮动比例的适用前提"这一方法论武器，争论停留在情绪层面 | FR-07 |

### 1.3 产品定位与成功标准

**一句话定位**：一个**本地可跑、方法论可查、计算可信**的薪酬诊断 Agent——HR 上传脱敏工资表，用中文对话完成「读表 → 识字段 → 诊断 → 带宽 → 对标 → 调薪 → 固浮比 → 报告」全链路。

因为是**面试作品集**，除了功能可用，还必须同时满足三件事（这三条贯穿全部 NFR）：

| 目标 | 含义 | 落地要求 |
|---|---|---|
| **可演示性** | 断网也能完整跑通全流程，图表能拿出来给面试官看 | NFR-06 离线可跑、NFR-07 图表产出、FR-15 本地入口 |
| **可讲解性** | 每个方法论都有据可查，面试追问答得上来 | NFR-04 中文注释覆盖、§7 金标准样例、README 方法论章节（面试讲解指南为本地私有备战笔记，见 data/private/interview-prep.md，不进仓库） |
| **数据安全** | 薪资数据极度敏感，默认本地部署 + 脱敏 | NFR-01/NFR-02、FR-10/FR-11/FR-12 |

---

## 2. 术语与口径词典（与 `schemas.py` 严格一致）

> **工程纪律**：本表是唯一口径。任何工具实现、任何报告文案、任何 QA 对账，都必须按本表。
> 字段名一律用 `schemas.py` 的**内部名**（如 `monthly_salary`），中文名只用于展示层。

### 2.1 字段口径

| 内部名 | 中文名 | 必填 | 类型 | PRD 补充口径 |
|---|---|---|---|---|
| `emp_id` | 员工ID | ✅ | str | 去重主键；`drop_duplicates(subset=["emp_id"], keep="first")` |
| `name` | 姓名（脱敏） | ❌ | str | 脱敏后格式 `姓**`（正则 `^[\u4e00-\u9fa5]\*\*$`） |
| `dept` | 部门 | ❌ | str | 分组维度之一 |
| `level` | 职级 | ✅ | str | **分组主键**；排序用 `level_sort_key()`，顺序恒为 `P1<P2<P3<P4<P5<P6<M1<M2<M3` |
| `job_title` | 岗位名称 | ❌ | str | 仅展示 |
| `job_family` | 岗位序列 | ❌ | str | 固浮比/分位值策略主键；缺失时用 `infer_job_family(dept, level)` 推断 |
| `job_score` | 岗位价值评估得分 | ❌ | number | 量纲 100~1600；缺失率约 39% |
| `monthly_salary` | 当前月薪 | ✅ | number | **CR 的分子**；单位：元/月 |
| `annual_total_cash` | 年度总现金 | ❌ | number | 缺失率约 19%；**不得用作调薪基数**（见 §2.3） |
| `tenure_years` | 司龄 | ❌ | number | 用于解释红圈成因（年资溢价） |
| `perf_grade` | 绩效等级 | ❌ | str | 归一化后取 `A/B/C/D`（大写、去空格、去 `+` 后缀） |
| `pay_mix` | 固浮比 | ❌ | str | 格式 `70:30` 或 `0.7`；缺失率约 32% |
| `band_min` / `band_mid` / `band_max` | 带宽下限/中位值/上限 | ❌ | number | 样本中 100% 缺失 → 走「工具建议带宽」路径 |
| `mkt_p25` / `mkt_p50` / `mkt_p75` | 市场 P25/P50/P75 | ❌ | number | `mkt_p25` 缺失率约 6.67%，对标模块必须容错 |

### 2.2 核心指标公式

| 指标 | 公式 | 业务含义 | 阈值 |
|---|---|---|---|
| **CR（Compa-Ratio）** | `CR = monthly_salary / band_mid` | 个人薪资在带宽中的相对位置；1.0 = 正好在中位值 | 见下 |
| **红圈 Red Circle** | `CR > 1.20` **或** `monthly_salary > band_max` | 薪酬高于带宽 → 成本溢出、内部公平性风险、再涨空间受限 | `RED_CIRCLE_CR = 1.20` |
| **绿圈 Green Circle** | `CR < 0.80` **或** `monthly_salary < band_min` | 薪酬低于带宽 → 外部竞争力不足、流失风险 | `GREEN_CIRCLE_CR = 0.80` |
| **合理** | 非红非绿 | — | `0.80 ≤ CR ≤ 1.20` 且薪资在 `[band_min, band_max]` 内 |
| **渗透率 Range Penetration** | `(monthly_salary - band_min) / (band_max - band_min)` | 薪资在带宽中的**绝对位置**；0% = 下限，100% = 上限 | 可 <0%（低于下限）或 >100%（高于上限） |
| **带宽幅度 Range Spread** | `(band_max - band_min) / band_min` | 带宽的宽窄 | 见 `LEVEL_TIER_RULES`（0.25~0.60） |
| **中位值级差 Midpoint Differential** | `(mid_{n+1} - mid_n) / mid_n` | 相邻职级中位值的增幅 | 默认 15% |
| **相邻带宽重叠度 Range Overlap** | `(band_max_n - band_min_{n+1}) / (band_max_n - band_min_n)` | 相邻职级带宽的重叠比例；**负数=薪酬断层（Gap）** | 经验值 20%~50% |
| **市场对标差距** | `(monthly_salary - mkt_p50) / mkt_p50` | 相对市场中位值的偏离；负数=低于市场 | — |

**带宽生成公式（BRIEFING 指定，工程必须严格实现）**：

```
band_min = band_mid / (1 + spread / 2)
band_max = band_min × (1 + spread)
```

由此可推出三条**恒等式**（QA 用这三条做数值对账，见 AC-11）：

1. `(band_min + band_max) / 2 == band_mid`（中位值恰为算术中点）
2. `(band_max - band_min) / band_min == spread`（幅度口径自洽）
3. `CR = 1.0` 时渗透率恒为 `0.500000`

**CR 与渗透率的数学关系**（QA 对账用，见 AC-13）：

```
penetration = (CR × (1 + spread/2) - 1) / spread
```

推论（面试可讲的点）：**带宽幅度越小，"CR 阈值"与"带宽上下限"两个判据越不一致**。
触顶 CR = `(1+spread)/(1+spread/2)`，触底 CR = `1/(1+spread/2)`：

| spread | 触顶 CR | 触底 CR | 说明 |
|---|---|---|---|
| 0.25 | 1.1111 | 0.8889 | 上下限判据先于 CR 阈值触发 |
| 0.35 | 1.1489 | 0.8511 | 上下限判据先触发 |
| 0.50 | 1.2000 | 0.8000 | **两套判据恰好重合** |
| 0.60 | 1.2308 | 0.7692 | CR 阈值先触发 |

### 2.3 全局计算口径（PRD 锁定，工程不得自行变更）

| # | 口径 | 决定 | 理由 |
|---|---|---|---|
| K1 | **分位值算法** | 统一用**线性插值**（pandas/numpy 默认 `linear`） | 不同插值法会让 P25/P75 对不上账 |
| K2 | **调薪基数** | `月薪 × 12`，**不用** `annual_total_cash` | 后者缺失 18.67% |
| K3 | **带宽取整** | **判定前一律不取整**；取整只发生在展示层 | 取整到 100 元会让红圈从 31 人变成 26 人（实测） |
| K4 | **职级排序** | `level_sort_key()` → `P1…P6, M1, M2, M3` | 字母序会把 M1 排在 P1 前，重叠度全错 |
| K5 | **绩效归一化** | `.strip().upper()` 并去掉 `+` 后缀（`a+`→`A`，`B `→`B`） | 真实表大小写/写法不统一 |
| K6 | **红绿圈判定的互斥性** | 先判红、后判绿；两者在数值上互斥 | 避免覆盖顺序导致结果漂移 |
| K7 | **调薪后再判定的带宽基准** | 默认用**调薪前的带宽**（`rebase_band=False`）；可选 `rebase_band=True` 让带宽同步上移；**报告必须并列展示两种情景**。✅ team-lead 2026-08-30 裁定 | 见 §3.6 |
| K8 | **岗位评估总分换算** | `总分 = weighted_to_total(W) = W × JOB_SCORE_SCALE`，`JOB_SCORE_SCALE = 160.0`；`W = Σ(因子得分 × 权重)`，`因子得分 = 子维度算术平均`（1~10）⇒ 总分 ∈ [160, 1600] | ✅ 已修复入 `schemas.py`（team-lead 2026-08-30）；不改则模块 6 恒输出 P1。见 §3.8 / AC-29 |
| K9 | **带宽取整的分工** | **判定与 CR 计算一律用未取整值**；`round_to` 取整只作用于**对外发布的带宽表**，且输出必须显式标注"本表为展示取整值，判定依据为未取整值" | ✅ team-lead 2026-08-30 裁定 |
| K10 | **预算分母** | 统一用 `Σ(月薪 × 12)`（覆盖率 100%）；`annual_total_cash` 缺 18.67%，**只作个人层面参考展示，不进任何预算/成本计算** | ✅ team-lead 2026-08-30 裁定 |

---

## 3. 功能清单（FR）

### 3.0 模块总览

| FR | 模块 | 对应工具 | 依赖 | 优先级 |
|---|---|---|---|---|
| FR-01 | 数据加载与字段识别 | `load_salary_data` | — | P0 |
| FR-02 | 字段映射确认 | `confirm_mapping` | FR-01 | P0 |
| FR-03 | 薪酬现状诊断 | `analyze_current_state` | FR-02、FR-04(建议带宽) | P0 |
| FR-04 | 薪酬带宽设计 | `generate_band` | FR-02 | P0 |
| FR-05 | 市场对标 | `market_benchmark` | FR-02、FR-04 | P0 |
| FR-06 | 调薪模拟 | `simulate_increase` | FR-03、FR-04 | P0 |
| FR-07 | 固浮比模拟 | `simulate_pay_mix` | FR-02 | P0 |
| FR-08 | 岗位价值评估 | `calc_job_score` | — | P1 |
| FR-09 | 报告生成 | `generate_report` | FR-03~FR-08 | P0 |
| FR-10 | 模拟数据生成 | CLI `mock_data.py` | — | P0 |
| FR-11 | 数据脱敏 | `desensitize_data` + CLI | — | P1 |
| FR-12 | 模型配置化切换 | `config.yaml` | — | P0 |
| FR-13 | PTC 代码执行 | `run_comp_code` | FR-01~FR-11 | P1 |
| FR-14 | 图表与导出基建 | （内部库） | plotly | P0 |
| FR-15 | 本地端到端入口 | `main.py` | 全部 | P0 |
| FR-16 | SKILL.md 对话行为 | `src/skills/comp-analyst/SKILL.md` | FR-01~FR-09 | P0 |

---

### FR-01 `load_salary_data` — 数据加载与字段识别

| 项 | 内容 |
|---|---|
| **输入** | `file_path: str`（.csv/.xlsx）；可选 `sheet_name`、`preview_rows: int = 5` |
| **处理** | ① 读文件（CSV 用 `utf-8-sig`，失败回退 `gbk`）；② `auto_suggest_mapping(columns)` 生成映射建议；③ 脏数据预检（重复 `emp_id`、非数值、必填缺失）；④ 数值字段 `pd.to_numeric(errors="coerce")` |
| **输出** | `{ok, data:{columns, preview(前5行), suggested_mapping, dirty_report:{dup_ids, non_numeric, missing_required}, row_count}, warnings, meta}` |
| **业务含义** | 真实企业的表头千奇百怪（"基本工资(元/月)"、"Base Pay"），这一步把"认表"这件事从人工变成机器给候选、模型拍板 |
| **异常处理** | 文件不存在→`FILE_NOT_FOUND`；格式不支持→`UNSUPPORTED_FORMAT`；空文件→`EMPTY_DATASET`；编码全失败→`UNSUPPORTED_FORMAT` |
| **⚠️安全** | `preview` 最多 5 行；`warnings` 只报条数与列名，**不打印完整数据表** |

---

### FR-02 `confirm_mapping` — 字段映射确认

| 项 | 内容 |
|---|---|
| **输入** | `mapping: dict`（原始列名 → 标准字段名）；可选 `strict: bool = True` |
| **处理** | ① 校验 mapping 的 value 都在 `CANONICAL_FIELDS` 内；② 检查三个必填字段（`emp_id`/`level`/`monthly_salary`）是否都已映射；③ 重命名列 → 标准字段；④ 数值字段强制转 float（非数值→NaN 并记 warning）；⑤ `drop_duplicates(subset=["emp_id"])`；⑥ 丢弃 `monthly_salary` 为 NaN 的行（无法诊断）；⑦ 绩效归一化（K5） |
| **输出** | `{ok, data:{session_id, mapped_fields, unmapped_columns, clean_stats:{raw_rows, cleaned_rows, dropped_dup, dropped_non_numeric, dropped_missing}, canonical_preview}, warnings, meta}` |
| **业务含义** | 把"任意不规范表头"一次性固化成标准面板，后续 7 个模块全部只读标准字段 |
| **异常处理** | 缺必填映射→`MISSING_REQUIRED_FIELD`；映射值非法→`INVALID_PARAM`；清洗后 0 行→`ALL_ROWS_DROPPED` |

---

### FR-03 `analyze_current_state` — 薪酬现状诊断

| 项 | 内容 |
|---|---|
| **输入** | `session_id`；可选 `group_by: list = ["level"]`、`band_source: "auto"｜"existing"｜"generated" = "auto"` |
| **处理** | ① **带宽准备**：若 `band_mid` 全缺 → 用各职级**现状中位数**作临时中位值（K3 不取整），套 `get_level_tier()` 的默认幅度生成建议带宽；否则用表内带宽；② 逐人算 `CR` 与 `penetration`；③ 红绿圈判定（K6）；④ 按 `group_by` 聚合 `count/min/max/median/P25/P75/mean`；⑤ 风险测算：红圈成本溢出、绿圈补足成本 |
| **输出** | `{ok, data:{band_used:{level:{band_min,band_mid,band_max,spread,tier}}, level_stats:[...], cr_summary:{mean,std,min,p25,median,p75,max}, flag_summary:{红圈:{count,pct,monthly_cost},绿圈:{...},合理:{...}}, risk:{red_overflow_monthly, green_fill_monthly, red_by_level:[...]}, charts:[...]}, warnings, meta}` |
| **业务含义** | 回答三个问题：公司在每个职级付多少钱？谁付多了（红圈，成本 + 内部公平风险）？谁付少了（绿圈，流失风险）？ |
| **图表** | ① 各职级薪酬分布（箱线/柱状）② CR 分布直方图 ③ 红绿圈人数占比（堆叠柱） |
| **异常处理** | 无 session→`INVALID_PARAM`；分组字段不存在→`INVALID_PARAM`；分组后空→`EMPTY_DATASET` |

**「成本溢出 / 补足成本」定义（QA 对账口径）**：

```
红圈成本溢出（月） = Σ max(0, monthly_salary - band_max)     # 只算超出上限的部分
绿圈补足成本（月） = Σ max(0, band_min - monthly_salary)     # 补到下限所需
```

---

### FR-04 `generate_band` — 薪酬带宽设计

| 项 | 内容 |
|---|---|
| **输入** | `levels: list`（默认取全部职级）、`base: "current_median"｜"market"｜"custom" = "current_median"`、`anchor_level`、`anchor_mid`（custom 时必填）、`midpoint_diff: float = 0.15`、`spread: "default"｜float｜dict`、`use_market: bool = False`、`mode: "new"｜"optimize" = "new"` |
| **处理** | ① 定中位值序列：`current_median` 用各职级现状中位数；`market` 用 `mkt_p50` 中位数；`custom` 以 `anchor_level/anchor_mid` 为锚、按 `midpoint_diff` 递推；② 套幅度：`spread="default"` 时用 `get_level_tier(level)[1]`；③ 算带宽三列（§2.2 公式）；④ 算相邻职级 `midpoint_diff` 与 `overlap`；⑤ `mode="optimize"` 时在控制总成本不增加的约束下平滑中位值序列 |
| **输出** | `{ok, data:{band_table:[{level,tier,spread,band_min,band_mid,band_max,n,current_median,current_cr_median}], adjacency:[{pair,mid_diff_pct,overlap_amt,overlap_pct}], summary:{spread_range, gap_pairs}}, csv_path, chart_path}` |
| **输出物** | `report/band_table_{timestamp}.csv`（`utf-8-sig`）+ Plotly 横向条形图（区间 + 重叠部分着色） |
| **业务含义** | 把"拍脑袋定上下限"变成"锚定中位值 + 分层幅度 + 可查的重叠度"，业务追问时能给出公式 |
| **异常处理** | `anchor_level` 不在数据里→`INVALID_PARAM`；`spread ≤ 0`→`INVALID_PARAM`；`midpoint_diff ≤ 0`→`INVALID_PARAM` |

---

### FR-05 `market_benchmark` — 市场对标

| 项 | 内容 |
|---|---|
| **输入** | `session_id`；`strategy: "P25"｜"P50"｜"P75"｜"by_family"＝"P50"`、`group_by: ["level"]｜["level","job_family"]`、`only_below: bool = True` |
| **处理** | ① 取市场字段（`by_family` 时按 `DEFAULT_MARKET_STRATEGY` 选 `mkt_p75/p50/p25`）；② 算个人差距 `(salary - mkt)/mkt`；③ 按组聚合（中位数口径 + 个人均值口径都要给）；④ 算达标所需调薪：`Σ max(0, mkt_target - salary)` 及占比、涉及人数 |
| **输出** | `{ok, data:{gap_table:[...], strategy_used, cost:{monthly, annual, pct_of_payroll, headcount}, by_family:[...]}, warnings, meta}` |
| **业务含义** | 回答"我们比市场低多少、补齐要花多少钱、按什么分位值花钱最划算" |
| **异常处理** | 无任何市场列→返回 `ok=false` + `error.code = MISSING_REQUIRED_FIELD`（提示"请先补充市场数据列"）；部分缺失→`warnings` 提示并按可用行计算 |

---

### FR-06 `simulate_increase` — 调薪模拟

| 项 | 内容 |
|---|---|
| **输入** | `session_id`；`budget_rate: float`（占薪资基数总额的比例，如 `0.05`）、`strategies: list = ["A","B","C","D"]`、`perf_weights: dict = PERF_WEIGHTS`、`custom_weights: dict = None`、`cap_at_max: bool = False`、`rebase_band: bool = False`、`freeze_red: bool`（策略 D 内部使用） |
| **处理（4 种策略，全部**预算守恒**）** | 见下表 |
| **输出** | `{ok, data:{budget:{rate, amount, base_total}, plans:{<策略>:{name, cost, usage_rate, per_perf_rate, after:{cr_mean, cr_median, red, green, normal, over_max}, detail_csv}}, recommendation:{strategy, reason}, charts:[...]}, meta}` |
| **输出物** | 每策略一份个人调薪明细 CSV（`report/increase_detail_{策略}_{timestamp}.csv`）+ 调薪前后 CR 分布对比图 + 各策略成本柱状图 |
| **业务含义** | 把"改一版重算一小时"变成"输入预算率，4 种策略一次性出齐"，并给出推荐与理由 |
| **异常处理** | `budget_rate ≤ 0 或 > 0.5`→`INVALID_PARAM`；绩效权重全 0→`INVALID_PARAM`；无绩效列而选策略 C/D→`warnings` + 回退到策略 A |

**四种分配策略（口径锁定）**：

| 策略 | 名称 | 算法 | 预算守恒机制 |
|---|---|---|---|
| **A** | 平均分配 | 每人调薪率 = `budget_rate` | 天然守恒 |
| **B** | 优先补绿圈 | ① 绿圈员工补到 `max(band_min, 0.8 × band_mid) × 1.001`；② **剩余预算**按绩效权重在全员中二次分配 | 两步合计 = 预算；若第①步已超预算，则第①步按比例缩放到预算 |
| **C** | 按绩效加权 | `调薪率_i = budget × w_i / Σ(w_j × base_j)`（同绩效等级的人调薪率相同，与个体薪资无关） | 天然守恒 |
| **D** | 优先保留红圈 | 红圈员工**冻结**（调薪额 = 0，报告中建议改为一次性补贴/晋升套改）；预算在**非红圈**员工中按绩效权重分配 | 天然守恒 |

**自定义权重**：`custom_weights` 覆盖 `PERF_WEIGHTS`，校验 `len == 4` 且 `≥ 0`。

**`cap_at_max`**：`True` 时单人调薪后不超过 `band_max`，此时**守恒改为 ≤ 预算**，并在返回里给出 `unused_budget`。默认 `False`（真实调薪极少硬性封顶，且封顶会破坏守恒可解释性）。

**`rebase_band`**（✅ team-lead 2026-08-30 裁定保留，**默认 `False`**）：`True` 时带宽三列同步按全员平均调薪率上移后再算 CR，用于回答"如果带宽跟着市场一起走会怎样"。

**双情景并列（强制）**：`simulate_increase` 的每个策略输出都必须含 `scenarios` 数组，长度为 2：

| 情景 | `rebase_band` | 语义 | 报告中的定位 |
|---|---|---|---|
| 主口径 | `False`（默认） | 带宽冻结在调薪前水平 —— 保守口径，暴露真实成本 | 主表格、推荐结论的依据 |
| 对照口径 | `True` | 带宽随全员平均调薪率同步上移 —— 反映"带宽年度重定级"后的稳态 | 副栏 / 对照说明 |

两者 `total_cost` 必须完全相同（rebase 只改变判定基准，不改变调薪额）。

**报告必含的解释文案**（FR-09 强制，QA 按 AC-33 机检）：当 `rebase_band=False` 且调薪后红圈人数上升时，必须输出：
> "调薪后红圈人数由 X 人增至 Y 人，是因为带宽锚定于**调薪前的现状中位数**、调薪期间保持不变；若带宽同步上移（rebase_band=True），红绿圈结构将保持不变。这提示：**普调会系统性抬高红圈比例，调薪预算应优先用于结构性补差而非全员普调。**"

---

### FR-07 `simulate_pay_mix` — 固浮比模拟

| 项 | 内容 |
|---|---|
| **输入** | `session_id`（或 `target_total_cash: float` 做单体测算）；`mix_by_family: dict = JOB_FAMILY_PAY_MIX`、`achievement_range: [0.0, 1.5]`、`step: 0.05` |
| **处理** | ① 目标固浮比：优先用表内 `pay_mix`，缺失时按 `infer_job_family()` 取 `JOB_FAMILY_PAY_MIX`；② 目标年总现金 `TC = monthly_salary × 12 + 奖金`；③ `固定 = TC × 固定%`，`浮动 = TC × 浮动%`；④ `实际收入(a) = 固定 + 浮动 × a`，`a ∈ [0, 1.5]`；⑤ 输出曲线数据 |
| **输出** | `{ok, data:{mix_table:[{job_family, fixed_pct, var_pct, headcount, target_tc}], curve:[{achievement, income_by_family:{...}}], break_even_achievement}, chart_path, warnings}` |
| **业务含义** | 回答"这个浮动比例下，业绩打几折员工收入才跌破市场水平"，把固浮比争论从情绪拉回数字 |
| **⚠️强制文案** | 输出与报告**必须**包含以下提示（QA 用正则机检）：<br>`高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；长周期协作型业务强行高浮动会破坏协作。` |
| **异常处理** | 固浮比解析失败（非 `70:30`/`0.7` 格式）→`warnings` + 回退到序列基准值；序列不在基准表→回退 `DEFAULT_PAY_MIX` |

---

### FR-08 `calc_job_score` — 岗位价值评估

| 项 | 内容 |
|---|---|
| **输入** | `model: "hay"｜"mercer" = "hay"`、`scores: dict`（子维度名 → 1~10 分）、`job_title`、`export_template: bool = False` |
| **处理** | ① 校验子维度名属于 `JOB_EVAL_MODELS[model]["subfactors"]`；② 校验分值在 `scale=(1,10)` 内（缺失维度按同因子已填均值兜底、全缺按 5 分兜底，并记 `warnings`）；③ `因子得分 = 该因子下子维度算术平均`；④ `W = Σ(因子得分 × 权重)`，W ∈ [1, 10]；⑤ **总分 = `weighted_to_total(W)` = `W × JOB_SCORE_SCALE`，`JOB_SCORE_SCALE = 160.0`**，总分 ∈ [160, 1600]；⑥ `score_to_level(总分)` 建议职级 |
| **输出** | `{ok, data:{model, model_label, position, factors:[{factor, weight, subfactor_mean, weighted}], W, total_score, suggested_level, level_band:{lo,hi,level}, scale_note, batch_result}, template_csv_path?}` |
| **输出物** | `template_out` 指定时导出空白打分表 CSV/Excel（含子维度、分值说明、示例行） |
| **业务含义** | 给"凭什么这个岗位是 P4"一个可复查的打分过程 |
| **异常处理** | `model` 非法→`INVALID_PARAM`；子维度名不匹配→`INVALID_PARAM`（附合法清单）；分值越界→`INVALID_PARAM` |

> **K8 换算口径（已修复入 `schemas.py`，2026-08-30）**
>
> **缺陷**：`JOB_EVAL_MODELS[*]["scale"] = (1,10)`，而 `JOB_SCORE_LEVEL_BANDS` 与 `mock_data` 的 `job_score` 量纲是 **100~1600**。若把 W（∈[1,10]）直接喂给 `score_to_level()`，W 恒落入 `(0, 300)` → **所有岗位一律判 P1**，模块 6 完全失效。这是必现 bug。
>
> **修复**：`schemas.py` 新增 `JOB_SCORE_SCALE: float = 160.0` 与 `weighted_to_total(w) -> w × 160`。系数 160 = 目标总分上限 1600 ÷ 子维度满分 10。
>
> **校验（实跑）**：① `mock_data` 的 `job_score` 实际范围 187~1414 ⊂ [160, 1600] ✓；② 9 个职级的 `job_score` 均值反查 `score_to_level()` 与原职级**一致率 9/9** ✓；③ 高管样例 W=8.875 → 总分 **1420** → **M3**（修复前恒判 P1）✓。
>
> **展示纪律**：`total_score` 展示时 `round()` 取整，**职级判定用未取整值**（与 K3/K9 一致，避免边界漂移）。
> **免责声明（必须写进输出 `scale_note` 与报告/README）**：本项目为**简易打分表**，采用统一 160 倍缩放使海氏与美世两种模型可比，**非正式认证评估结果**，仅用于内部校准演示与教学。

---

### FR-09 `generate_report` — 报告生成

| 项 | 内容 |
|---|---|
| **输入** | `session_id`；`sections: list = 全部 7 节`、`fmt: "markdown"｜"html" = "markdown"`、`include_charts: bool = True` |
| **输出** | `report/薪酬诊断报告_{timestamp}.md`（+ 可选同名 `.html`）；`assets/` 下 Plotly 图表（`.html` 内嵌；有 `kaleido` 时额外出 `.png`） |
| **7 节结构（标题字符串固定，QA 逐条 grep）** | 1 执行摘要<br>2 数据概览与字段映射说明<br>3 薪酬现状诊断<br>4 带宽设计与市场对标建议<br>5 调薪/套改方案对比与推荐<br>6 固浮比与激励建议<br>7 风险提示与实施路线图 |
| **必含图表（≥6 张）** | ① 各职级薪酬分布 ② CR 分布直方图 ③ 红绿圈占比 ④ 带宽区间横向条形图（重叠着色）⑤ 市场对标对比 ⑥ 调薪前后 CR 对比 ⑦ 各策略成本柱状图 ⑧ 固浮比收入曲线 |
| **⚠️安全** | 报告正文**只出现汇总统计**；个人明细一律走单独 CSV，且姓名列必须为脱敏值 |
| **异常处理** | 未跑过任何分析→`warnings` 提示并只生成已有章节；图表渲染失败→`warnings` + 降级为内嵌 HTML，**不中断报告生成** |

---

### FR-10 模拟数据生成（CLI）

| 项 | 内容 |
|---|---|
| **入口** | `python mock_data.py [-n 150] [--seed 20260830] [--with-band] [--no-market] [--no-excel] [-o data]` |
| **产出** | `data/sample_salary.csv`（标准表头）、`data/messy_salary.csv`（乱列名 + 4 类脏数据）、`data/sample_salary.xlsx`（含「字段说明」sheet） |
| **脏数据类型（4 类，QA 逐条复验）** | ① 重复员工记录（系统同步重复导入）② 薪资列混入非数值 `"待定"` / `"—"` ③ 必填字段缺失（新入职未定薪，NaN）④ 绩效等级大小写/写法不统一（`a+` / `B `） |
| **⚠️安全** | 控制台只打印统计口径，**不逐行打印数据**；文件头部注释声明"全部为随机合成数据" |
| **已知待修** | BRIEFING §3 记录：`mock_data.py` 注入脏数据时曾有 `FutureWarning`，已用 `.astype(object)` 修复但**未复跑验证** → 见 AC-38 |

---

### FR-11 `desensitize_data` — 数据脱敏

| 项 | 内容 |
|---|---|
| **入口** | ✅ **第 11 个注册工具** `desensitize_data`（team-lead 2026-08-30 裁定批准）+ CLI `python mock_data.py --desensitize <file>` |
| **输入** | `file_path`、`mode: "scale"｜"jitter" = "scale"`、`seed: int = 20260830`、`output_path` |
| **处理** | `scale`：全员月薪 × 同一随机系数 `k ∈ [0.8, 1.2]`（由 seed 决定）；`jitter`：每人独立系数 `∈ [0.85, 1.15]`。两种模式都做：姓名 → `姓**`（`re.sub(r'(.).*', r'\1**', name)`）、`emp_id` → 顺序重编码 `E0001...`、删除备注类自由文本列 |
| **输出** | `{ok, data:{output_path, mode, factor, rows, verify:{corr_with_original, median_ratio}}, warnings}` + 脱敏后 CSV |
| **业务含义** | 让 HR 敢拿真数据在本地跑；`scale` 模式保持内部相对关系（相关系数 = 1.0），适合做方案测算；`jitter` 模式破坏个体可反推性，适合对外分享 |
| **⚠️强制文案** | 输出与 README 必须包含：<br>`比例缩放不等于匿名化。脱敏数据对外分享仍需签署保密协议；处理真实薪资请使用本地模型部署，切勿上传至云端 API。` |
| **✅定位** | 工具集最终为 **11 个** = 9 个薪酬工具 + `run_comp_code`（PTC 沙箱）+ `desensitize_data`。README 把它作为**独立亮点**讲：对话里说一句"把这份表脱敏"当场看到效果，比 CLI 参数有说服力得多。 |

---

### FR-12 模型配置化切换

| 项 | 内容 |
|---|---|
| **输入** | `config.yaml` |
| **配置结构** | ```yaml<br>model:<br>  provider: deepseek   # deepseek / ollama / vllm<br>  name: deepseek-chat<br>  base_url: https://api.deepseek.com<br>  api_key_env: DEEPSEEK_API_KEY<br>  temperature: 0.2<br>data_safety:<br>  allow_cloud_with_real_data: false<br>  whitelist_dirs: ["data/"]<br>``` |
| **三档 Provider** | `deepseek`（云端 API）/ `ollama`（本地 Ollama，默认 `http://localhost:11434`）/ `vllm`（本地 vLLM，OpenAI 兼容，默认 `http://localhost:8000/v1`） |
| **⚠️数据安全闸门** | 当 `allow_cloud_with_real_data: false` 且输入文件**不在** `whitelist_dirs` 内时，若 `provider` 为云端 → **拒绝执行**并返回 `error.code = "DATA_SAFETY_BLOCKED"`，提示改用本地模型 |
| **业务含义** | 面试/演示用云端，真实数据用本地——一键切换，零代码改动 |
| **异常处理** | `provider` 非法→`INVALID_PARAM`；`api_key_env` 对应的环境变量缺失→`INVALID_PARAM`（提示 `如何设置`） |

---

### FR-13 `run_comp_code` — PTC（Programmatic Tool Calling）

| 项 | 内容 |
|---|---|
| **背景** | dsh 原生 Code Mode 需第一方 `dsh-code-runtime-python` 后端，本机**未安装**（BRIEFING §2.3）→ 自建 PTC 沙箱，语义等价且**离线可跑** |
| **输入** | `code: str`（Python 片段）、`timeout_ms: int = 10000`、`max_rows: int = 50`、`max_chars: int = 5000` |
| **预绑定命名空间** | 11 个工具函数（`load_salary_data` / `confirm_mapping` / `analyze_current_state` / `generate_band` / `market_benchmark` / `simulate_increase` / `simulate_pay_mix` / `calc_job_score` / `generate_report` / `desensitize_data`）+ `pd` / `np` / `json` / `math` / `print`。结果统一写入变量 `result` |
| **安全（双层）** | **静态层**：AST 遍历拦截 `Import`/`ImportFrom`（白名单外）、`open`、`eval`、`exec`、`compile`、`__import__`、`getattr`/`setattr`/`delattr`、`globals`/`locals`/`vars`/`dir`、`input`/`exit`/`quit`、双下划线属性 → `error.code = "SAFETY_BLOCKED"`<br>**运行层**：`timeout_ms` 硬超时 → `error.code = "TIMEOUT"`；异常 → `INTERNAL_ERROR`（只回中文提示，**不回 traceback 里的变量值**） |
| **输出** | `{ok, data:{result, truncated, elapsed_ms, stdout_tail(最后 200 字符)}}` |
| **业务含义** | 模型写一段代码批量调度 9 个工具、只把最终结果带回来 → 省 Token、避免"模型心算"、可复现 |
| **⚠️README 必须如实说明** | 与 dsh 原生 Code Mode 的差异：本实现为自建受限 Python 沙箱，非 dsh 官方运行时；不支持 pip 安装与文件写盘 |

---

### FR-14 图表与导出基建

| 项 | 内容 |
|---|---|
| **引擎** | Plotly 5.24.1 |
| **降级策略（硬要求）** | `kaleido` 未安装 → **不得报错**：优先 `fig.write_html()`；仅当 `kaleido` 可用时才 `fig.write_image()` 出 PNG，并置 `warnings` |
| **统一导出** | 所有 CSV 用 `encoding="utf-8-sig"`（BOM，Excel 直接打开不乱码）；所有图表落在 `report/assets/` |
| **中文显示** | 图表字体需支持中文，标题/轴标签全中文 |
| **异常处理** | 渲染异常 → `warnings` + 跳过该图，**不中断主流程** |

---

### FR-15 `main.py` — 本地端到端入口

| 项 | 内容 |
|---|---|
| **入口** | `python main.py [--data data/sample_salary.csv] [--budget 0.05] [--offline] [--out report/]` |
| **流程** | 造数校验 → 加载 → 确认映射 → 诊断 → 带宽 → 对标 → 4 策略调薪 → 固浮比 → 岗位评估 → 报告 |
| **业务含义** | 面试演示时"一条命令跑通全流程，断网也能跑"，这是可演示性的核心 |
| **退出码** | 成功 `0`；任一 P0 步骤失败 `1`（并打印中文错误原因，**不打印 traceback**） |
| **⚠️安全** | 全程日志只打印步骤名 + 汇总数字（如"红圈 31 人，占 20.67%"），**不打印任何单行员工数据** |

---

### FR-16 SKILL.md — 对话行为约束

| 项 | 内容 |
|---|---|
| **角色** | 首席薪酬官助手（CCO Copilot），精通 3P 模型、海氏/美世评估、CR 与渗透率、市场对标 |
| **强制行为** | ① 先 `load_salary_data` 预览，再引导用户确认映射；② 按数据完整度自动决定"先诊断"还是"先生成建议带宽"（`band_mid` 全缺 → 先建议带宽）；③ 拿到 JSON 结果后用自然语言解读，**只讲关键发现，不罗列全部原始数据**；④ 涉及调薪/固浮比时输出方法论前提与风险提示；⑤ 最后调用 `generate_report` |
| **⚠️红线** | **模型绝不心算薪酬数字**——所有数值必须来自工具返回的 JSON；SKILL.md 中必须显式写明"如需计算，请调用工具或写 `run_comp_code` 片段，不要自己算" |
| **示例表述** | `"红圈员工 31 人，占 20.67%，主要集中在 P1(8人)/P3(5人) 职级，年成本溢出约 95.0 万元"` |

---

## 4. 验收标准（AC，全部可机检）

### 4.0 机检基线（所有 AC 的前提）

| 项 | 约定 |
|---|---|
| **数据源** | `data/sample_salary.csv`（150 行 × 18 列，seed=20260830），`data/messy_salary.csv`（151 行 × 21 列） |
| **Python** | `C:/ProgramData/anaconda3/python.exe`（加 `PYTHONIOENCODING=utf-8`） |
| **路径风格** | `C:/...` 正斜杠；**禁止** `/c/...` |
| **数值容差** | 金额类：相对误差 ≤ `1e-6`；比率类：绝对误差 ≤ `1e-6`；人数类：**精确相等**（浮点判定用 `1e-9` 容差，且**判定前不取整**，见 K3） |
| **统一返回契约** | 成功 `{ok:true, data, warnings, meta}`；失败 `{ok:false, error:{code, message, hint, details?}}`；**任何情况下不抛异常到调用方**。已由 `src/tools/errors.py`（`ok_result` / `error_result` / `tool_guard`）落地实现，全项目统一走这三个函数。 |
| **error.code 枚举（已实现 7 个）** | `COMP_ERROR`（兜底）/ `FILE_NOT_FOUND` / `COLUMN_MISSING` / `MAPPING_NOT_CONFIRMED` / `INVALID_PARAMETER` / `SESSION_NOT_FOUND` / `UPSTREAM_MISSING` |
| **error.code 枚举（待补齐 6 个）** | `UNSUPPORTED_FORMAT` / `EMPTY_DATASET` / `NO_MARKET_DATA` / `BUDGET_INFEASIBLE` / `DATA_SAFETY_BLOCKED` / `SANDBOX_VIOLATION` / `SANDBOX_TIMEOUT`（共 7，QA 按 §4.8 的 9 个异常用例逐条验证） |

> ⚠️ **命名冲突待仲裁（PM 已上报 team-lead）**：`docs/ARCHITECTURE.md §4.11` 的 16 码表与 `src/tools/errors.py` 已实现的 7 个码**命名不一致**（如 `INVALID_PARAMS` vs `INVALID_PARAMETER`、`NO_SESSION` vs `SESSION_NOT_FOUND`、`COLUMN_NOT_FOUND` vs `COLUMN_MISSING`）。**以已落地的 `errors.py` 为准**，ARCHITECTURE.md §4.11 需回写对齐，否则 QA 按哪一份都对不上。

---

### 4.1 数据加载与字段识别（AC-01 ~ AC-04）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-01** | 标准样本加载正确 | `load_salary_data("data/sample_salary.csv")` | 行数 **150**；列数 **18**；职级 **9** 个；月薪 min/median/max = **5,400 / 16,200 / 95,600**；年度基数（月薪×12）合计 **39,170,400** 元 |
| **AC-02** | 缺失率报告正确 | 检查 `dirty_report` / 缺失率 | `job_score` **39.33%**（59/150）；`annual_total_cash` **18.67%**（28/150）；`pay_mix` **32.00%**（48/150）；`mkt_p25` **6.67%**（10/150）；`band_min/mid/max` **100.00%**；三个必填字段 **0.00%** |
| **AC-03** | 字段自动识别覆盖率 | 对 `messy_salary.csv` 的 21 列跑 `auto_suggest_mapping` | **18/18 标准字段全部命中**（`suggest` 非 None），其中 **18 列 confidence = 100**；3 个干扰列（`入职日期`/`备注`/`数据状态`）`suggest == None`；18 个标准字段**无遗漏、无重复映射** |
| **AC-04** | 脏数据清洗正确 | 对 `messy_salary.csv` 走 `load → confirm_mapping` | 原始 **151** 行；检出薪资非数值 **3** 条（`待定` / `—` / NaN）；检出重复 `emp_id` **1** 条；清洗后 **147** 行；绩效归一化后分布 **A21 / B79 / C44 / D7** |

---

### 4.2 现状诊断（AC-05 ~ AC-09）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-05** | 红绿圈人数与占比 | 用「建议带宽」（中位值 = 各职级现状中位数，**不取整**）判定 | **红圈 31 人（20.67%）**、**绿圈 26 人（17.33%）**、**合理 93 人（62.00%）**；三者之和 = 150 |
| **AC-06** | CR 分布统计量 | `cr_summary` | mean **1.0141**、std **0.1757**、min **0.6467**、P25 **0.9222**、median **1.0000**、P75 **1.1158**、max **1.5227**（容差 `1e-4`） |
| **AC-07** | 红绿圈成本测算 | `risk` | **红圈成本溢出 = 79,191 元/月（年化 95.0 万元）**；**绿圈补足到下限 = 35,073 元/月（年化 42.1 万元）** |
| **AC-08** | 逐职级现状统计表 | 与下表逐格 diff | 见 **附录 A**（count/min/P25/median/mean/P75/max 七列全对） |
| **AC-09** | 分职级红绿圈分布 | `pd.crosstab(level, flag)` | P1: 合理7/红8/绿5；P2: 19/4/5；P3: 20/5/5；P4: 17/4/3；P5: 13/4/1；P6: 7/2/3；M1: 4/3/2；M2: 4/1/1；M3: 2/0/1 |

---

### 4.3 带宽设计（AC-10 ~ AC-14）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-10** | 带宽公式三项恒等式 | 对全部 9 个职级逐一验证 | ① `abs((band_min+band_max)/2 - band_mid) < 1e-9`；② `abs((band_max-band_min)/band_min - spread) < 1e-9`；③ CR=1.0 时渗透率 = **0.500000** |
| **AC-11** | 建议带宽表数值 | `band_source="auto"` 的输出 | P3：**12,042.5532 / 14,150 / 16,257.4468**（幅度 0.35）；P1：7,422.2222 / 8,350 / 9,277.7778；M3：72,230.7692 / 93,900 / 115,569.2308。完整 9 行见**附录 B** |
| **AC-12** | 相邻带宽重叠度 | `adjacency` | 8 对相邻职级；**P1→P2 = 13.08%**、**P2→P3 = −18.88%**、P3→P4 = −1.07%、P4→P5 = 42.29%、P5→P6 = 11.18%、P6→M1 = 52.22%、M1→M2 = 11.13%、M2→M3 = 7.54%；含**负重叠（断层）2 对**（P2→P3、P3→P4），`summary.gap_pairs == 2` |
| **AC-13** | CR 与渗透率的数学关系 | 全样本逐行验证 `pen == (cr×(1+spread/2) − 1)/spread` | **最大绝对误差 < 1e-9**（实测 1.11e-15） |
| **AC-14** | 「全新设计」模式 | `mode="new"`，`base="custom"`，`anchor_level="P3"`，`anchor_mid=14150`，`midpoint_diff=0.15`，`spread="default"` | P1 中位值 **10,699.4**、P2 **12,304.3**、P3 **14,150.0**、P4 **16,272.5**、P5 **18,713.4**、P6 **21,520.4**、M1 **24,748.4**、M2 **28,460.7**、M3 **32,729.8**；且 **8 对相邻重叠度全为正**（46.05% ~ 76.75%）——与 AC-12 的"现状模式 2 处断层"形成对比，是报告里的核心论点 |

---

### 4.4 市场对标（AC-15 ~ AC-18）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-15** | 整体市场对标差距 | 个人差距的**均值**口径 | **−3.23%**（公司整体低于市场 P50 约 3%，与 BRIEFING 的造数设定一致）；逐职级中位数口径差距见**附录 D**，其中最低 **M1 −11.23%**、最高 **M3 +4.80%** |
| **AC-16** | 补齐到 P50 的成本 | `strategy="P50"` | **315,900 元/月 = 年化 379.1 万元**，占薪资基数 **9.68%**，涉及 **96 人** |
| **AC-17** | 补齐到 P75 的成本 | `strategy="P75"` | **857,400 元/月 = 年化 1,028.9 万元**，占 **26.27%**，涉及 **135 人**；且 `cost(P75) > cost(P50)`（单调性） |
| **AC-18** | 分位值策略成本 | `strategy="by_family"` | 销售/技术→P75、管理/职能→P50、操作→P25；**498,000 元/月 = 年化 597.6 万元**，占 **15.26%**，涉及 **99 人**；且 `cost(P25策略) < cost(by_family) < cost(P75)` |

---

### 4.5 调薪模拟（AC-19 ~ AC-24）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-19** | **预算守恒（核心）** | 4 策略 × 3 档预算（3% / 5% / 8%）共 12 组 | 每组 `abs(cost - budget) / budget ≤ 1e-6`；`usage_rate == 1.000000`。预算额：3% = **1,175,112** 元；5% = **1,958,520** 元；8% = **3,133,632** 元 |
| **AC-20** | 策略 A 均匀性 | 5% 档下检查每人调薪率 | 全部 **= 预算率**（5.000000%），标准差 = 0 |
| **AC-21** | 策略 C 绩效梯度 | 5% 档下按绩效分组查调薪率 | **A = 9.060064%**、**B = 6.040042%**、**C = 2.516684%**、**D = 0.000000%**；且 `rate_A : rate_B : rate_C : rate_D == 1.8 : 1.2 : 0.5 : 0`（精确，容差 1e-9） |
| **AC-22** | 策略 B 消除绿圈 | 3% / 5% / 8% 三档 | 调薪后**绿圈人数 == 0**（三档均成立）；第①步补绿圈成本 **472,889** 元，涉及 **26** 人 |
| **AC-23** | 策略 D 冻结红圈 | 5% 档 | 调薪前判定为红圈的员工，调薪额 **全部 == 0**；且非红圈员工调薪额之和 == 预算 |
| **AC-24** | `rebase_band=True` 恒等性（✅ team-lead 裁定保留） | `strategies=["A"]`，`budget_pct=0.05`，`rebase_band=True` | 调薪后红/绿/合理 = **31 / 26 / 93**，与调薪前**完全一致**（恒等，容差 0 人） |
| **AC-24b** | **双情景并列（强制）** | 报告第 5 节 + `simulate_increase` 输出 | 每一个策略都必须同时给出 `rebase_band=False` 与 `True` 两栏（`scenarios` 数组，长度 == 2）；`False` 为默认/主口径；两栏的 `total_cost` 必须**完全相同**（rebase 只改判定基准，不改调薪额） |
| **AC-25** | 产出物完整 | 检查 `report/` | 每策略 1 份个人调薪明细 CSV（≥4 份），含列 `emp_id / level / perf_grade / 调薪前月薪 / 调薪额 / 调薪率 / 调薪后月薪 / 调薪后CR / 调薪后判定`；CR 分布对比图 + 成本柱状图各 1 张 |

> **参考值（不作硬 AC，因依赖实现细节）**：`rebase_band=False`、5% 档下策略 A 调薪后红圈 41 / 绿圈 17 / 合理 92（红圈上升属预期行为，见 FR-06 强制解释文案 AC-33）。
> **PM 说明**：`rebase_band=False` 时红圈由 31 升至 41，这个数字**不得隐藏、也不得通过调参抹平**——它是"带宽必须年度重定级"的最有力论据，报告与面试讲解都要主动展示它。

---

### 4.6 固浮比与岗位评估（AC-26 ~ AC-29）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-26** | 固浮比收入公式 | 以年总现金 300,000 为基数，测各序列 | `实际收入(达成率 a) = TC×(固定%) + TC×(浮动%)×a`；**a = 1.0 时实际收入 == 300,000**（各序列误差 < 1e-9） |
| **AC-27** | 固浮比曲线端点 | 销售 40:60 | 达成率 **0% → 120,000**；**50% → 210,000**；**100% → 300,000**；**150% → 390,000** |
| **AC-28** | 固浮比风险提示文案 | `grep`/正则匹配输出与报告 | 必须包含完整句：`高浮动比例的前提条件是：业绩可量化、可归因到个人、结算周期短；长周期协作型业务强行高浮动会破坏协作。` |
| **AC-29** | 岗位评估换算与职级归属（K8，已修复） | `calc_job_score(model="hay", scores={...})` | 见 **§7 样例 6**。硬断言：① `weighted_to_total(8.875) == 1420.0`；② `weighted_to_total(1.0) == 160.0`、`weighted_to_total(10.0) == 1600.0`；③ 高管样例 → **M3**（**回归红线：若返回 P1 即判定 K8 换算未生效，一票否决**）；④ 边界左闭右开：`score_to_level(299.9)==P1`、`score_to_level(300)==P2`、`score_to_level(1350)==M3`；⑤ `mock_data` 9 个职级的 `job_score` 均值反查职级一致率 **9/9** |
| **AC-29b** | 缩放系数单一真理源 | grep `src/tools/` | `160` 只允许出现在 `schemas.JOB_SCORE_SCALE` 一处；`jobeval.py` 必须调用 `weighted_to_total()`，**不得内联 `× 160`** |

---

### 4.7 报告与图表（AC-30 ~ AC-33）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-30** | 报告 7 节齐全 | grep 标题 | 7 个固定标题全部存在：`1 执行摘要` / `2 数据概览与字段映射说明` / `3 薪酬现状诊断` / `4 带宽设计与市场对标建议` / `5 调薪/套改方案对比与推荐` / `6 固浮比与激励建议` / `7 风险提示与实施路线图` |
| **AC-31** | 报告体量与关键数字 | 字符数 + 关键串 | 正文 ≥ **6,000** 字符；必须出现 `31 人`、`20.67%`、`95.0 万元`、`39,170,400` 中的至少 3 项 |
| **AC-32** | 图表产出 | 列目录 | `report/assets/` 下 `.html` 图表 **≥ 6** 个；`kaleido` 缺失时**不报错**且仍有 ≥6 个 HTML 图 |
| **AC-33** | 调薪解释文案 | 正则匹配第 5 节 | 当 `rebase_band=False` 且调薪后红圈上升时，必须出现 `带宽锚定` 与 `普调会系统性抬高红圈比例` 两段关键表述 |

---

### 4.8 异常与容错（AC-34 ~ AC-37）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-34** | 五类异常输入**不抛异常、返回友好错误** | 逐个调用并断言 | 见下表 AC-34 明细 |
| **AC-35** | 进程不崩溃 | 跑完 5 个异常用例 | 进程 `exit code == 0`；stdout 无 `Traceback` 字样 |
| **AC-36** | 错误码守恒 | 断言 `error.code` | 全部落在 §4.0 的 13 个枚举内；`message` 为中文且长度 ≥ 10；`hint` 非空 |
| **AC-37** | 空值与部分缺失容错 | 用 `--no-market` 造一份无市场列的数据跑全流程 | `market_benchmark` 返回 `ok=false` + `MISSING_REQUIRED_FIELD`；其余 6 个模块全部 `ok=true` |

**AC-34 明细（5 类异常用例）**：

| # | 异常场景 | 构造方式 | 期望 `error.code` |
|---|---|---|---|
| 1 | 文件不存在 | `load_salary_data("data/not_exist.csv")` | `FILE_NOT_FOUND` |
| 2 | 必填列缺失 | 删掉 `level` 列后 `confirm_mapping` | `MISSING_REQUIRED_FIELD` |
| 3 | 非数值混入 | `monthly_salary = "待定"` | 不报错；该行计入 `dropped_non_numeric`，返回 `ok=true` + `warnings` |
| 4 | 全部为空/全被丢弃 | 造一份月薪全为 `—` 的表 | `ALL_ROWS_DROPPED` |
| 5 | 重复数据 | 同一 `emp_id` 重复 3 行 | 不报错；`clean_stats.dropped_dup == 2`，返回 `ok=true` + `warnings` |
| 6 | 参数非法 | `simulate_increase(budget_rate=-0.1)` | `INVALID_PARAM` |
| 7 | 安全拦截 | `run_comp_code("import os")` | `SAFETY_BLOCKED` |
| 8 | 超时 | `run_comp_code("while True: pass", timeout_ms=3000)` | `TIMEOUT` |
| 9 | 数据安全闸门 | `provider=deepseek` + 非白名单目录文件 | `DATA_SAFETY_BLOCKED` |

---

### 4.9 数据安全与可复现（AC-38 ~ AC-41）

| AC | 验收标准 | 机检方式 | 期望值 |
|---|---|---|---|
| **AC-38** | 造数零告警 | `python -W error::FutureWarning mock_data.py` | 退出码 **0**，stderr **无任何 Warning**；产出 150 / 151 行不变 |
| **AC-39** | 脱敏正确性 | `desensitize_data(mode="scale")` | `corr(原始月薪, 脱敏月薪) == 1.000000`（容差 1e-9）；`median_ratio` ∈ [0.8, 1.2] 且与返回 `factor` 一致（容差 1e-9）；姓名 100% 匹配 `^[\u4e00-\u9fa5]\*\*$`；`emp_id` 一一对应无重复 |
| **AC-40** | 日志不泄露数据表 | 抓 `main.py` 全量 stdout/stderr + 日志文件 | ① 单条日志 ≤ **500** 字符；② 不出现连续 ≥ **10** 行的表格输出；③ 不出现 ≥ **10** 个 `emp_id` 的枚举；④ 姓名列值全部匹配脱敏正则 |
| **AC-41** | 可复现 | 固定 seed 跑两次 | ① 两次 `mock_data.py -n 150 --seed 20260830` 产出的 `sample_salary.csv` **sha256 完全一致**；② 两次 `main.py` 产出的报告剔除 timestamp 后**逐字节一致** |

---

## 5. 非功能需求（NFR）

| ID | 类别 | 要求 | 机检方式 |
|---|---|---|---|
| **NFR-01** | 数据安全 · 本地优先 | 默认配置必须能在**完全离线**下跑通全流程；`data/` 之外的数据配合云端 provider 时触发 `DATA_SAFETY_BLOCKED` | AC-37、AC-34#9 |
| **NFR-02** | 数据安全 · 脱敏 | 演示/测试/默认加载**一律使用模拟数据**；提供 `desensitize_data`；README 与 CLI 输出必须含"切勿上传云端 API"警告 | AC-39 + README grep |
| **NFR-03** | **模型绝不心算** | 所有薪酬数值必须由 Python 计算。SKILL.md 与工具描述中显式禁止模型自行计算；`src/tools/**` 下不得出现硬编码的薪酬常数（除 `schemas.py` 的参数字典与测试 fixture） | grep `src/tools/` 排除 `schemas.py` 后，无 `1[0-9]{4}` 形式的裸薪资常量 |
| **NFR-04** | 中文注释覆盖 | ① 每个 public 函数/工具入口必须有中文 docstring（**100%**，AST 可检）；② 涉及薪酬公式的数值运算语句，其前后 5 行内必须存在含中文的注释（**≥ 95%**）；③ 全部注释行中含中文的比例 **≥ 90%** | 脚本 AST 遍历 + 正则统计 |
| **NFR-05** | 异常处理覆盖 | 5 类异常（文件不存在 / 列缺失 / 空值 / 非数值 / 重复数据）+ 参数非法 + 安全拦截 + 超时，**每个工具都要 try-except + 中文友好返回** | AC-34 ~ AC-36 |
| **NFR-06** | 离线可跑 | `main.py` 全流程在断网（或 `COMP_AGENT_OFFLINE=1` 禁用 socket）条件下成功退出，退出码 0 | QA 断网实测 |
| **NFR-07** | 图表产出 | Plotly 生成 HTML（必需）；`kaleido` 可用时额外出 PNG（可选降级，**不得报错**） | AC-32 |
| **NFR-08** | 可复现 | 固定随机种子；同输入同输出 | AC-41 |
| **NFR-09** | 零告警 | 全流程不产生 `FutureWarning` / `DeprecationWarning` | AC-38（`python -W error::FutureWarning`） |
| **NFR-10** | 可讲解性 | 每个方法论（3P / 海氏 / 美世 / CR / 渗透率 / 红绿圈 / 固浮比前提 / 分位值策略）在 README「模块方法论」中都有独立条目，写明**公式 + 业务含义 + 依据** | README 章节 grep |

---

## 6. 模块依赖顺序与并行建议

### 6.1 依赖关系图（前置 → 后继）

```
【L0 地基，无依赖，最先做】
  schemas.py (已完成)  ─┬─→ FR-14 图表与导出基建  ─┐
  config.yaml / FR-12  ─┘                          │
                                                   │
【L1 数据入口】                                     │
  FR-01 load_salary_data  ──→ FR-02 confirm_mapping ┤
                                                   │
【L2 核心计算（依赖 L1，内部可并行）】               │
  ├─ FR-04 generate_band      （被 FR-03/05/06 依赖）
  ├─ FR-03 analyze_current_state（依赖 FR-04 的建议带宽）
  ├─ FR-05 market_benchmark   （依赖 FR-04）
  ├─ FR-07 simulate_pay_mix   （仅依赖 L1）
  └─ FR-08 calc_job_score     （仅依赖 schemas，完全独立）
                                                   │
【L3 组合分析】                                     │
  FR-06 simulate_increase  （依赖 FR-03 + FR-04）   │
                                                   │
【L4 输出】                                         │
  FR-09 generate_report    （依赖 L2 + L3 + FR-14）─┤
  FR-15 main.py            （依赖全部）             │
                                                   │
【L5 集成与安全（可并行于 L2~L4）】                  │
  FR-10 mock_data（已完成）/ FR-11 desensitize      │
  FR-12 模型配置切换                                │
  FR-13 run_comp_code（依赖 FR-01~FR-11 全部注册完）┘
  FR-16 SKILL.md（依赖工具签名稳定）
```

### 6.2 前置/可并行判定表

| 模块 | 性质 | 说明 |
|---|---|---|
| `schemas.py` | **已完成的前置** | 单一真理源，任何人不得擅自修改；要改必须走 `team-lead` |
| FR-14 图表与导出基建 | **强前置** | FR-03/04/05/06/07/09 都要用；建议**最先实现**（含 kaleido 降级） |
| FR-01 → FR-02 | **强前置链** | 后续所有模块的输入都是 FR-02 产出的标准面板 |
| FR-04 generate_band | **强前置** | FR-03（无带宽时）、FR-05、FR-06 都要调它 |
| FR-03 / FR-05 / FR-07 / FR-08 | **可并行** | 四者互不依赖（FR-03、FR-05 只依赖 FR-04）→ 工程师 A/B 可分工并行 |
| FR-06 | **串行在 FR-03 之后** | 需要 FR-03 的红绿圈判定结果作为输入 |
| FR-09 / FR-15 | **最后** | 必须在所有分析模块稳定后实现 |
| FR-10 / FR-11 / FR-12 | **完全独立，随时可做** | 与 L2~L4 无耦合，可安排给任一空闲人力 |
| FR-13 run_comp_code | **最后** | 需要 11 个工具全部注册完成才能绑定命名空间 |
| FR-16 SKILL.md | **工具签名稳定后** | 与 FR-13 可并行撰写，但需在 QA 前对齐工具名与参数 |

### 6.3 建议排期分组（供 team-lead 参考）

| 批次 | 内容 | 可并行人数 |
|---|---|---|
| 批次 0 | FR-14 图表基建 + FR-12 config.yaml | 1~2 |
| 批次 1 | FR-01 + FR-02（数据入口） | 1 |
| 批次 2 | FR-04 → {FR-03, FR-05} ／ FR-07 ／ FR-08 | 3（FR-04 必须先出） |
| 批次 3 | FR-06 ／ FR-11 ／ FR-16 | 3 |
| 批次 4 | FR-09 + FR-15（端到端串联） | 1~2 |
| 批次 5 | FR-13（PTC）+ QA 全流程 | 1~2 |

---

## 7. 金标准数值样例（手算可校验，QA 照此对账）

> 全部取自 `data/sample_salary.csv` 的**真实行**。基准带宽 = 「建议带宽」（中位值 = 各职级**现状中位数**，幅度 = `get_level_tier()` 默认值）。
> **P3 职级基准带宽**：下限 **12,042.5532** / 中位值 **14,150** / 上限 **16,257.4468**（幅度 0.35）。

### 样例 1 — 带宽公式手算（P3 职级）

```
已知：P3 现状月薪中位数 = 14,150 元；get_level_tier("P3") → ("专业/技术", 0.35)

下限 = 中位值 / (1 + 幅度/2) = 14,150 / (1 + 0.175) = 14,150 / 1.175 = 12,042.5532 元
上限 = 下限 × (1 + 幅度)      = 12,042.5532 × 1.35   = 16,257.4468 元

校验 1：(12,042.5532 + 16,257.4468) / 2 = 14,150.0000  ✓ 等于中位值
校验 2：(16,257.4468 - 12,042.5532) / 12,042.5532 = 0.350000  ✓ 等于幅度
校验 3：CR = 1.0 时渗透率 = (14,150 - 12,042.5532) / 4,214.8936 = 0.500000  ✓
```

### 样例 2 — 红圈判定（E0001 宋**）

| 项 | 值 | 手算过程 |
|---|---|---|
| 员工 | **E0001 宋\*\*，P3 技术专家，产品技术部，绩效 B，司龄 2.6 年** | — |
| 月薪 | **19,900** 元 | — |
| CR | **1.406360** | `19,900 / 14,150 = 1.406360` |
| 判定 | **红圈** | `CR 1.406 > 1.20` ✓ **且** `19,900 > 16,257.4468（上限）` ✓ —— 两个判据同时成立 |
| 渗透率 | **1.864210** | `(19,900 − 12,042.5532) / 4,214.8936 = 7,857.4468 / 4,214.8936 = 1.864210`（>1，即超出上限 86% 个带宽宽） |
| 超出上限金额 | **3,642.55** 元/月 | `19,900 − 16,257.4468` |
| 市场对标 | **+29.22%** | 市场 P50 = 15,400 → `(19,900 − 15,400) / 15,400 = +29.22%`（显著高于市场） |
| 关系校验 | ✓ | `pen = (1.406360 × 1.175 − 1) / 0.35 = (1.652473 − 1) / 0.35 = 1.864210` |

### 样例 3 — 绿圈判定（E0062 韩**）

| 项 | 值 | 手算过程 |
|---|---|---|
| 员工 | **E0062 韩\*\*，P3 技术专家，研发中心，绩效 B，司龄 3.3 年** | — |
| 月薪 | **10,100** 元 | — |
| CR | **0.713781** | `10,100 / 14,150 = 0.713781` |
| 判定 | **绿圈** | `CR 0.714 < 0.80` ✓ **且** `10,100 < 12,042.5532（下限）` ✓ |
| 渗透率 | **−0.460878** | `(10,100 − 12,042.5532) / 4,214.8936 = −1,942.5532 / 4,214.8936 = −0.460878`（低于下限） |
| 补到下限所需 | **1,942.55** 元/月 | `12,042.5532 − 10,100` |
| 市场对标 | **−33.55%** | 市场 P50 = 15,200 → `(10,100 − 15,200) / 15,200 = −33.55%`（流失风险高） |
| 关系校验 | ✓ | `pen = (0.713781 × 1.175 − 1) / 0.35 = (0.838693 − 1) / 0.35 = −0.460878` |

### 样例 4 — 合理区间（E0035 孙**）

| 项 | 值 | 手算过程 |
|---|---|---|
| 员工 | **E0035 孙\*\*，P3 薪酬绩效经理，法务合规部，绩效 C，司龄 0.5 年** | — |
| 月薪 | **14,300** 元 | — |
| CR | **1.010601** | `14,300 / 14,150 = 1.010601` |
| 判定 | **合理** | `0.80 ≤ 1.0106 ≤ 1.20` ✓ 且 `12,042.5532 ≤ 14,300 ≤ 16,257.4468` ✓ |
| 渗透率 | **0.535588** | `(14,300 − 12,042.5532) / 4,214.8936 = 0.535588`（略高于中位） |
| 市场对标 | **−6.54%** | 市场 P50 = 15,300 → `(14,300 − 15,300) / 15,300 = −6.54%` |
| 关系校验 | ✓ | `pen = (1.010601 × 1.175 − 1) / 0.35 = (1.187456 − 1) / 0.35 = 0.535588` |

### 样例 5 — 调薪模拟（策略 C，预算率 5%，绩效 A 员工 E0040 陈**）

```
基数总额    = Σ(月薪 × 12) = 39,170,400 元
预算        = 39,170,400 × 5% = 1,958,520 元
Σ(w_i × 基数_i) = 38,910,720 元
加权平均权重   = 38,910,720 / 39,170,400 = 0.993371

员工 E0040：P3 工程师，月薪 13,800，绩效 A（权重 1.8）
  调薪率   = 预算 × w / Σ(w×基数) = 1,958,520 × 1.8 / 38,910,720 = 9.060064%
  月调薪额 = 13,800 × 12 × 9.060064% / 12 = 165,600 × 9.060064% / 12 = 1,250.29 元
  调薪后月薪 = 13,800 + 1,250.29 = 15,050.29 元
  调薪后 CR  = 15,050.29 / 14,150 = 1.063625  （晋升到带宽中上部，仍在合理区间）

四档绩效调薪率（同一策略下，与个体薪资无关，仅取决于绩效等级）：
  A = 1,958,520 × 1.8 / 38,910,720 = 9.060064%
  B = 1,958,520 × 1.2 / 38,910,720 = 6.040042%
  C = 1,958,520 × 0.5 / 38,910,720 = 2.516684%
  D = 1,958,520 × 0.0 / 38,910,720 = 0.000000%
  校验：A : B : C : D = 9.060064 : 6.040042 : 2.516684 : 0 = 1.8 : 1.2 : 0.5 : 0  ✓
  校验：Σ(调薪额) = 1,958,520 元 = 预算  ✓ 守恒
```

### 样例 6 — 岗位价值评估（海氏三要素，⚠️依赖 K8 换算 `总分 = 160 × W`）

```
模型：海氏三要素法   权重：知识技能 0.40 / 解决问题 0.25 / 应负责任 0.35
被测岗位：事业部总经理（M3 参考岗）

子维度打分（1~10）：
  知识技能：专业知识深度 9、管理技能要求 9、人际沟通技能 9   → 因子得分 = 27/3 = 9.000
  解决问题：思维环境复杂度 9、思维难度挑战 8                 → 因子得分 = 17/2 = 8.500
  应负责任：行动自由度 9、对结果的影响 9、影响范围大小 9      → 因子得分 = 27/3 = 9.000

加权因子分 W = 9.000×0.40 + 8.500×0.25 + 9.000×0.35
             = 3.600 + 2.125 + 3.150 = 8.875
总分 = 160 × 8.875 = 1,420.0 分

score_to_level(1420) → 命中区间 [1350, 99999) → 建议职级 M3  ✓
交叉校验：mock_data.py 中 M3 职级的岗位得分基准值正是 1420  ✓（量纲对齐）

对照组（同模型不同量级）：
  生产操作岗  知识(2,1,2)/解决(2,2)/责任(1,2,1) → W = 1.6333 → 总分 261.3 → P1
  工程师      知识(4,2,3)/解决(4,4)/责任(3,4,3) → W = 3.3667 → 总分 538.7 → P3
  资深技术专家 知识(7,4,6)/解决(7,6)/责任(6,7,6) → W = 6.1083 → 总分 977.3 → P6
```

---

## 8. Out of Scope（明确不做）

| # | 不做的事 | 原因 | 若被追问的回应 |
|---|---|---|---|
| 1 | **真实社保 / 公积金 / 个人所得税计算** | 各地基数、比例、专项附加扣除政策频繁变动，且与薪酬诊断无关 | "本工具只做**应发现金**口径的结构诊断，社保个税属于薪酬核算系统职责；接入需要实时政策库，会引入合规风险" |
| 2 | **对接真实 HR 系统 / 数据库**（SAP、北森、Workday 等） | 需要客户授权与接口文档，作品集阶段无此条件 | "已做标准字段映射层，接任何系统只要导出一份表即可；接口对接是后续产品化工作" |
| 3 | **股权 / 期权 / 长期激励（LTI）估值** | 估值模型复杂（Black-Scholes / 蒙特卡洛），且多数中小企业无 LTI | "本工具聚焦**年度总现金**；LTI 可作为独立模块扩展" |
| 4 | **真实市场薪酬数据库**（Mercer / 韬睿惠悦 / 米高蒲志） | 商业数据库需付费授权 | "市场分位值由用户自行导入（`mkt_p25/p50/p75` 三列）；本工具负责**对标测算与策略选择**，不提供数据源" |
| 5 | **多人协作 / 权限管理 / 审批流** | 作品集聚焦分析能力，不做企业级 SaaS | "单机本地工具，权限由操作系统文件权限保证" |
| 6 | **Web UI / 前端页面** | dsh 已提供对话式交互入口；图表以 HTML 文件产出 | "交互层交给 dsh，本插件只做工具与分析内核" |
| 7 | **模型微调 / 训练** | 与"Function Calling + Python 计算"的定位冲突 | "刻意**不让**模型学薪酬数字——计算必须确定性、可复现、可审计" |
| 8 | **真实员工数据的云端处理** | 数据安全红线 | "默认本地部署；云端仅用于演示模拟数据" |
| 9 | **dsh 原生 Code Mode / `dsh-code-runtime-python`** | 本机未安装该后端（BRIEFING §2.3） | "自建 PTC 沙箱语义等价且离线可跑，README 已如实标注差异" |
| 10 | **多币种 / 跨国薪酬** | 涉及汇率、外派补贴、税务平衡，超出范围 | "人民币单一口径" |

---

## 附录 A — 逐职级现状统计表（AC-08 对账基准）

单位：元/月。分位数用**线性插值**（K1）。

| 职级 | 层级 | 幅度 | n | min | P25 | median | mean | P75 | max |
|---|---|---|---|---|---|---|---|---|---|
| P1 | 基层/操作 | 0.25 | 20 | 5,400 | 7,425 | 8,350 | 8,385.00 | 9,425 | 11,400 |
| P2 | 基层/操作 | 0.28 | 28 | 7,500 | 9,650 | 10,300 | 10,267.86 | 11,000 | 14,100 |
| P3 | 专业/技术 | 0.35 | 30 | 10,100 | 13,525 | 14,150 | 14,363.33 | 15,875 | 19,900 |
| P4 | 专业/技术 | 0.38 | 24 | 13,300 | 18,100 | 19,400 | 20,045.83 | 21,400 | 27,900 |
| P5 | 中层/专家 | 0.45 | 18 | 19,200 | 22,325 | 24,350 | 24,911.11 | 27,525 | 31,100 |
| P6 | 中层/专家 | 0.48 | 12 | 23,000 | 31,050 | 34,500 | 34,333.33 | 39,200 | 43,000 |
| M1 | 中层管理 | 0.45 | 9 | 33,100 | 37,700 | 41,900 | 45,022.22 | 51,300 | 63,800 |
| M2 | 高层管理 | 0.55 | 6 | 46,700 | 54,200 | 61,050 | 61,700.00 | 63,775 | 84,800 |
| M3 | 高层管理 | 0.60 | 3 | 71,700 | 82,800 | 93,900 | 87,066.67 | 94,750 | 95,600 |

## 附录 B — 建议带宽表（AC-11 对账基准，未取整）

| 职级 | tier | 幅度 | band_min | band_mid | band_max | 中位值级差 |
|---|---|---|---|---|---|---|
| P1 | 基层/操作 | 0.25 | 7,422.2222 | 8,350 | 9,277.7778 | — |
| P2 | 基层/操作 | 0.28 | 9,035.0877 | 10,300 | 11,564.9123 | 23.35% |
| P3 | 专业/技术 | 0.35 | 12,042.5532 | 14,150 | 16,257.4468 | 37.38% |
| P4 | 专业/技术 | 0.38 | 16,302.5210 | 19,400 | 22,497.4790 | 37.10% |
| P5 | 中层/专家 | 0.45 | 19,877.5510 | 24,350 | 28,822.4490 | 25.52% |
| P6 | 中层/专家 | 0.48 | 27,822.5806 | 34,500 | 41,177.4194 | 41.68% |
| M1 | 中层管理 | 0.45 | 34,204.0816 | 41,900 | 49,595.9184 | 21.45% |
| M2 | 高层管理 | 0.55 | 47,882.3529 | 61,050 | 74,217.6471 | 45.70% |
| M3 | 高层管理 | 0.60 | 72,230.7692 | 93,900 | 115,569.2308 | 53.81% |

## 附录 C — 逐职级市场对标差距（AC-15 对账基准）

| 职级 | n | 公司月薪中位数 | 市场P50中位数 | 中位数口径差距 | 个人均值口径差距 |
|---|---|---|---|---|---|
| P1 | 20 | 8,350 | 8,250 | +1.21% | −2.33% |
| P2 | 28 | 10,300 | 10,800 | −4.63% | −3.24% |
| P3 | 30 | 14,150 | 15,000 | −5.67% | −6.65% |
| P4 | 24 | 19,400 | 19,900 | −2.51% | −6.04% |
| P5 | 18 | 24,350 | 27,300 | −10.81% | −12.92% |
| P6 | 12 | 34,500 | 36,450 | −5.35% | −3.16% |
| M1 | 9 | 41,900 | 47,200 | −11.23% | −13.79% |
| M2 | 6 | 61,050 | 62,700 | −2.63% | −5.51% |
| M3 | 3 | 93,900 | 89,600 | +4.80% | +3.07% |
| **整体** | **150** | **16,200** | **15,600** | **+3.85%** | **−3.23%** |

> 注：中位数口径（+3.85%）与个人均值口径（−3.23%）符号相反，是因为低职级人数多、高职级人数少——**这正是薪酬诊断里"用错口径会得出相反结论"的经典陷阱**，报告中必须同时给出两个口径并说明差异（QA 检查报告是否同时出现这两个数字）。

## 附录 D — QA 机检命令清单

```bash
# 0. 环境（Windows Git Bash，路径用 C:/ 正斜杠）
PY="C:/ProgramData/anaconda3/python.exe"
ROOT="."  # 在仓库根目录执行本脚本即可，无需写死绝对路径
export PYTHONIOENCODING=utf-8

# 1. 造数零告警（AC-38）
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY -W error::FutureWarning mock_data.py

# 2. 端到端跑通（AC-30~33, NFR-06）——断网执行
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY main.py --data data/sample_salary.csv --budget 0.05

# 3. 异常用例（AC-34~36）
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY -m pytest tests/test_edge_cases.py -q

# 4. 数值对账（AC-05~29）：QA 自写脚本，逐条断言本 PRD §4 的期望值
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY tests/verify_golden.py

# 5. 注释覆盖率（NFR-04）
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY tests/check_docstring_coverage.py

# 6. 日志泄露检查（AC-40）
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY main.py > /tmp/run.log 2>&1 && \
  PYTHONIOENCODING=utf-8 $PY tests/check_log_safety.py /tmp/run.log

# 7. 可复现（AC-41）
cd "$ROOT" && PYTHONIOENCODING=utf-8 $PY mock_data.py -n 150 --seed 20260830 && \
  sha256sum data/sample_salary.csv
```

---

## 附录 E — FR × AC 覆盖矩阵

| FR | 覆盖的 AC |
|---|---|
| FR-01 | AC-01, AC-02, AC-03, AC-04, AC-34, AC-35, AC-36 |
| FR-02 | AC-03, AC-04, AC-34, AC-36, AC-37 |
| FR-03 | AC-05, AC-06, AC-07, AC-08, AC-09, AC-13 |
| FR-04 | AC-10, AC-11, AC-12, AC-14 |
| FR-05 | AC-15, AC-16, AC-17, AC-18, AC-37 |
| FR-06 | AC-19, AC-20, AC-21, AC-22, AC-23, AC-24, AC-25, AC-33 |
| FR-07 | AC-26, AC-27, AC-28 |
| FR-08 | AC-29 |
| FR-09 | AC-30, AC-31, AC-32, AC-33 |
| FR-10 | AC-38, AC-41 |
| FR-11 | AC-39, AC-40 |
| FR-12 | AC-34(#9), NFR-01 |
| FR-13 | AC-34(#7, #8) |
| FR-14 | AC-32, NFR-07 |
| FR-15 | AC-30~AC-33, AC-40, AC-41, NFR-06 |
| FR-16 | NFR-03, NFR-10 |

**合计：16 条 FR / 43 条 AC / 10 条 NFR / 10 条口径锁定（K1~K10）/ 6 组金标准样例 / 10 条 Out of Scope。**

---

## 附：口径裁定记录（team-lead 2026-08-30）

| # | 待裁定项 | 裁定 | 落点 |
|---|---|---|---|
| 1 | 岗位评估量纲断层（必现 bug） | **修复**：`JOB_SCORE_SCALE = 160.0` + `weighted_to_total(w)` 已入 `schemas.py` | K8 / FR-08 / AC-29 / AC-29b / §7 样例 6 |
| 2 | `rebase_band` 参数 | **批准，默认 `False`**，报告**并列展示双情景** | K7 / FR-06 / AC-24 / AC-24b / AC-33 |
| 3 | 带宽判定前不取整 | **批准**；取整只作用于对外发布带宽表并显式标注 | K3 / K9 |
| 4 | 调薪基数 | **批准** `Σ月薪×12`；`annual_total_cash` 仅作个人层面展示 | K2 / K10 |
| 5 | `desensitize_data` 注册为第 11 个工具 | **批准**；README 方法论章节作为独立亮点 | FR-11 / §3.0 |
