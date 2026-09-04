# 贡献指南（CONTRIBUTING）

感谢关注本项目！这是一个 HR 薪酬领域的作品集项目，欢迎 Issue 与 PR。

## 环境搭建

```bash
git clone https://github.com/helibeiqi/salary-diagnosis-cli.git
cd salary-diagnosis-cli
pip install -r requirements.txt        # 或 pip install -e ".[dev]"
python -m pytest tests/ -q             # 应全绿
python run_agent.py --list             # 11 个工具全部 ✓
```

Python 版本要求 ≥ 3.11（CI 在 3.11 上运行，3.12+ 亦可）。

## 提交前必做

1. **本地跑全量测试**：`python -m pytest tests/ -q`（与 CI 同版本解释器，避免"我机器上能跑"）。
2. **金标准对账**：改动计算口径时必须跑 `python tests/verify_golden.py`，数值漂移 = 拒绝。
3. **E2E 回归**：`python tests/qa_e2e.py`。

## 本仓库的几条硬纪律（违反会被安全闸门 / 评审直接打回）

- **真实薪资数据绝不入库**：`report/`、`assets/*.html`、`src/data/`、`.state/`、
  `data/band_*.csv` 均已被 `.gitignore` 拦截——它们是运行期产物，内嵌行级薪资。
  演示一律使用 `data/` 下的模拟样例。
- **口径改动必须过 `src/tools/schemas.py`**：所有业务常量（红绿圈阈值、带宽幅度、
  绩效权重、固浮比基准）单一真理源在 schemas.py，不要在别处散落新常量。
- **报告数字不二写**：generate_report 的数字必须全部来自上游工具 meta，
  新增章节须通过 `tests/test_meta_contract.py` 契约回归。
- **默认本地、绝不自动上云**：涉及模型 provider 的改动不得引入"失败自动回退云端"
  的行为（`config.yaml` 的 `fallback.enabled` 必须保持 false）。

## 提交规范

- commit message 用 conventional 风格：`feat:` / `fix:` / `docs:` / `test:` / `refactor:`，
  作用域写模块名（如 `fix(increase): ...`）。
- CI（`.github/workflows/ci.yml`）会跑：pytest → golden 对账 → e2e 回归 → 安全闸门
  （敏感路径入库 / 核心引入网络 import 直接 fail）。PR 请确保四步全绿。
