# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
