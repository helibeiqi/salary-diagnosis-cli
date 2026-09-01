# -*- coding: utf-8 -*-
"""
mock_data.py — 一键生成高仿真「脱敏」薪酬模拟数据
================================================================================
设计目的
--------------------------------------------------------------------------------
1. 薪酬数据极度敏感，演示/测试/默认加载**一律使用模拟数据**，真实数据永不进仓库。
2. 生成的 150 条数据在统计上刻意贴近真实企业的典型形态：
   - 职级越高，薪资中位数越高，且中位值级差（Midpoint Differential）约 15%-40%
   - 同一职级内薪资呈右偏分布（少数资深员工拉高均值，均值 > 中位数）
   - 存在约 11% 红圈（CR > 1.2，多为司龄长的技术骨干或历史遗留高薪）
   - 存在约 13% 绿圈（CR < 0.8，多为新入职/快速晋升员工）
   - 公司整体薪资略低于市场 P50（约 -3%），为模块 3 对标留出真实课题
   - 部分字段按真实表格习惯留空（年度总现金 / 岗位评估得分 / 固浮比 等）
3. 同时生成一份「乱列名 + 脏数据」版本，用于演示 AI 字段识别映射与数据清洗能力。

用法
--------------------------------------------------------------------------------
    python mock_data.py                      # 默认 150 条，输出标准版 + 乱列名版
    python mock_data.py -n 300 --seed 42     # 自定义条数与随机种子
    python mock_data.py --with-band          # 同时填充带宽下限/中位值/上限
    python mock_data.py --no-market          # 不生成市场 P25/P50/P75
    python mock_data.py --no-excel           # 不输出 Excel（无 openpyxl 时自动跳过）

输出
--------------------------------------------------------------------------------
    data/sample_salary.csv      标准表头（可直接跑全流程）
    data/sample_salary.xlsx     同上，Excel 版（含「字段说明」sheet）
    data/messy_salary.csv       乱列名 + 少量脏数据（演示映射与清洗）

⚠️ 安全声明：本脚本产出的全部姓名、薪资均为随机合成，与任何真实自然人无关。
"""

from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

# 让本脚本可被独立运行：把 src/ 加进搜索路径，复用标准字段定义（单一真理源）
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "src"))

from tools.schemas import (  # noqa: E402
    CANONICAL_FIELDS,
    CANONICAL_ORDER,
    get_level_tier,
    JOB_FAMILY_PAY_MIX,
    infer_job_family,
)

# =============================================================================
# 一、基础素材（全部为合成数据）
# =============================================================================

# 脱敏姓氏：只保留姓，名字以 ** 遮蔽 —— 这是 HR 对外提供数据时的推荐做法
SURNAMES = ["张", "王", "李", "赵", "陈", "刘", "杨", "黄", "周", "吴", "徐", "孙",
            "马", "朱", "胡", "郭", "何", "高", "林", "罗", "郑", "梁", "谢", "宋",
            "唐", "许", "韩", "冯", "邓", "曹"]

# 职级 -> (人数, 带宽中位值月薪(元))
# 中位值设定参考：一线城市制造业+科技混合型企业的典型水平
LEVEL_CONFIG = [
    ("P1", 20, 8000),
    ("P2", 28, 10500),
    ("P3", 30, 14500),
    ("P4", 24, 19500),
    ("P5", 18, 26500),
    ("P6", 12, 35500),
    ("M1", 9, 46000),
    ("M2", 6, 62000),
    ("M3", 3, 88000),
]

# 岗位序列 -> 部门候选
FAMILY_DEPTS = {
    "销售": ["销售一部", "销售二部", "大客户部", "渠道发展部"],
    "技术": ["研发中心", "产品技术部", "工艺工程部", "设备技术部"],
    "管理": ["总经办", "运营管理部", "事业部管理"],
    "操作": ["生产制造部", "厂务部", "质量管理部", "仓储物流部"],
    "职能": ["人力资源部", "财务部", "行政部", "采购部", "法务合规部"],
}

# 岗位序列 -> 职级 -> 岗位名称候选
FAMILY_TITLES = {
    "销售": {"low": ["销售代表", "客户经理", "渠道专员"],
             "mid": ["高级客户经理", "区域销售经理", "解决方案顾问"],
             "high": ["大区销售总监", "行业销售负责人"]},
    "技术": {"low": ["助理工程师", "初级开发工程师", "工艺技术员"],
             "mid": ["工程师", "高级工程师", "技术专家"],
             "high": ["资深技术专家", "首席工程师", "研发架构师"]},
    "管理": {"low": ["项目主管", "团队组长"],
             "mid": ["部门经理", "事业部负责人"],
             "high": ["总监", "事业部总经理"]},
    "操作": {"low": ["操作工", "生产线技工", "检验员"],
             "mid": ["高级技工", "班组长", "设备技师"],
             "high": ["生产主管", "车间主任"]},
    "职能": {"low": ["人事专员", "会计专员", "行政专员"],
             "mid": ["人力资源主管", "财务主管", "薪酬绩效经理"],
             "high": ["人力资源总监", "财务总监"]},
}

# 绩效等级分布（真实企业常见：A 15% / B 45% / C 30% / D 10%）
PERF_POOL = ["A"] * 15 + ["B"] * 45 + ["C"] * 30 + ["D"] * 10


def _family_of_level(level: str, rng: np.random.Generator) -> str:
    """
    按职级分配岗位序列：
    - M 序列（M1-M3）固定为「管理」
    - P1-P2 以操作/职能为主（基层）
    - P3-P6 以技术/销售/职能为主（专业骨干）
    """
    lv = level.upper()
    if lv.startswith("M"):
        return "管理"
    num = int(lv[1:]) if lv[1:].isdigit() else 1
    if num <= 2:
        return rng.choice(["操作", "职能", "操作", "技术"], p=[0.4, 0.3, 0.2, 0.1])
    if num <= 4:
        return rng.choice(["技术", "职能", "销售", "操作"], p=[0.42, 0.25, 0.20, 0.13])
    return rng.choice(["技术", "销售", "管理", "职能"], p=[0.45, 0.22, 0.18, 0.15])


def _title_of(family: str, level: str, rng: np.random.Generator) -> str:
    """按序列+职级取一个岗位名称。"""
    num = int(level[1:]) if level[1:].isdigit() else 1
    if level.upper().startswith("M"):
        band = "mid" if num == 1 else "high"
    elif num <= 2:
        band = "low"
    elif num <= 5:
        band = "mid"
    else:
        band = "high"
    return str(rng.choice(FAMILY_TITLES[family][band]))


# =============================================================================
# 二、核心生成逻辑
# =============================================================================

def generate(n: int = 150, seed: int = 20260830, with_band: bool = False,
             with_market: bool = True) -> pd.DataFrame:
    """
    生成模拟薪酬数据。

    参数
    ----------
    n : int
        目标人数。若 n 与 LEVEL_CONFIG 的人数合计不一致，按比例缩放各职级人数。
    seed : int
        随机种子，保证可复现（面试演示时结果每次一致）。
    with_band : bool
        是否填充 带宽下限/中位值/上限。False 时留空，用于演示「工具自动建议带宽」。
    with_market : bool
        是否填充 市场 P25/P50/P75。

    返回
    -------
    pandas.DataFrame，列 = CANONICAL_ORDER（标准字段名）
    """
    rng = np.random.default_rng(seed)

    # --- 1. 展开职级名单（按配置比例缩放到目标人数） -------------------------
    base_total = sum(c[1] for c in LEVEL_CONFIG)
    levels: list = []
    for lv, cnt, _ in LEVEL_CONFIG:
        k = max(1, int(round(cnt * n / base_total)))
        levels.extend([lv] * k)
    # 人数取整后可能与目标有 ±2 的差，用最后一个职级补齐/裁剪
    while len(levels) < n:
        levels.append(LEVEL_CONFIG[0][0])
    levels = levels[:n]
    rng.shuffle(levels)

    rows = []
    seq = 1
    for level in levels:
        mid_design = dict((lv, m) for lv, _, m in LEVEL_CONFIG)[level]
        family = _family_of_level(level, rng)
        dept = str(rng.choice(FAMILY_DEPTS[family]))
        title = _title_of(family, level, rng)

        # --- 2. 司龄：职级越高司龄越长（资深员工沉淀在高职级），加噪声 ---------
        num = int(level[1:]) if level[1:].isdigit() else 1
        base_tenure = {1: 1.0, 2: 2.0, 3: 3.2, 4: 4.6, 5: 6.0, 6: 7.6}[min(num, 6)]
        tenure = round(float(np.clip(rng.normal(base_tenure, 1.8), 0.2, 20.0)), 1)

        # --- 3. 目标 CR：主体正态分布在 1.0 附近，再定向注入红圈/绿圈 ---------
        # 红圈典型成因：长期未调薪的老员工被晋升稀释、历史高薪引进、稀缺人才溢价
        # 绿圈典型成因：快速晋升未同步调薪、新入职低于市场、历史低起点
        roll = rng.random()
        if roll < 0.11:
            target_cr = float(rng.uniform(1.22, 1.45))     # 红圈
        elif roll < 0.24:
            target_cr = float(rng.uniform(0.66, 0.79))     # 绿圈
        else:
            # 均值 1.0、标准差 0.10，并让司龄长的略微偏高（年资溢价）
            target_cr = float(np.clip(rng.normal(1.0 + 0.004 * tenure, 0.10), 0.80, 1.20))

        # --- 4. 月薪 = 中位值 × 目标CR，再加一点个体噪声（绩效/能力差异） -----
        salary = mid_design * target_cr * float(rng.normal(1.0, 0.02))
        # 薪资取整到百元（真实工资表常见做法）
        salary = float(np.round(salary / 100.0) * 100)

        # --- 5. 年度总现金 = 月薪×12 + 奖金（按序列给奖金月数），25% 留空 -----
        bonus_months = {"销售": 2.6, "技术": 2.2, "管理": 3.0, "操作": 1.2, "职能": 1.8}[family]
        annual = salary * 12 * (1 + bonus_months / 12.0) * float(rng.normal(1.0, 0.05))
        annual = float(np.round(annual / 100.0) * 100)
        if rng.random() < 0.25:
            annual = np.nan     # 模拟真实表常见缺列

        # --- 6. 岗位价值评估得分：与职级强相关，约 45% 留空 -------------------
        score_map = {"P1": 260, "P2": 360, "P3": 490, "P4": 630, "P5": 775,
                     "P6": 920, "M1": 1060, "M2": 1230, "M3": 1420}
        job_score = np.nan
        if rng.random() < 0.55:
            job_score = float(np.clip(rng.normal(score_map[level], 45), 100, 1600))
            job_score = float(round(job_score))

        # --- 7. 固浮比：按序列基准 + 噪声，30% 留空 ---------------------------
        pay_mix = ""
        if rng.random() < 0.70:
            fx, fl = JOB_FAMILY_PAY_MIX.get(family, (70, 30))
            # 高职级浮动占比略升（责任更大、与结果绑定更紧）
            bump = int(min(8, num))
            fx2 = max(30, fx - bump)
            pay_mix = f"{fx2}:{100 - fx2}"

        # --- 8. 市场数据：公司整体略低于市场 P50（约 -3%），留真实课题 --------
        mkt_p25 = mkt_p50 = mkt_p75 = np.nan
        if with_market:
            noise = float(rng.normal(1.03, 0.04))     # 公司中位值 ≈ 市场 P50 × 0.97
            mkt_p50 = float(np.round(mid_design * noise / 100.0) * 100)
            mkt_p25 = float(np.round(mkt_p50 * float(rng.uniform(0.80, 0.88)) / 100.0) * 100)
            mkt_p75 = float(np.round(mkt_p50 * float(rng.uniform(1.15, 1.25)) / 100.0) * 100)
            if rng.random() < 0.08:                   # 少量缺口，演示容错
                mkt_p25 = np.nan

        # --- 9. 带宽三列：默认留空（走「工具建议带宽」路径） ------------------
        band_min = band_mid = band_max = np.nan
        if with_band:
            _, spread = get_level_tier(level)
            band_mid = float(mid_design)
            band_min = float(np.round(mid_design / (1 + spread / 2.0) / 100.0) * 100)
            band_max = float(np.round(band_min * (1 + spread) / 100.0) * 100)

        rows.append({
            "emp_id": f"E{seq:04d}",
            "name": f"{rng.choice(SURNAMES)}**",       # 脱敏：只留姓
            "dept": dept,
            "level": level,
            "job_title": title,
            "job_family": family,
            "job_score": job_score,
            "monthly_salary": salary,
            "annual_total_cash": annual,
            "tenure_years": tenure,
            "perf_grade": str(rng.choice(PERF_POOL)),
            "pay_mix": pay_mix,
            "band_min": band_min,
            "band_mid": band_mid,
            "band_max": band_max,
            "mkt_p25": mkt_p25,
            "mkt_p50": mkt_p50,
            "mkt_p75": mkt_p75,
        })
        seq += 1

    df = pd.DataFrame(rows, columns=CANONICAL_ORDER)
    return df


# =============================================================================
# 三、乱列名版本（演示 AI 字段识别映射 + 数据清洗）
# =============================================================================

# 真实企业导出的表头往往是这种「半规范」形态：
#   带单位、带英文缩写、同名不同叫法、还有几列无关字段
MESSY_RENAME = {
    "emp_id": "工号",
    "name": "员工姓名",
    "dept": "所属部门",
    "level": "职务级别",
    "job_title": "岗位",
    "job_family": "职族",
    "job_score": "岗位评估得分",
    "monthly_salary": "基本工资(元/月)",
    "annual_total_cash": "年度总现金(元)",
    "tenure_years": "司龄(年)",
    "perf_grade": "上年度绩效",
    "pay_mix": "固定浮动比",
    "band_min": "带宽下限",
    "band_mid": "带宽中位值",
    "band_max": "带宽上限",
    "mkt_p25": "市场25分位",
    "mkt_p50": "市场中位值",
    "mkt_p75": "市场75分位",
}

# 乱列名版本中额外插入的无关列（真实表几乎总有几列没用的）
NOISE_COLS = {"入职日期": "2019-07-01", "备注": "", "数据状态": "有效"}


def make_messy(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """
    生成「乱列名 + 脏数据」版本，用于演示：
      - auto_suggest_mapping() 的语义识别
      - 工具对重复行 / 非数值 / 空值的容错清洗
    """
    m = df.rename(columns=MESSY_RENAME).copy()

    # 打乱列顺序（真实表的列顺序基本是随机的）
    cols = list(m.columns)
    rng.shuffle(cols)
    m = m[cols]

    # 插入无关列
    for i, (c, default) in enumerate(NOISE_COLS.items()):
        m.insert(min(i * 2, len(m.columns)), c, default)

    # 注入 4 类脏数据（都有真实对应场景）
    # ① 重复员工记录（系统同步重复导入）
    m = pd.concat([m, m.iloc[[0]].copy()], ignore_index=True)
    # ② 薪资列混入非数值（"待定" / "—" / 空）—— 先放宽 dtype 再写入，避免 pandas 告警
    m["基本工资(元/月)"] = m["基本工资(元/月)"].astype(object)
    m.loc[m.index[3], "基本工资(元/月)"] = "待定"
    m.loc[m.index[7], "基本工资(元/月)"] = "—"
    # ③ 必填字段缺失（新入职未定薪）
    m.loc[m.index[11], "基本工资(元/月)"] = np.nan
    # ④ 绩效等级大小写/写法不统一
    m.loc[m.index[5], "上年度绩效"] = "a+"
    m.loc[m.index[9], "上年度绩效"] = "B "
    return m


# =============================================================================
# 四、主入口
# =============================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="生成脱敏的模拟薪酬数据（150 条默认）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("-n", "--num", type=int, default=150, help="生成条数")
    ap.add_argument("--seed", type=int, default=20260830, help="随机种子")
    ap.add_argument("--with-band", action="store_true", help="填充带宽下限/中位值/上限")
    ap.add_argument("--no-market", action="store_true", help="不生成市场 P25/P50/P75")
    ap.add_argument("--no-excel", action="store_true", help="不输出 Excel")
    ap.add_argument("-o", "--outdir", default=os.path.join(HERE, "data"), help="输出目录")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    rng = np.random.default_rng(args.seed + 1)

    df = generate(n=args.num, seed=args.seed,
                  with_band=args.with_band, with_market=not args.no_market)

    # --- 标准版 CSV -----------------------------------------------------------
    csv_path = os.path.join(args.outdir, "sample_salary.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")   # BOM：Excel 直接打开不乱码

    # --- 乱列名版 CSV（演示映射 + 清洗） --------------------------------------
    messy_path = os.path.join(args.outdir, "messy_salary.csv")
    make_messy(df, rng).to_csv(messy_path, index=False, encoding="utf-8-sig")

    # --- Excel 版（含字段说明 sheet） -----------------------------------------
    xlsx_path = os.path.join(args.outdir, "sample_salary.xlsx")
    if not args.no_excel:
        try:
            doc = pd.DataFrame(
                [{"标准字段名": k, "中文名": v[0], "业务含义": v[1],
                  "是否必填": "是" if v[2] else "否", "类型": v[3]}
                 for k, v in CANONICAL_FIELDS.items()])
            with pd.ExcelWriter(xlsx_path, engine="openpyxl") as w:
                df.to_excel(w, sheet_name="薪酬数据", index=False)
                doc.to_excel(w, sheet_name="字段说明", index=False)
        except Exception as e:                       # openpyxl 缺失不应中断造数
            print(f"[warn] Excel 输出跳过（{type(e).__name__}: {e}）")
            xlsx_path = None

    # --- 控制台摘要（只打印统计口径，不逐行打印数据） --------------------------
    print("=" * 68)
    print("模拟薪酬数据已生成（全部为随机合成数据，与任何真实自然人无关）")
    print("=" * 68)
    print(f"人数            : {len(df)}")
    print(f"职级数          : {df['level'].nunique()}  ({', '.join(sorted(df['level'].unique()))})")
    print(f"月薪区间        : {df['monthly_salary'].min():,.0f} ~ {df['monthly_salary'].max():,.0f} 元")
    print(f"月薪中位数      : {df['monthly_salary'].median():,.0f} 元")
    print(f"年度薪资总额    : {df['monthly_salary'].sum() * 12 / 10000:,.1f} 万元")
    print(f"市场数据        : {'有' if not args.no_market else '无'}")
    print(f"带宽数据        : {'有' if args.with_band else '无（将由工具生成建议带宽）'}")
    print("-" * 68)
    print("各职级人数与月薪中位数：")
    g = df.groupby("level")["monthly_salary"].agg(["count", "median"]).sort_index()
    for lv, r in g.iterrows():
        print(f"  {lv:<4} 人数 {int(r['count']):>3}   中位数 {r['median']:>10,.0f} 元")
    print("-" * 68)
    print(f"输出文件：\n  {csv_path}\n  {messy_path}" + (f"\n  {xlsx_path}" if xlsx_path else ""))
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
