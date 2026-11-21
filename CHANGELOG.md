# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.3.0] — 2026-11-21

### 新增（岗位价值评估专业化升级）
- **海氏升级为 Guide Chart 点数法（非线性）**：`schemas.compute_hay_total`——
  知识技能三子维查点数表相加、解决问题查"思维环境 × 思维难度"百分比表
  （封顶 87%）、应负责任 = 行动自由度点数 × 影响乘数 × 规模乘数，
  **总分 = KH + KH×PS% + ACC**。查表刻度按公开文献结构原理自建校准
  （商业版 Guide Chart 为 Korn Ferry 专有 IP，不使用不复制），
  校准约束：全表最大 ≈1494 ≤ 1600、最小 ≈29 落 P1、单一子维度升档单调不减
  （`tests/test_jobeval_guide_chart.py` 锁定）。
- **BARS 行为锚定描述（A③）**：两模型共 18 个子维度 × 4 档（1-3/4-6/7-8/9-10）
  行为锚定语，新增工具 **`get_job_eval_guide`**（无会话依赖）输出完整打分指南；
  工具总数 11 → 12（registry 自检断言同步）。
- **美世诚实标注（A①）**：label/method 明示"**线性近似打分卡**"，
  method_note 说明正版 IPE 阶梯表为 Mercer 专有 IP、线性链路只能反映相对排序。

### 修复（岗位评估历史静默缺陷，A②）
- **子维度缺失告警 + 权重重归一化**：此前要素整列缺失时按 0 分静默参与
  （系统性低估且无提示）；现美世模型缺失要素 → 权重按其余要素占比重归一化 +
  告警，要素内子维度部分缺失 → 用已有列平均 + 告警；海氏模型 KH 子维度缺失
  按 0 点计、PS/ACC 缺失按中位档/乘数 1.0 兜底，逐行告警聚合为"【N/M 行】"条目。
- `calc_job_score` 走 job_score 列降级路径时（海氏）新增提示：该路径只有线性
  量纲含义，Guide Chart 结构未生效。

### 文档
- README「六、岗位价值评估」章节重写；金标准样例标注 job_score 降级路径口径。

### 验证
- pytest **43 passed**（含新增 `tests/test_jobeval_guide_chart.py` 16 项，
  `-W error::DeprecationWarning` 严格模式通过）；金标准对账 **16 PASS**；
  e2e 回归 **28 PASS**；`--demo` 全链路 9/9 步正常出报告。

## [0.2.2] — 2026-09-04

### 新增
- **`--demo` 一键演示**（ROADMAP P2「CLI 易用性」）：`python run_agent.py --demo`
  零交互跑通完整诊断链——
  - 数据源固定为 `data/sample_salary.csv` 合成模拟数据（synthetic 分级，
    报告渲染中性横幅、不触发真实数据护栏，外发 / 投屏零风险）；
  - 固定参数：预算 5% · 策略 A（平均分配）· 海氏岗位评估 · 自动映射；
  - 样例缺失时自动调用 `mock_data.py` 按固定种子重新生成
    （实测产出与仓库基线逐字节一致，可复现）。

### 验证
- 本地实测：`--demo` 9/9 步成功，报告 + 5 张图表落盘；pytest 27 passed。

## [0.2.1] — 2026-09-04

### 修复
- **CI 连续失败（P0）**：修复 `test_charts_report.py` / `test_report_viz.py` 两处
  f-string 表达式内嵌 `\"` 转义（Python < 3.12 语法错误，PEP 701），此前导致
  pytest 收集失败、CI 连续 8 次红灯且 golden / e2e / 安全闸门全部被跳过。
- **CI 缺 pyarrow（P0，第二层根因）**：`session.py` / `telemetry.py` 的 parquet
  会话持久化依赖 pyarrow，作者本机 anaconda 自带故从未暴露；CI 环境补装并纳入
  `requirements.txt` / `pyproject.toml` 声明。
- **pandas 暂钉 `<3.0`**：CI 曾拉到 pandas 3.0.5，但本项目仅在 2.3.3 下验证过；
  pandas 3 的 dtype / Copy-on-Write 破坏性变更未经测试，实测通过后放宽。
- **测试收集纯净性**：`test_selfcheck_ta.py` 模块级 `os.remove` 加 `OSError` 保护，
  收集阶段的临时文件清理失败不再中断整个测试会话。

### 新增（开源化基建）
- `LICENSE`：MIT。
- `requirements.txt` + `pyproject.toml`：依赖声明与打包元数据，
  支持 `pip install -e .`，并提供 `salary-diagnosis` 命令入口。
- `config.example.yaml`：配置模板（`python_exe` 用通用值；本机 `config.yaml` 不受影响）。
- `CONTRIBUTING.md`：环境搭建、提交前必做清单、仓库硬纪律。
- README：修正 `cd` 目录与仓库名不一致、解释器路径通用化、
  明示 `run_agent.py` 为零依赖入口、dsh 插件层为可选增强；新增状态徽章。

### 验证
- 本地（Python 3.11.9）：pytest 27 passed；金标准对账 PASS=16 / FAIL=0；
  E2E 回归 PASS=28 / FAIL=0。CI（Ubuntu, Python 3.11）：四道关卡全绿。

## [0.2.0] — 2026-09-02

### 新增
- **R1 职位→职级推断**：`src/tools/level_infer.py`，`confirm_mapping` 在缺
  `level` 但有 `job_title` 时确定性启发式补级（O/P/S/M 四段 11 级），
  解决「缺 level 整行被删 → 能诊断人数骤降」。
- **R2 敏感数据护栏**：真实数据报告顶部红字水印 + 页脚警示 + stderr 警告；
  `assets/*.html` 图表移出版本库。
- **R3 脱敏↔报告标记打通**：`data_classification` 分级体系（real / sanitized /
  synthetic / simulated），护栏不再依赖单一命名约定。
- **R4 外发二次确认门**：`classification=real` 且未确认时生成
  `报告名.data_guard.md` 安全提示；CLI `--i-know-this-is-real-data` 可抑制。
- 语义化 `summary_md` 单一真理源（`src/tools/_summary.py`）+ 映射自动固化 + 验证闭环。

## [0.1.1] — 2026-09-02

### 修复
- `simulate_increase` 在缺 `perf_grade` 列时优雅回退，不再 `KeyError` 崩溃。

## [0.1.0] — 2026-09-02

### 新增
- 独立版薪酬诊断 CLI 首个公开版本：11 个确定性工具
  （load / mapping / band / diagnose / benchmark / increase / paymix /
  jobeval / report / sandbox / desensitize）。
