# 薪酬诊断 CLI（salary-diagnosis-cli）路线图 ROADMAP

> 版本：v0.2.1（2026-09-04 更新）　｜　维护者：何李贝奇　｜　口径：北京 / 中国 A 股语境（红=偏高/成本风险，绿=偏低/流失风险）
>
> 本路线图汇总当前已知问题、已落地改进与后续演进方向。**优先级**：P0=阻断性（会导致崩溃/错误结论）；P1=安全/合规红线；P2=体验/可扩展性。
> 评审项编号 R1–R6 来自 2026-09-02 的 CLI 整体评审。

---

## 一、本版本已落地（v0.2）

| 项 | 状态 | 说明 |
|----|------|------|
| **R1 职位→职级推断** | ✅ 已落地 | 新增 `src/tools/level_infer.py`；`confirm_mapping` 在「缺 level 但有 job_title」时确定性启发式补 level（O/P/S/M 四段 11 级），覆盖率透明进 warnings。解决「缺 level 整行被删 → 能诊断人数骤降（实测开箱 4/9）」。 |
| **R2 敏感数据护栏** | ✅ 已落地 | ① `assets/*.html` 6 张图表从 git 跟踪移除（历史运行内嵌真实薪资，绝不入库），`src/data/` 行级明细红线延续；② `report.py` 真实数据报告加**顶部红字水印 + 页脚警示 + stderr 警告**，不再谎称「脱敏模拟数据」；已脱敏（`_desensitized.csv` 或 `data_classification=sanitized`）则标注可安全外发。 |
| **R3 `desensitize`↔报告标记打通** | ✅ 已落地 | `desensitize()` 显式写 `data_classification=sanitized` 入 session meta（重加载仍带标记）；`load_salary_data` 文件名兜底 + 显式 `data_classification` 参数；本次 P1-4 扩展 `synthetic`/`simulated`：模拟数据渲染中性横幅、不触发真实数据护栏。护栏不再依赖单一命名约定。 |
| **R4 外发二次确认门** | ✅ 已落地 | `classification=real` 且未确认时生成 `报告名.data_guard.md` 安全提示；CLI `--i-know-this-is-real-data` 可抑制。 |
| **P1-4 模拟数据分级** | ✅ 已落地（本批） | `sample_salary.csv`/`messy_salary.csv` 等合成演示数据加载后自动判为 `synthetic`，报告去红字水印、可安全外发演示；`load_salary_data(..., data_classification=...)` 可显式覆盖。 |
| **P0 perf_grade 崩溃** | ✅ 已落地（v0.1.1） | `increase._resolve_perf_weights` 在缺 `perf_grade` 列时优雅回退，不再 `KeyError`。 |
| **开源化基建（v0.2.1，2026-09-04）** | ✅ 已落地 | ① 修复 CI 连续 6 次失败的根因：`test_charts_report.py` / `test_report_viz.py` 两处 f-string 内嵌 `\"` 转义（py<3.12 语法错误，PEP 701），改为先取计数再进 f-string；`test_selfcheck_ta.py` 模块级 `os.remove` 加保护（收集阶段清理失败不再中断）。② 新增 `LICENSE`（MIT）、`requirements.txt`、`pyproject.toml`（含 `pip install -e .` 与 `salary-diagnosis` 命令入口）、`config.example.yaml`、`CONTRIBUTING.md`。③ README 修正运行指引（项目名与克隆目录一致、解释器路径通用化、dsh 插件层声明为可选增强）。 |

---

## 二、优先级总览

### P0 — 阻断性问题（必须优先修复）
- [x] **perf_grade 列缺失崩溃**（已修，commit `ff2157e`）。
- [ ] **`src/data/` 行级明细写入红线复核**：`increase._export_detail` 导出 `monthly_salary / new_monthly_salary` 行级明细，已 `.gitignore` 拦截；需固化「导出即落 `.bak` + 校验」硬闸门，避免任何路径误 `git add`。
- [ ] **报告数字全部来自 meta 单一真理源**：继续维持「模型不改写数字」纪律，新增章节须通过 `tests/test_meta_contract.py` 契约回归（键名漂移历史已踩坑 10+ 处）。

### P1 — 安全 / 合规红线
- [x] **R2 图表与报告护栏**（已修，见上）。
- [x] **R3 `desensitize` ↔ 报告标记打通（已落地）**：`desensitize()` 已显式把 `data_classification=sanitized` 写入 session meta（`loader.py:1498`），即便重加载也带标记；`load_salary_data` 同时按文件名兜底（`_desensitized.csv` → sanitized）。本次（P1-4）进一步把该分级体系扩展到 **`synthetic`/`simulated`**：`mock_data.py` 产出的 `sample_salary.csv`/`messy_salary.csv` 加载后自动识别为模拟数据，报告渲染中性「模拟数据，可安全演示」横幅、**不**触发真实数据红字水印与 `data_guard.md` 护栏；`load_salary_data` 亦接受显式 `data_classification` 参数。护栏逻辑因此**不再依赖单一命名约定**。
- [x] **R4 外发二次确认门（已落地）**：`generate_report` 在 `classification=real` 且未确认时，除 stderr 警告外额外生成 `报告名.data_guard.md` 安全提示文件；CLI 层 `--i-know-this-is-real-data`（`main.py:495`）可抑制该提示，避免误把真实报告提交到公开仓库。
- [ ] **R5 推断覆盖率阈值告警**：R1 推断覆盖率低于阈值（建议 70%）时，除 warning 外，在 `confirm_mapping` 返回里标 `needs_level_review=True`，驱动调用方（人或模型）在落库前复核，而非仅被动提示。

### P2 — 体验 / 可扩展性
- [ ] **R6 单测覆盖（CI 部分已完成）**：~~接入 GitHub Actions 跑 `pytest`~~ ✅ CI 已上线（`ci.yml`：pytest → golden → e2e → 安全闸门，2026-09-03 起）；待补 `tests/test_level_infer.py`（启发式 + 覆盖率）、`tests/test_report_guard.py`（真实/脱敏水印分支）、脱敏往返测试（脱敏→重加载→报告标注 sanitized）。CI 已含 `src/data/` / `assets/` 误提交硬失败（`ci_security_gate.py`）。
- [ ] **市场分位数据接入**：当前市场对标依赖用户上传 P25/P50/P75；规划接一个可离线/可配置的市场分位源（本地 JSON 或可选 API），降低「市场模块直接跳过」的概率。
- [ ] **CLI 易用性**：`run_agent.py` 入口补 `--demo` 一键跑通（自带脱敏样例，外发零风险）、`--check-data-guard` 预检外发风险。
- [ ] **带宽/CR 口径可配置**：红绿圈阈值、带宽幅度、重叠健康区间目前散在 `schemas.py` 与 `charts.py`，规划统一到一处配置，便于不同行业复用。

---

## 三、R1–R6 明细

### R1 职位→职级推断（✅ 已落地）
- **问题**：真实表常「有岗位无职级」，level 缺失 → 整行被 `clean_dataframe` 剔除。
- **方案**：`level_infer.infer_levels(df, title_col)` 用关键词 + 罗马数字后缀确定性推断，返回 `(df, 覆盖率报告)`，由 `confirm_mapping` 在清洗前补全。
- **诚实边界**：启发式覆盖率取决于职位词表；未命中样本逐条进 warnings，由用户决定「够不够用」。词表在 `level_infer._KEYWORD_LEVEL` 集中维护，新增行业岗位直接加表。

### R2 敏感数据护栏（✅ 已落地）
- **问题**：`assets/` 6 张图表 html 含历史运行真实薪资且被 git 跟踪；报告页脚硬编码「数据均为脱敏模拟数据」——对真实数据属**虚假声明**。
- **方案**：① `git rm --cached` 6 张图 + `.gitignore` 加 `assets/*.html`（保留 `.gitkeep`）；② 报告按 `data_classification` 动态生成水印/页脚，真实数据强制红字警示 + stderr 警告。
- **残留风险**：本地已生成的图表 html 仍含真实薪资（仅不在仓库）；如需彻底清除，`rm assets/*.html` 即可，下次运行自动重生。

### R3 desensitize ↔ 报告标记打通（规划中，P1）
- 让 `desensitize()` 接受可选 `session_id`，把 `data_classification=sanitized` 写入该会话 meta；重加载脱敏文件时优先读此标记，弱化对文件名的依赖。

### R4 外发二次确认门（规划中，P1）
- `generate_report` 在真实数据 + HTML 输出时，强制交互确认或落 `data_guard.md`，CLI 提供 `--i-know-this-is-real-data` 显式放行。

### R5 推断覆盖率阈值告警（规划中，P1）
- 覆盖率 < 70% → `confirm_mapping` 返回 `needs_level_review=True`，驱动复核而非仅提示。

### R6 单测与 CI 覆盖（规划中，P2）
- 新增 level_infer / report_guard / 脱敏往返单测；GitHub Actions 跑 pytest，对 `src/data/` 与 `assets/*.html` 误提交 fail。

---

## 四、不做的事（边界）
- 不内置任何外部薪酬数据库明文缓存；市场分位默认走用户上传，敏感数据永不明文落库。
- 不替代 HR 专员判断：所有结论标注「测算稿 / 仅供参考」，调薪与组织决策权在用户。
- 不自动 `git push`：推送由人工在 GitHub Desktop 点 Push（沙箱网络不可靠，历史已踩坑）。
