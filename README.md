# salary-diagnosis-cli · 薪酬诊断 Agent

![CI](https://github.com/helibeiqi/salary-diagnosis-cli/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Release](https://img.shields.io/badge/release-v0.2.2-orange)

> 做薪酬的人大概都有同感：AI 能帮上大忙，但有一个地方它不能碰——**数字**。
>
> 这个工具让大模型负责听懂人话、串起流程、把结果讲成人话；所有跟钱有关的计算，
> 一律交给一段确定性程序去算。上传脱敏工资表，它就能跑完「数据读取 → 字段识别 →
> 现状诊断 → 带宽设计 → 市场对标 → 调薪模拟 → 固浮比分析 → 岗位评估 → 报告导出」
> 这一整条链。
>
> **两种用法，按需选择**：
> - `run_agent.py` 本地 CLI 入口 —— **零外部依赖环境**，不需要 dsh、不需要 Ollama
>   也能跑通确定性流水线（`--pipeline` 模式下模型完全不参与计算）；
> - dsh 插件层（`src/plugins/comp-tool/`）—— 可选增强，提供自然语言对话形态，
>   需要已安装 dsh 运行时与本地模型。
>
> 项目内部开发名 `comp-agent-harness`，开源仓库名 `salary-diagnosis-cli`，二者指同一项目。

---

## 三十秒上手

```bash
git clone https://github.com/helibeiqi/salary-diagnosis-cli.git
cd salary-diagnosis-cli
pip install -r requirements.txt
python run_agent.py --demo
```

`--demo` 用的是内置合成模拟数据（与任何真实自然人无关），零交互跑完整条链，
报告落在 `report/`。**这四步不需要任何大模型，也不需要联网。**

想用网页版：`pip install -e ".[web]"` 之后 `streamlit run app.py`。

---

## 这个项目从哪来

我在一家芯片制造公司做人力，公司三百多人，薪酬这块的事是我负责。

芯片厂的用工跟一般公司不太一样。产线要连轴转，洁净室里倒班；同一条线上，
正式工和外包是混在一起的——产线工人大半是外包，工程师和职能岗基本是正式工。
两套用工，两套定薪逻辑，摆在同一张表里看。

于是每次做薪酬复盘、排调薪方案，都是一张大表打底：谁落在带宽的哪一段、
谁该调、调多少、全年加薪总额会不会超。领导要动一个参数——今年研发口多倾斜两个点，
或者某个岗位序列整体按住——整张表就得从头再算一遍。**改一版，大半天。**

算得多了就发现，真正的难点不是算，是**看不清**：这三百多个人现在到底站在市场的什么位置？
谁明显偏低、得赶紧补，谁已经偏高、今年不能再加了？
结构不清楚，后面所有的测算都是在沙子上盖楼。

所以我想要一个能随时看清"现在这批人到底站在哪"的东西。
中间也试过直接把名单和预算丢给通用 AI，让它排一版方案。它给得很快，也很好看，
每个人的涨幅都有说法。问题出在加总的时候：**总数比预算多出来一截**。
而如果它算出来的是「少了一截」，我大概扫一眼就过去了。

所以后来定了条规矩：**所有跟钱有关的数，一律不让模型算。**
模型负责听懂人话、串起流程、把结果讲成人话；算盘，留在确定性程序手里。

这个仓库就是照这条规矩搭起来的。现在把它开源出来——如果你也在做薪酬诊断、
调薪方案或者市场对标，希望它能替你省下一些重复劳动。

---

## 可信，靠这三件事

| | 做法 | 为什么 |
|---|---|---|
| **模型不算数** | 所有薪酬数值由确定性程序执行；模型只做语义理解、工具调度、自然语言解读 | 结果可复现、可被脚本对账，不会「错得很有道理」 |
| **口径只有一个地方** | 业务常量集中在 `src/tools/schemas.py`，沙箱与主路径调用同一份 handler | 改口径不用动模型，前后口径零漂移 |
| **数据默认不出门** | 默认本地模型，本地挂了只报错、**不回退云端**；一键脱敏；报告强制安全声明 | 薪资是 HR 手中最敏感的数据，出门就收不回来 |

三条里最要紧的是第一条。技术实现见下方「二、架构」与「七、数据安全声明」。

---

## 一、项目介绍（解决 HR 的什么痛点）

| 痛点（Excel 手搓时代） | 本项目的答案 |
|---|---|
| 带宽靠拍脑袋，各职级薪资区间不统一 | `generate_band` 按层级默认幅度 + 中位值级差自动生成带宽表，含重叠度校验 |
| 红圈/绿圈靠肉眼在透视表里找 | `analyze_current_state` 用 CR 与带宽边界自动判定，给出占比与成本/风险 |
| 调薪方案改一版、重算一小时 | `simulate_increase` 四策略一键对比，总成本严格守恒于预算 |
| 固浮比拍不准，被业务方怼 | `simulate_pay_mix` 按岗位序列给目标固浮比与收入曲线，并附前提警告 |
| 市场对标不会做、分位值不会选 | `market_benchmark` 按岗位序列分位策略（核心岗 P75 / 通用岗 P50）算达标成本 |
| 岗位"凭什么值这个职级"说不清 | `calc_job_score` 海氏/美世打分表 → 可复查的岗位价值总分与职级建议 |
| 真实薪资上云不安全 | 默认本地模型（Ollama/vLLM），**不自动回退云端**；`desensitize_data` 一键脱敏 |

**目标用户**：在职 HR / 薪酬专员 / 薪酬绩效（C&B）相关岗位，用于内部诊断、
调薪方案推演，以及向候选人或管理层讲解口径与依据。

---

## 二、架构

```
                ┌─────────────────────────────────────────────┐
   上传/CLI ───▶ │  dsh 运行时（Cordis 插件树）                 │
   (工资表)      │  · systemPrompt 组装                         │
                │  · ctx.tools 注册表（11 个工具）              │
                └───────────────┬─────────────────────────────┘
                                │  Function Calling（模型只返回"调哪个函数+参数"）
                                ▼
                ┌─────────────────────────────────────────────┐
   TS 插件层     │  dsh-comp-tool（薄适配，零业务计算）           │
   (software-    │  index.ts → service.ts(11×defineTool)        │
    architect-2) │  → bridge.ts（持久 stdio worker）            │
                └───────────────┬─────────────────────────────┘
                                │  JSON-RPC 2.0 · Content-Length 分帧
                                ▼
                ┌─────────────────────────────────────────────┐
   Python 计算   │  server.py（分发）→ registry.py（单一真理源） │
   核心          │  loader / diagnose / band / market /         │
   (engineers)   │  increase / paymix / jobeval / charts /     │
                │  report / sandbox(PTC) / session             │
                └───────────────┬─────────────────────────────┘
                                │  产物落盘（不进模型上下文）
                                ▼
                assets/*.html（图表）  report/*.md|html（报告）  .state/（会话缓存）
```

**关键设计取舍**
- **模型不计算**：所有薪酬数值由 Python 确定性执行，模型只做语义理解、工具调度、
  自然语言解读 → 结果可复现、可审计。
- **单一真理源**：`src/tools/schemas.py` 集中所有业务常量（带宽幅度、红绿圈阈值、
  绩效权重、固浮比基准、岗位评估换算）；`registry.py` 让 PTC 沙箱与主路径调用
  **同一份 handler**，口径零漂移。
- **默认本地、默认安全**：`config.yaml` 默认 `ollama-local` 且 `fallback.enabled:false`
  （本地模型挂了也不自动上云）。

---

## 三、11 个工具（一行一个）

| 工具 | 用途 |
|---|---|
| `load_salary_data` | 读 csv/xlsx，清洗脏数据，给字段映射建议与预览（只回前 5 行） |
| `confirm_mapping` | 固化「原始列→标准字段」映射，生成标准 DataFrame（可自动补带宽/年现） |
| `analyze_current_state` | 各职级分布、CR、渗透率、红绿圈占比与成本/风险诊断 |
| `generate_band` | 设计/优化薪酬带宽表，算相邻职级重叠度 |
| `market_benchmark` | 按岗位序列分位策略对标市场 P25/P50/P75，算达标成本 |
| `simulate_increase` | 按预算% + 4 策略模拟调薪，对比红绿圈变化与成本（预算守恒） |
| `simulate_pay_mix` | 按岗位序列算目标固浮比下的收入-达成率曲线 |
| `calc_job_score` | 海氏/美世打分 → 岗位评估总分 → 建议职级 |
| `generate_report` | 整合 7 节结构化报告（md + html），强制注入数据安全声明 |
| `run_comp_code` | PTC 沙箱：受限 Python 里批量编排 9 个工具或自定义 pandas 分析 |
| `desensitize_data` | 对当前会话数据生成脱敏副本（不改原数据），可安全外发 |

> 完整 JSON Schema 见 `docs/ARCHITECTURE.md` §4；需求与验收见 `docs/PRD.md`。

---

## 四、方法论速览

**1. 带宽设计（Band Design）**
带宽幅度（Range Spread，相对下限口径）= (上限 − 下限) / 下限。
- 下限 = 中位值 ÷ (1 + 幅度/2)
- 上限 = 下限 × (1 + 幅度)
- 相邻职级重叠度 = (低职级上限 − 高职级下限) / (低职级上限 − 低职级下限)
行业惯例：基层/操作 20-30%、专业/技术 30-40%、中层/专家 40-50%、高层 50%+。

**2. 比较比率（Compa-Ratio, CR）与带宽渗透率（Range Penetration）**
- CR = 个人薪资 ÷ 该职级带宽中位值。1.0 = 中位；>1.2 红圈；<0.8 绿圈。
- 渗透率 = (薪资 − 下限) ÷ (上限 − 下限)，反映个人在带宽内的位置（约 20%-80% 为合理）。
- ⚠️ 实际生效门槛依赖带宽幅度：当幅度 s ≥ 0.50 时，CR 阈值会被带宽边界
  `1/(1+s/2)` 与 `(1+s)/(1+s/2)` 收紧（详见 `docs/ARCHITECTURE.md` §6.2）。

**3. 红圈 / 绿圈（Red / Green Circle）**
- 红圈（CR>1.20 或 薪资>上限）：薪酬偏高、成本溢出，多为老员工/关键岗积累。
- 绿圈（CR<0.80 或 薪资<下限）：薪酬偏低、流失风险，需优先补差。

**4. 调薪预算守恒（Budget Conservation）**
预算 = 调薪前年度薪资基数 × 预算比例（基数 = Σ 月薪×12，覆盖率 100%）。
四种策略（A 平均 / B 优先补绿圈 / C 按绩效加权 / D 优先保留红圈）的
`total_cost` 必须 ≈ 预算（偏差 ≤ 1e-6），否则视为计算错误。

**5. 固浮比（Pay Mix）**
目标固浮比（固定:浮动）：销售 40:60、技术 70:30、管理 60:40、操作 80:20、职能 75:25。
实际总收入 = 固定部分 + 浮动目标 × 业绩达成率。
> **前提警告（必须原文输出）**：高浮动比例的前提条件是：业绩可量化、可归因到个人、
> 结算周期短；长周期协作型业务强行高浮动会破坏协作。

**6. 岗位价值评估（海氏 / 美世）**
- 海氏三要素：知识技能 / 解决问题 / 应负责任（权重 0.4 / 0.25 / 0.35）。
- 美世 IPE 四因素：影响 / 沟通 / 创新 / 知识。
- 子维度 1-10 分 → 要素内平均 → 按权重加权得 W → **总分 = W × 160**
  （量纲对齐 160-1600 的职级分段表）。这是修复过的 P0 缺陷：若不乘 160，
  W∈(0,10) 会恒判 P1，模块 6 全废。

---

## 五、如何运行

> **前置**：Python ≥ 3.11，然后 `pip install -r requirements.txt`。
> 第 1、2 步（确定性流水线）**不需要任何大模型**；第 3 步（对话形态）需要本地
> Ollama（`ollama pull deepseek-r1:14b`）或本地 vLLM。**全流程无需任何云端密钥。**
>
> **Web UI（可选）**：`pip install -e ".[web]"`（或 `pip install streamlit`）后，
> 即可用浏览器跑诊断，见下方第 5 步。Web UI 与 CLI 共用同一套底层计算代码。
>
> Windows 下命令行建议带 `PYTHONIOENCODING=utf-8` 前缀防中文乱码；
> macOS / Linux 直接用 `python3` 即可。

### 1) 一键演示（推荐首选，零交互 · 外发零风险）
```bash
python run_agent.py --demo
# 自带合成模拟样例（synthetic 分级，与任何真实自然人无关）零交互跑完整链：
# 预算 5% · 策略 A · 海氏评估 · 自动映射 → report/ 落盘 md+html 报告
# 样例缺失时自动按固定种子重新生成，产出可复现
```

### 2) 生成模拟数据（需要指定造数参数时）
```bash
python mock_data.py
# 产出 data/sample_salary.csv（标准表头，150 行）、data/messy_salary.csv（脏数据）
```

### 3) 本地离线入口（不经 dsh，适合演示/调试）
```bash
python run_agent.py --file data/sample_salary.csv
# 按对话式流程依次调用 11 个工具，最终在 report/ 落盘报告
```

### 4) 接入 dsh 插件（自然语言对话形态，可选）
```bash
# 需要先安装 dsh 运行时；插件层为可选增强，不影响第 1、2 步
dsh plugin add ./src/plugins/comp-tool
dsh --profile comp "帮我看看 data/sample_salary.csv，诊断一下红绿圈"
```
模型会按 `src/skills/comp-analyst/SKILL.md` 的剧本自动走完
`load → confirm → diagnose → band → benchmark → increase → paymix → report`。

> 默认 provider 在 `config.yaml` 中为 `ollama-local`，且 `models.fallback.enabled:false`
> —— 本地模型不可用时**不会**偷偷切到云端，宁可报错提示你启动 Ollama。

### 5) Web UI（零命令行，适合分享给同事）

不想让同事记命令行参数？`app.py` 把底层诊断链包成网页界面，全部计算在本机完成，
**薪酬数据不出内网**：

```bash
pip install -e ".[web]"      # 首次：装 streamlit
streamlit run app.py         # 启动，终端会打印本地访问地址（默认 http://localhost:8501）
```

打开网页后有两种模式：

| 模式 | 说明 | 风险 |
|---|---|---|
| **一键演示（零风险）** | 内置合成模拟数据，点「运行演示」零交互跑完整链；报告带"模拟数据"中性横幅，**可安全截图外发 / 投屏** | 无 |
| **诊断我的工资表** | 上传 `.csv` / `.xlsx` → 自动映射 → 生成报告 → 网页预览 + 下载 md/html | 真实敏感数据，报告仅限本地查看，**切勿外发** |

> Web UI 不重新实现任何计算逻辑，只复用 `src/main.main(argv)` 编程式调用底层诊断链；
> 因此与 CLI 口径完全一致、结论可审计。真实数据报告默认带红色水印与 `data_guard.md`
> 安全提示，网页也明确标注"切勿外发"。

---

## 六、演示走查（基于 `data/sample_salary.csv`）

数据规模：**150 人，9 个职级（P1-P6 / M1-M3），年度薪资基数 39,170,400 元**，
整体略低于市场 P50 约 3%。

| 步骤 | 工具 | 关键结果（工具 JSON） |
|---|---|---|
| 现状诊断 | `analyze_current_state` | 红圈 **31 人（20.67%）**、绿圈 **26 人（17.33%）**、合理 93 人 |
| 带宽（P3） | `generate_band` | 中位值 **14,150** → 下限 **12,042.5532** / 上限 **16,257.4468**（恒等式成立） |
| 对账样例（E0001） | — | P3，月薪 19,900 → CR = 19,900 ÷ 14,150 = **1.4064** → 红圈 |
| 调薪（5% 预算） | `simulate_increase` | 预算 = 39,170,400 × 5% = **1,958,520 元**；四策略总成本均 ≈ 该值 |
| 岗位评估（M3 样例） | `calc_job_score` | 海氏 W=8.875 → 总分 8.875×160 = **1,420** → 建议 **M3** |

> 这些数值可在 `docs/PRD.md` 的「金标准样例」与 QA 机检命令中找到逐条对账脚本，
> 你可以拿去当场复算，验证口径是否与你预期的一致。

---

## 七、数据安全声明（请务必阅读）

薪资数据是公司最敏感的数据之一。本项目把安全作为**第一优先级**：

1. **默认本地部署**：`config.yaml` 默认 `ollama-local`，薪资不出内网。
2. **不自动回退上云**：`models.fallback.enabled:false`。本地模型挂了只报错，
   绝不偷偷把数据发往云端 API。
3. **脱敏工具**：对话里说"把这份表脱敏"，`desensitize_data` 立即生成副本
   （姓名泛化、薪资比例缩放、ID 重编号），原会话数据不变。
4. **会话缓存用 parquet + JSON 只存 meta**：明细以 parquet 落盘于 `.state/`
   （已 gitignore），JSON 元数据不存行级薪资；云端模式下强制 `persist_dataframe:false`。
5. **绝不打印完整真实数据**：工具只返回前 5 行预览与聚合统计；日志受
   `max_line_chars` / `allow_row_level_salary:false` 约束。
6. **报告强制声明**：每份报告注入"数据来自本地模拟数据，未上传任何云端服务"。

> ⚠️ **脱敏 ≠ 匿名化**。比例缩放保留了全部统计特征与个体相对排序，仍属敏感数据。
> 外发前请确认接收方资格并签署保密协议。处理真实薪资，**务必使用本地模型部署，
> 切勿上传至云端 API**。

---

## 八、FAQ

**Q1：为什么不让 AI 直接算，非要绕一层工具？**
Function Calling 机制下，模型只负责"调哪个函数、传什么参数"，所有数值计算由
Python 确定性执行。好处：① 结果可复现、可被脚本对账；② 公式与方法论集中在
`schemas.py`，改口径不用动模型；③ 避免大模型心算/幻觉导致的数字错误。

**Q2：我的表表头很乱（"基本工资""Base Pay"混用），能识别吗？**
能。`load_salary_data` 内置 `auto_suggest_mapping()`，对 18 个标准字段穷举别名词典 +
语义归一化匹配，乱列名上实测 18/18 命中、置信度全 100。你只需确认/微调映射。

**Q3：没有带宽三列怎么办？**
`confirm_mapping(fill_missing_band=true)` 会用各职级**现状中位数 + 分层默认幅度**
自动生成临时带宽，诊断照常进行（工具会标注 `band_generated=true`）。

**Q4：红绿圈为什么有时和我手工算的不完全一样？**
当带宽幅度 ≥ 50% 时，CR 阈值会被带宽边界收紧（见 §四.2）。本项目默认幅度均 <50%，
直接用 0.80/1.20。以工具返回的 `effective_thresholds` 为准。

**Q5：PTC（run_comp_code）和 dsh 官方 Code Mode 什么关系？**
本项目自建的 `run_comp_code` 与官方 `run_code` 传输契约同构
（`{code, description}` → `{logs, result}`），差异仅在执行语言为 Python、执行环境为
一次性子进程。`dsh-code-runtime-python` 后端交付后可无缝替换。默认**不开启**官方
Code Mode，避免模型混淆两种语言。

---

## 九、目录结构


```
salary-diagnosis-cli/
├── config.yaml              # 全局配置（模型/路径/安全/业务默认值）★单一配置入口
├── config.example.yaml      # 配置模板（python_exe 用通用值，供新机器参考）
├── requirements.txt         # Python 依赖（pip install -r requirements.txt）
├── LICENSE                  # MIT 许可证
├── .env.example             # 密钥与环境变量样例（密钥绝不进仓库）
├── mock_data.py             # 造数脚本（150 行高仿真脱敏数据 + 脏数据变体）
├── run_agent.py             # 本地 CLI 入口（零外部依赖也能跑确定性流水线）
├── app.py                   # Streamlit Web UI（可选；pip install -e ".[web]" 后 streamlit run app.py）
├── docs/
│   ├── PRD.md               # 产品需求文档（FR/AC/NFR/金标准样例）
│   └── ARCHITECTURE.md      # 架构与 11 工具接口规格（工程师实现合同）
├── src/
│   ├── tools/               # Python 计算核心（schemas/loader/diagnose/band/...）
│   ├── plugins/comp-tool/   # dsh TS 插件（薄适配，software-architect-2 负责）
│   └── skills/comp-analyst/ # 本 Agent 技能（SKILL.md，本文档作者负责）
├── data/                    # 输入数据（模拟数据，git 跟踪样例；private/ 不跟踪）
├── report/                  # 报告产物
├── assets/                  # 图表 HTML/PNG 与明细 CSV
└── .state/                  # 会话缓存（gitignore，绝不进仓库）
```

---

## 十、相关文档

- `docs/PRD.md` —— 功能清单、可机检验收标准、非功能需求、金标准对账样例。
- `docs/ARCHITECTURE.md` —— 分层架构、11 工具完整 JSON Schema、PTC 沙箱设计、并发安全。
- `src/tools/schemas.py` —— 所有业务常量的单一真理源（含方法论中文注释）。
