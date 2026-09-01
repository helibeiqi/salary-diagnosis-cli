# -*- coding: utf-8 -*-
"""
schemas.py — 薪酬诊断 Agent 的「标准字段词典」与业务参数字典
================================================================================
本模块是整个项目的**单一真理源（Single Source of Truth）**：

1. CANONICAL_FIELDS：定义 18 个标准字段的内部名、中文显示名、业务含义、是否必填。
   所有下游计算（CR、渗透率、带宽、调薪、固浮比）只读标准字段名，
   绝不直接读用户原始列名 —— 这样才能做到「任意不规范表头 → 映射一次 → 全链路通用」。

2. 表头匹配引擎（两代并存，v2 主路 + v1 兜底）：
   · v2：SEMANTIC_ROOTS / DIMENSION_MODIFIERS / AMBIGUITY_MODIFIERS 三层词根表
     + FIELD_ROUTING_TABLE 路由表 + parse_column_semantics() 确定性匹配算法。
     这是**主路**，把「这是什么（语义）」与「哪个维度（月/年、上/中/下限、P25/50/75）」
     拆开，用组合而非穷举覆盖表头空间，并把「缺维度」显式标记为 ambiguous。
   · v1：FIELD_ALIASES 扁平别名词典。**降级为 fallback，不删除** ——
     词根表未命中时仍做子串匹配（confidence=60 且强制 ambiguous=True），
     保证历史上真实见过的怪表头不会因为重构而丢失覆盖。

3. 业务参数字典（职级分层 / 默认带宽幅度 / 绩效权重 / 固浮比基准 / 海氏美世打分维度）：
   全部集中在本文件，便于 HR 按公司实际口径一条条改，不用翻代码。

方法论依据见 README「模块方法论」章节。
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# =============================================================================
# 一、标准字段定义（内部统一命名，全部小写 + 下划线）
# =============================================================================

# 字段结构：内部名 -> (中文显示名, 业务含义, 是否必填, 数据类型)
CANONICAL_FIELDS: Dict[str, Tuple[str, str, bool, str]] = {
    "emp_id": ("员工ID", "员工唯一标识，用于去重与结果回写", True, "str"),
    "name": ("姓名（脱敏）", "员工姓名，强烈建议使用脱敏值（如 张**）", False, "str"),
    "dept": ("部门", "所属部门/一级组织，用于分组统计", False, "str"),
    "level": ("职级", "职级（如 P1-P6 / M1-M3），带宽与 CR 计算的分组主键", True, "str"),
    "job_title": ("岗位名称", "具体岗位（如 高级Java工程师）", False, "str"),
    "job_family": ("岗位序列", "岗位序列：销售/技术/管理/操作/职能，固浮比分析主键", False, "str"),
    "job_score": ("岗位价值评估得分", "海氏/美世岗位评估得分，用于校验职级合理性", False, "number"),
    "monthly_salary": ("当前月薪", "月固定+月浮动现金的合计月薪（元），CR 计算的分子", True, "number"),
    "annual_total_cash": ("年度总现金", "年度现金总收入（元）= 月薪×12 + 奖金/提成；缺省时用月薪×12 推算", False, "number"),
    "tenure_years": ("司龄", "在本公司的服务年限（年），用于解释 CR 偏高（老员工积累）", False, "number"),
    "perf_grade": ("绩效等级", "最近一次绩效等级 A/B/C/D，调薪加权分配的核心依据", False, "str"),
    "pay_mix": ("固浮比", "固定薪:浮动薪，如 70:30 或 0.7", False, "str"),
    "band_min": ("带宽下限", "该职级薪酬带宽最小值（月薪，元）", False, "number"),
    "band_mid": ("带宽中位值", "该职级薪酬带宽中位值（月薪，元），CR 的分母", False, "number"),
    "band_max": ("带宽上限", "该职级薪酬带宽最大值（月薪，元）", False, "number"),
    "mkt_p25": ("市场P25", "市场对标 25 分位月薪（元）", False, "number"),
    "mkt_p50": ("市场P50", "市场对标 50 分位月薪（元），最常用对标锚点", False, "number"),
    "mkt_p75": ("市场P75", "市场对标 75 分位月薪（元），核心/稀缺岗位常用锚点", False, "number"),
}

# 必填字段：缺任何一个都无法开始诊断
REQUIRED_FIELDS: List[str] = [k for k, v in CANONICAL_FIELDS.items() if v[2]]

# 数值型字段（读入后需强制转 float；非数值会置 NaN 并给出清洗提示）
NUMERIC_FIELDS: List[str] = [k for k, v in CANONICAL_FIELDS.items() if v[3] == "number"]

# 输出 CSV 时的列顺序
CANONICAL_ORDER: List[str] = list(CANONICAL_FIELDS.keys())

# =============================================================================
# 二、字段别名词典（auto_suggest_mapping 的匹配素材）
# =============================================================================
# 设计原则：只写「真实 HR 表里见过的写法」，不要臆造。
# 匹配时会做归一化（去空格/标点/括号内容、全角转半角、英文小写），再做包含匹配。

FIELD_ALIASES: Dict[str, List[str]] = {
    "emp_id": ["员工id", "员工编号", "工号", "员工号", "人员编号", "职员编号", "empid",
               "employeeid", "emp_no", "empcode", "staffid", "id", "number", "no"],
    "name": ["姓名", "员工姓名", "名字", "人员姓名", "name", "employeename", "staffname",
             "员工名称", "职工姓名"],
    "dept": ["部门", "所属部门", "部门名称", "一级部门", "二级部门", "组织", "组织机构",
             "所属组织", "成本中心", "department", "dept", "org", "organization", "bu",
             "事业部", "中心", "科室", "车间"],
    "level": ["职级", "职位级别", "职务级别", "岗位级别", "级别", "职等", "职级代码",
              "薪级", "岗位等级", "员工级别", "level", "grade", "joblevel", "jobgrade",
              "band", "级别代码", "rank", "子职级"],
    "job_title": ["岗位名称", "岗位", "职位", "职务", "岗位名称(标准)", "标准岗位",
                  "职位名称", "职务名称", "岗位title", "jobtitle", "position", "job",
                  "role", "岗位职务"],
    "job_family": ["岗位序列", "职族", "职位序列", "岗位类别", "序列", "职能序列",
                   "族群", "jobfamily", "jobfamily", "family", "jobcategory",
                   "岗位族", "职业序列", "人员类别"],
    "job_score": ["岗位价值评估得分", "岗位评估得分", "海氏得分", "美世得分", "岗位得分",
                  "评估分数", "岗位价值分", "价值评估得分", "ipe得分", "jobscore",
                  "jobpoints", "evaluationscore", "岗位点数", "职级分"],
    "monthly_salary": ["当前月薪", "月薪", "月工资", "基本工资", "月基本工资", "月固定工资",
                       "月薪资", "月度工资", "月度薪资", "月标准工资", "月薪酬", "月收入",
                       "月现金", "月总现金", "标准月薪", "basepay", "basesalary",
                       "monthlysalary", "monthlypay", "salary", "monthly", "base",
                       "固定工资", "月工资标准", "现工资", "调整前工资"],
    "annual_total_cash": ["年度总现金", "年总现金", "年薪总额", "年度现金总收入", "年现金",
                          "年度总收入", "年总收入", "年薪", "年度薪酬总额", "年度总薪酬",
                          "totalcash", "annualcash", "annualsalary", "tc", "totalcomp",
                          "年包", "年度总包", "全薪", "年度货币收入"],
    "tenure_years": ["司龄", "司龄(年)", "工龄", "服务年限", "入职年限", "在公司年限",
                     "本公司工龄", "年资", "司龄年", "tenure", "seniority",
                     "serviceyear", "yearsofservice"],
    "perf_grade": ["绩效等级", "绩效", "绩效考核等级", "绩效评级", "年度绩效", "绩效结果",
                   "绩效等次", "上年度绩效", "绩效等级(a/b/c/d)", "performance",
                   "perfgrade", "rating", "performancerating", "绩效档次", "考核结果"],
    "pay_mix": ["固浮比", "固定浮动比", "固定浮动比例", "固浮比例", "薪酬结构",
                "固定:浮动", "固浮", "固定浮动", "paymix", "fixvarratio",
                "fixedvariableratio", "薪酬固浮比", "固定浮动占比"],
    "band_min": ["带宽下限", "薪酬带宽下限", "带宽最小值", "薪档下限", "下限", "最小值",
                 "下限值", "bandmin", "rangemin", "min", "薪资下限", "带宽下沿"],
    "band_mid": ["带宽中位值", "带宽中值", "中位值", "薪酬中位值", "带宽中点", "中点值",
                 "中值", "bandmid", "rangemid", "midpoint", "mid", "薪资中位值",
                 "带宽中位值(月)", "参照中位"],
    "band_max": ["带宽上限", "薪酬带宽上限", "带宽最大值", "薪档上限", "上限", "最大值",
                 "上限值", "bandmax", "rangemax", "max", "薪资上限", "带宽上沿"],
    "mkt_p25": ["市场p25", "市场25分位", "市场25分位值", "p25", "市场25", "25分位",
                "市场低分位", "marketp25", "p25(月)", "市场对标p25", "市场25分位月薪"],
    "mkt_p50": ["市场p50", "市场50分位", "市场中位值", "市场50分位值", "p50", "市场50",
                "50分位", "市场中位数", "marketp50", "市场median", "市场对标p50",
                "市场50分位月薪", "市场薪酬中位"],
    "mkt_p75": ["市场p75", "市场75分位", "市场75分位值", "p75", "市场75", "75分位",
                "市场高分位", "marketp75", "p75(月)", "市场对标p75", "市场75分位月薪"],
}

# =============================================================================
# 二·补、结构化词根表（表头匹配引擎 v2 的单一真理源）
# =============================================================================
# 为什么要在 FIELD_ALIASES 之上再加一层
# -----------------------------------------------------------------------------
# 扁平别名词典的死穴是**穷举不完**：真实表头是「语义 × 维度」的组合爆炸 ——
#     带宽 × {下限, 中位, 上限}      市场 × {P25, P50, P75}
#     薪资 × {月, 年}                评估 × {岗位, 绩效}
# 词典必须为每个组合各写一条（上面四组就是 3+3+2+2 条起），漏一条就退化成子串
# 瞎猜；更糟的是「下限」这种**只有维度、没有语义**的列名，词典只能给出一个看起来
# 很确定的错误答案（命中 band_min，可它同样可能是 mkt_p25），把本该交给人/模型的
# 歧义**悄悄吞掉**。这类"静默猜对了算运气、猜错了没人知道"的行为，在薪酬场景里
# 直接等于报告里的数字错位。
#
# 三层结构把两件事拆开，组合关系交给路由表表达：
#     第一层 SEMANTIC_ROOTS      —— 语义词根：这一列在讲什么概念
#     第二层 DIMENSION_MODIFIERS —— 维度修饰：月/年、上/中/下限、P25/P50/P75
#     第三层 AMBIGUITY_MODIFIERS —— 歧义标记：哪些词根单独出现时**不足以**定字段
# 于是「3 个词根 + 3 个修饰」就覆盖 9 种列名；而「有语义没维度」这件事会被
# **显式标记**成 ambiguous 并附具体原因，交给 LLM 或用户拍板 ——
# 这正是"确定性优先、LLM 只在程序标记 ambiguous 时做最后拍板"的落点。
#
# 词表维护约定
# -----------------------------------------------------------------------------
# · 标 [spec] 的条目是需求原文钉死的，不要随意删改；
# · 标 [ext] 的条目是实测 sample_salary.csv / messy_salary.csv 与 6 个 fixture
#   后**必须补**的扩展（如 "现金" 不在原始 compensation 词根里，导致
#   "年度总现金" 完全不命中）。每条 [ext] 都对应一个真实的失配用例。
# · **复合词根**（如 "职务级别" / "jobfamily" / "绩效等级" / "perfgrade"）是刻意
#   写进来的：匹配算法按「最长词根优先 + 占位消费」扫描，复合词根会一次吃掉整段，
#   避免 "职务级别" 被拆成 job("职务") + level("级别") 两个词根而无法路由。
#   这是本引擎里最容易踩的坑 —— 删掉复合词根，多词根冲突会立刻回来。

#: 第一层：语义词根（表达"这是什么"）。值为该语义下的写法，匹配时会先 normalize_col。
SEMANTIC_ROOTS: Dict[str, List[str]] = {
    "compensation": [
        # [spec]
        "工资", "薪资", "薪酬", "薪", "报酬", "收入", "salary", "pay", "comp", "income",
        # [ext] "年度总现金" / "Annual TC" / "底薪" / "年包" 在原表里全部不命中
        "现金", "总现金", "totalcash", "cash", "tc", "底薪", "年包", "货币收入",
    ],
    "band": ["带宽", "薪档", "薪酬带", "band", "range"],                       # [spec]
    "market": ["市场", "market", "行业", "对标",                                # [spec]
               "mkt"],                                                          # [ext] mkt_p25 列名
    "percentile": ["分位", "分位值", "pct", "percentile"],                      # [spec]
    "level": [
        # [spec]
        "职级", "级别", "职等", "薪级", "等级", "level", "grade", "rank",
        # [ext] 复合词根：不加则被拆成 job+level 双词根，路由退化为歧义
        "职务级别", "岗位级别", "职位级别", "员工级别", "职级代码", "级别代码",
        "岗位等级", "joblevel", "jobgrade",
        # [ext] 极端简写：'岗级' 既不含 '岗位' 也不含 '级别'，不单列会整列失配
        "岗级", "薪等",
    ],
    "job": ["岗位", "职位", "职务", "job", "position", "role"],                 # [spec]
    "family": [
        "序列", "职族", "族群", "family", "category",                           # [spec]
        # [ext] 复合词根，同上（"岗位序列" 会被拆成 job+family）
        "岗位序列", "职位序列", "职能序列", "职业序列", "岗位类别", "人员类别",
        "岗位族", "jobfamily", "jobcategory",
    ],
    "evaluation": ["评估", "评价", "打分", "得分", "evaluation", "score", "points",  # [spec]
                   "海氏得分", "美世得分", "ipe得分"],                          # [ext]
    # ⚠️ 刻意**不收** 'jobscore' 这类跨类复合写法：它会被最长优先扫描整段吃掉，
    #    连带吞掉 job 词根，使 {job, evaluation} 双词根路由永远匹配不上
    #    （实测 'job_score' 因此退化成 evaluation 歧义）。'job'+'score' 天然可组合，
    #    无需复合词根。v1 的 FIELD_ALIASES 里仍保留 'jobscore' 作兜底，不删。
    "performance": [
        "绩效", "考核", "评级", "performance", "rating", "perf",                 # [spec]
        # [ext] 复合词根 "绩效等级" 必须在，否则被拆成 performance+level
        "绩效等级", "绩效评级", "绩效等次", "绩效结果", "绩效档次", "考核结果",
        "考核等级", "kpi", "perfgrade", "performancerating",
    ],
    "tenure": ["司龄", "工龄", "年资", "服务年限", "tenure", "seniority",        # [spec]
               "入职年限", "工作年限", "在公司年限", "serviceyear", "yearsofservice"],  # [ext]
    "paymix": ["固浮", "固定浮动", "薪酬结构", "paymix", "fixvar",              # [spec]
               "固浮比", "固定浮动比", "fixvarratio", "fixedvariableratio"],    # [ext]
    "person": ["姓名", "名字", "name",                                          # [spec]
               "员工姓名", "职工姓名", "employeename", "staffname"],            # [ext]
    "dept": ["部门", "组织", "department", "dept", "org", "bu",                 # [spec]
             "所属部门", "组织机构", "成本中心", "事业部"],                     # [ext]
    "id": ["编号", "工号", "id", "code", "no",                                  # [spec]
           "员工编号", "人员编号", "职员编号", "empid", "employeeid", "staffid"],  # [ext]
}

#: 第二层：维度修饰词（表达"哪个维度/位置"）。dim -> {写法: 归一化维度值}
DIMENSION_MODIFIERS: Dict[str, Dict[str, str]] = {
    "time": {
        # [spec]
        "月": "monthly", "月度": "monthly",
        "年": "annual", "年度": "annual", "年包": "annual",
        # [ext] 英文表头（"Annual TC" / "monthly_salary"）
        "monthly": "monthly", "mth": "monthly",
        "annual": "annual", "annually": "annual", "yearly": "annual",
    },
    "position": {
        # [spec]
        "下限": "min", "最小值": "min",
        "上限": "max", "最大值": "max",
        "中位": "mid", "中值": "mid", "中点": "mid",
        # [ext] 缩写与同义写法（"带宽min" / "带宽中位值" / "带宽下沿"）
        "min": "min", "max": "max", "mid": "mid", "midpoint": "mid",
        "中位值": "mid", "中位数": "mid", "中点值": "mid",
        "下沿": "min", "上沿": "max", "最低": "min", "最高": "max",
        "低分位": "min", "高分位": "max",
    },
    "percentile_num": {
        # [spec]
        "25": "p25", "p25": "p25",
        "50": "p50", "p50": "p50",
        "75": "p75", "p75": "p75",
        # [ext]
        "median": "p50",
    },
}

#: 第三层：歧义修饰词标记 —— 这些词根单独出现时**不足以**判定字段，必须组合。
#: 键 = 触发歧义的语义词根（或修饰维度）名，值 = 给 LLM / 用户看的具体原因。
AMBIGUITY_MODIFIERS: Dict[str, str] = {
    "compensation": "薪资类字段但缺少月/年时间维度",
    "percentile": "分位类字段但缺少 P25/P50/P75 数值",
    "evaluation": "评估类字段但缺少岗位/绩效对象",
    "position": "有上下限修饰但缺少带宽/市场语义前缀",
    # [ext] 与 position 对称的一条：只有分位数字、没有语义前缀（列名 "P50"）。
    # 需求原文只列了 4 条，这条是实测 fixture 6「极端简写」补的 —— 否则 'P50'
    # 会掉进"别名词典 fallback"给出笼统原因，模型看不到到底缺什么。
    "percentile_num": "有 P25/P50/P75 分位数值但缺少市场/带宽语义前缀",
}

# -----------------------------------------------------------------------------
# 路由表：Root × Modifier -> Canonical Field
# -----------------------------------------------------------------------------
# 条目格式：(语义词根集合, 维度修饰集合, 标准字段, 是否需要消歧)
#
# 匹配与排序规则（parse_column_semantics 实现，改表前必读）
#   1. 一个条目**可匹配**当且仅当：条目词根集合 ⊆ 列名命中的词根集合，
#      且条目要求的每一个 (维度, 值) 都被列名命中的修饰满足。
#   2. 空修饰集合 = **维度不敏感**（多余的修饰不影响匹配），
#      所以 "上年度绩效"（含 time=annual）仍能命中 ({"performance"}, {})。
#   3. 多个条目同时可匹配时，按 (词根数 desc, 修饰数 desc, 表内顺序 asc) 取最具体的：
#      · "月薪" 同时匹配 ({compensation},{time:monthly}) 与 ({compensation},{},歧义)，
#        前者修饰数更多 → 精确命中 monthly_salary，不会被歧义条目截胡；
#      · "基本工资" 只能匹配后者 → 正确地标成 ambiguous。
#      这条排序规则是"精确优先、歧义兜底"能同时成立的唯一原因。
FIELD_ROUTING_TABLE: List[Tuple[set, Dict[str, str], Optional[str], bool]] = [
    # (语义词根集合, 维度修饰集合, 标准字段, 是否需要消歧)
    ({"compensation"}, {"time": "monthly"}, "monthly_salary", False),          # [spec]
    ({"compensation"}, {"time": "annual"},  "annual_total_cash", False),       # [spec]
    ({"band"},         {"position": "min"}, "band_min", False),                # [spec]
    ({"band"},         {"position": "mid"}, "band_mid", False),                # [spec]
    ({"band"},         {"position": "max"}, "band_max", False),                # [spec]
    ({"market"},       {"percentile_num": "p25"}, "mkt_p25", False),           # [spec]
    ({"market"},       {"percentile_num": "p50"}, "mkt_p50", False),           # [spec]
    ({"market"},       {"percentile_num": "p75"}, "mkt_p75", False),           # [spec]
    # [ext] 市场 × 上/中/下限：真实表头大量写 "市场中位值" 而非 "市场50分位"，
    #       缺这三条会让 fixture 4「带宽+市场共存消歧」的市场侧全部退化为歧义。
    #       口径：市场下限≈低分位(P25)、中位≈P50、上限≈高分位(P75)，与
    #       FIELD_ALIASES 里 "市场低分位"→mkt_p25 / "市场高分位"→mkt_p75 一致。
    ({"market"},       {"position": "min"}, "mkt_p25", False),                 # [ext]
    ({"market"},       {"position": "mid"}, "mkt_p50", False),                 # [ext]
    ({"market"},       {"position": "max"}, "mkt_p75", False),                 # [ext]
    ({"level"},        {}, "level", False),                                    # [spec]
    ({"job"},          {}, "job_title", False),                                # [spec]
    ({"family"},       {}, "job_family", False),                               # [spec]
    ({"evaluation", "job"},         {}, "job_score", False),                   # [spec]
    ({"evaluation", "performance"}, {}, "perf_grade", False),                  # [spec]
    ({"performance"},  {}, "perf_grade", False),                               # [spec]
    ({"tenure"},       {}, "tenure_years", False),                             # [spec]
    ({"paymix"},       {}, "pay_mix", False),                                  # [spec]
    ({"person"},       {}, "name", False),                                     # [spec]
    ({"dept"},         {}, "dept", False),                                     # [spec]
    ({"id"},           {}, "emp_id", False),                                   # [spec]
    # ---- 需要消歧的组合（词根命中但维度不足，程序拒绝拍板，交 LLM / 用户）----
    ({"compensation"}, {}, None, True),    # 有薪资无月/年 → ambiguous          # [spec]
    ({"percentile"},   {}, None, True),    # 有分位无市场前缀无数值 → ambiguous  # [spec]
    ({"evaluation"},   {}, None, True),    # 有评估无对象 → ambiguous            # [spec]
]

#: 歧义条目的候选字段：某个词根被标成歧义时，把"它可能是哪几个标准字段"摆给
#: LLM / 用户。**只从这里取，模型不得发明字段**（见 registry.confirm_mapping 描述）。
AMBIGUOUS_CANDIDATE_FIELDS: Dict[str, List[str]] = {
    "compensation": ["monthly_salary", "annual_total_cash"],
    "percentile": ["mkt_p25", "mkt_p50", "mkt_p75"],
    "evaluation": ["job_score", "perf_grade"],
    # 只有 position 修饰、没有语义前缀时，按修饰值给候选
    "position:min": ["band_min", "mkt_p25"],
    "position:mid": ["band_mid", "mkt_p50"],
    "position:max": ["band_max", "mkt_p75"],
    # 只给了分位数字、没给"市场/带宽"语义前缀（列名就叫 "P50" / "50分位"）。
    # 实务上八成是市场分位，但 P50 也可能是内部带宽中位 → 仍交给人/模型拍板。
    "percentile_num:p25": ["mkt_p25"],
    "percentile_num:p50": ["mkt_p50", "band_mid"],
    "percentile_num:p75": ["mkt_p75"],
}

# =============================================================================
# 三、职级分层与默认带宽幅度
# =============================================================================
# 行业惯例（参考 WorldatWork / 美世 IPE 落地实践）：
#   基层/操作类：带宽幅度 20%-30%（工作内容标准化，成长空间有限）
#   专业/技术类：30%-40%（能力差异大，需要带宽容纳资深与新手）
#   中层管理/专家：40%-50%（绩效与能力差异显著放大）
#   高层管理：50%+（岗位影响面大，个体差异极大）
#
# 注：带宽幅度（Band Spread / Range Spread）的标准定义 = (上限 - 下限) / 下限
#     本项目统一用「相对下限的幅度」这一口径，并在报告里显式标注。

LEVEL_TIER_RULES: Dict[str, Tuple[str, float]] = {
    # 职级前缀/精确职级 -> (层级名, 默认带宽幅度)
    "P1": ("基层/操作", 0.25),
    "P2": ("基层/操作", 0.28),
    "P3": ("专业/技术", 0.35),
    "P4": ("专业/技术", 0.38),
    "P5": ("中层/专家", 0.45),
    "P6": ("中层/专家", 0.48),
    "M1": ("中层管理", 0.45),
    "M2": ("高层管理", 0.55),
    "M3": ("高层管理", 0.60),
}

# 未在规则表内时的兜底：按字母前缀猜
TIER_FALLBACK: Dict[str, Tuple[str, float]] = {
    "M": ("管理层", 0.50),
    "P": ("专业/技术", 0.35),
}
DEFAULT_SPREAD: float = 0.35          # 完全无法识别时的兜底带宽幅度
DEFAULT_MIDPOINT_DIFF: float = 0.15   # 默认中位值级差（相邻职级中位值增幅 15%）

# 职级排序权重（让 P1<P2<...<P6<M1<M2<M3）
LEVEL_ORDER_HINT: List[str] = ["P1", "P2", "P3", "P4", "P5", "P6", "M1", "M2", "M3"]


def level_sort_key(level: str) -> Tuple[int, int, str]:
    """职级排序键：先在提示表里找，找不到再按 (字母序, 数字) 排，保证结果稳定可复现。"""
    s = str(level).strip().upper()
    if s in LEVEL_ORDER_HINT:
        return (0, LEVEL_ORDER_HINT.index(s), s)
    m = re.match(r"^([A-Za-z]+)[^\d]*(\d+)?$", s)
    if m:
        prefix, num = m.group(1).upper(), int(m.group(2) or 0)
        return (1, ord(prefix[0]) * 1000 + num, s)
    return (2, 0, s)


def get_level_tier(level: str) -> Tuple[str, float]:
    """返回职级所属层级名与默认带宽幅度。"""
    s = str(level).strip().upper()
    if s in LEVEL_TIER_RULES:
        return LEVEL_TIER_RULES[s]
    m = re.match(r"^([A-Za-z]+)", s)
    if m and m.group(1).upper() in TIER_FALLBACK:
        tier, spread = TIER_FALLBACK[m.group(1).upper()]
        # 同前缀下数字越大，带宽略增（每级 +2%，上限 60%）
        num_m = re.search(r"(\d+)", s)
        if num_m:
            spread = min(0.60, spread + 0.02 * max(0, int(num_m.group(1)) - 1))
        return (tier, round(spread, 3))
    return ("未分层", DEFAULT_SPREAD)


# =============================================================================
# 四、岗位序列（固浮比分析用）
# =============================================================================
# 目标固浮比（固定:浮动）的行业参考值：
#   销售 40:60 —— 业绩可直接归因到个人、结算周期短（季度/月），适合高浮动
#   技术 70:30 —— 产出偏长周期、强协作，浮动过高会破坏知识共享
#   管理 60:40 —— 与经营结果绑定，但需保留固定部分承担管理职责
#   操作 80:20 —— 产出标准化、个体可控度低，浮动仅用于质量/安全考核
#   职能 75:25 —— 产出难量化，浮动主要用于年度绩效兑现
JOB_FAMILY_PAY_MIX: Dict[str, Tuple[int, int]] = {
    "销售": (40, 60),
    "技术": (70, 30),
    "管理": (60, 40),
    "操作": (80, 20),
    "职能": (75, 25),
}
DEFAULT_PAY_MIX: Tuple[int, int] = (70, 30)

# 部门 -> 岗位序列 的推断规则（真实表常常只有部门没有序列）
DEPT_FAMILY_RULES: Dict[str, str] = {
    "销售": "销售", "营销": "销售", "市场": "销售", "大客户": "销售", "渠道": "销售",
    "商务": "销售", "客户": "销售",
    "研发": "技术", "技术": "技术", "产品": "技术", "it": "技术", "软件": "技术",
    "工程": "技术", "设计": "技术", "测试": "技术", "工艺": "技术", "设备": "技术",
    "生产": "操作", "制造": "操作", "厂务": "操作", "车间": "操作", "运营": "操作",
    "物流": "操作", "仓储": "操作", "品质": "操作", "质检": "操作",
    "人力": "职能", "行政": "职能", "财务": "职能", "法务": "职能", "采购": "职能",
    "hr": "职能", "财务共享": "职能", "战略": "职能",
    "总经办": "管理", "总经理": "管理", "管理": "管理", "高管": "管理",
}


def infer_job_family(dept: Optional[str], level: Optional[str] = None) -> str:
    """由部门（必要时结合职级）推断岗位序列；无法推断返回 '职能'（最保守的口径）。"""
    if level:
        lv = str(level).strip().upper()
        if lv.startswith("M"):
            return "管理"
    if dept:
        d = str(dept).strip().lower()
        for kw, fam in DEPT_FAMILY_RULES.items():
            if kw.lower() in d:
                return fam
    return "职能"


# =============================================================================
# 五、绩效等级权重（调薪策略 C 用）
# =============================================================================
# 依据：绩效调薪矩阵（Merit Increase Matrix）—— 高绩效者获得高于平均的调薪，
# 低绩效者少涨或不涨，从而在有限预算内拉开差距、保留关键人才。
PERF_WEIGHTS: Dict[str, float] = {"A": 1.8, "B": 1.2, "C": 0.5, "D": 0.0}
DEFAULT_PERF_WEIGHT: float = 1.0

# =============================================================================
# 六、红绿圈判定阈值
# =============================================================================
# CR（Compa-Ratio）= 个人薪资 / 该职级带宽中位值
#   1.00 = 正好在中位值；行业通行做法：
#   0.80 ≤ CR ≤ 1.20 视为合理区间（对应带宽渗透率约 20%-80%）
#   CR > 1.20 或 薪资 > 带宽上限  → 红圈（Red Circle）：薪酬高于带宽，成本溢出
#   CR < 0.80 或 薪资 < 带宽下限  → 绿圈（Green Circle）：薪酬低于带宽，流失风险
RED_CIRCLE_CR: float = 1.20
GREEN_CIRCLE_CR: float = 0.80

# =============================================================================
# 七、岗位价值评估（模块 6）
# =============================================================================
# 海氏（Hay Guide Chart-Profile Method）三要素及常用权重：
#   1. 知识技能 Know-How：岗位所需的专业知识、管理技巧、人际技能
#   2. 解决问题 Problem Solving：思维环境与思维难度
#   3. 应负责任 Accountability：行动自由度、影响性质、影响范围
# 美世 IPE（International Position Evaluation）四因素：影响 / 沟通 / 创新 / 知识
# 本项目两种模型都支持，由 model 参数切换；分数越高岗位价值越大、职级越高。
JOB_EVAL_MODELS: Dict[str, Dict[str, object]] = {
    "hay": {
        "label": "海氏三要素法",
        "factors": ["知识技能", "解决问题", "应负责任"],
        "weights": {"知识技能": 0.4, "解决问题": 0.25, "应负责任": 0.35},
        "subfactors": {
            "知识技能": ["专业知识深度", "管理技能要求", "人际沟通技能"],
            "解决问题": ["思维环境复杂度", "思维难度挑战"],
            "应负责任": ["行动自由度", "对结果的影响", "影响范围大小"],
        },
        "scale": (1, 10),   # 每个子维度 1-10 分
    },
    "mercer": {
        "label": "美世IPE四因素法",
        "factors": ["影响", "沟通", "创新", "知识"],
        "weights": {"影响": 0.35, "沟通": 0.20, "创新": 0.20, "知识": 0.25},
        "subfactors": {
            "影响": ["影响性质", "贡献度", "组织规模"],
            "沟通": ["沟通性质", "沟通范围"],
            "创新": ["创新要求", "复杂性"],
            "知识": ["知识要求", "团队角色", "知识应用"],
        },
        "scale": (1, 10),
    },
}

# 岗位得分 -> 建议职级（可覆盖；区间为左闭右开）
JOB_SCORE_LEVEL_BANDS: List[Tuple[float, float, str]] = [
    (0, 300, "P1"), (300, 420, "P2"), (420, 560, "P3"), (560, 700, "P4"),
    (700, 850, "P5"), (850, 1000, "P6"), (1000, 1150, "M1"),
    (1150, 1350, "M2"), (1350, 99999, "M3"),
]


# -----------------------------------------------------------------------------
# ⚠️ 量纲换算系数（2026-08-30 修复 P0 缺陷）
# -----------------------------------------------------------------------------
# 缺陷描述：JOB_EVAL_MODELS 的子维度打分是 1-10 分，各要素内取平均后按权重加权
# 得到 W ∈ [1, 10]；而 JOB_SCORE_LEVEL_BANDS 用的是岗位评估的**百分制总分**
# （业界海氏/美世通行 0~1600 分量纲）。此前若直接把 W 传给 score_to_level()，
# W 恒落入 (0, 300) 区间 → **所有岗位一律判为 P1**，模块 6 完全失效。
#
# 修复：引入换算系数把 W 映射到百分制总分。
#   JOB_SCORE_SCALE = 160 = 目标总分上限 1600 ÷ 子维度满分 10
#   换算后 总分 = W × 160 ∈ [160, 1600]，与分段表同量纲。
#
# 实测校验（2026-08-30，data/sample_salary.csv，150 条）：
#   ① mock_data 的 job_score 实际范围 187~1414，落在 [160, 1600] 内 ✓
#   ② 9 个职级的 job_score 均值反查 score_to_level() 与原职级一致率 **9/9** ✓
#   ③ 高层样例 W=8.875 → 总分 1420 → 判 M3（修复前恒判 P1）✓
JOB_SCORE_SCALE: float = 160.0


def weighted_to_total(w: float) -> float:
    """
    把 1-10 量纲的加权分 W 换算为百分制岗位评估总分。

    参数
    ----
    w : float
        各要素子维度（1-10 分）先取要素内平均、再按 JOB_EVAL_MODELS 的
        weights 加权求和得到的分数，值域 [1, 10]。

    返回
    ----
    float
        百分制岗位评估总分，值域 [160, 1600]，可直接喂给 score_to_level()。

    业务含义
    --------
    岗位评估的"分"本身没有绝对单位，只有**相对排序**有意义。换算系数 160
    只是把 1-10 的小数拉到 HR 熟悉的千分制刻度上，便于与职级分段表对齐、
    也便于向业务方解释（"这个岗 1420 分，属于 M3 量级"）。
    若公司已有自己的评估刻度，改这一个常数即可，不必动分段表。
    """
    return float(w) * JOB_SCORE_SCALE


def score_to_level(score: float, bands=None) -> str:
    """
    岗位评估总分 -> 建议职级归属。

    注意：入参 score 必须是**百分制总分**（即 weighted_to_total() 的输出），
    不能直接传 1-10 量纲的加权分 W，否则会恒判 P1。详见 JOB_SCORE_SCALE 注释。
    """
    bands = bands or JOB_SCORE_LEVEL_BANDS
    for lo, hi, lv in bands:
        if lo <= score < hi:
            return lv
    return bands[-1][2]


# =============================================================================
# 八、市场对标策略
# =============================================================================
# 分位值策略（Pay Positioning Strategy）：
#   P25 = 滞后市场（成本优先，常见于劳动密集型/可替代岗位）
#   P50 = 跟随市场（多数通用岗位的默认选择）
#   P75 = 领先市场（核心岗位/稀缺人才，用薪酬换留存与质量）
DEFAULT_MARKET_STRATEGY: Dict[str, str] = {
    "销售": "P75", "技术": "P75", "管理": "P50", "操作": "P25", "职能": "P50",
}
MARKET_FIELD_BY_KEY: Dict[str, str] = {"P25": "mkt_p25", "P50": "mkt_p50", "P75": "mkt_p75"}

# =============================================================================
# 八·补、会话 meta 分区键契约（单一真理源）
# =============================================================================
# 为什么要有这张表
# -----------------------------------------------------------------------------
# 各分析模块把结果写进 `session.meta[<分区键>]`，最后一个模块 `generate_report`
# 再从这些键里把数字取出来渲染报告。这是一条**跨 7 个模块的隐式契约**：
# 写入方改一个字母、读取方不跟着改，报告不会报错，只会静默地整章空掉
# —— 2026-08-30 就真实发生过：六个模块写 `diagnose/band/increase/jobeval/
# market/paymix`，而 report.py 读 `current_state/increase_sim/pay_mix/
# job_eval/market_benchmark`，五个键全对不上，于是七章报告里六章打印
# 「本章未执行（前置步骤缺失）」，而每个工具的单元测试全绿。
#
# 结论：**这类契约不能靠注释约束，必须落到常量 + 回归测试**。
#   · 写入方（band/diagnose/market/increase/paymix/jobeval）必须只写这里的键；
#   · 读取方（report.py）必须只经 `META_SECTION_KEYS` 取键，禁止再写字面量；
#   · `tests/test_meta_contract.py` 跑完整链路后逐个断言这些键非空，
#     并断言报告正文不出现「本章未执行」——一旦漂移就红灯。
#
# 键名规则：**分区键 = 模块名**，刻意与 DataFrame 列名、工具名都不重叠。
# 反例警示：早期 report.py 用的 `pay_mix` 同时也是数据表里的固浮比列名，
# `market_benchmark` 同时是工具名 —— 同名不同义是这类 bug 的温床。
META_SECTION_KEYS: Dict[str, str] = {
    #   分区键       ->  由哪个工具写入 / 含义
    "mapping": "confirm_mapping   —— 已固化的字段映射（其余 loader 产物如 shape / "
               "dirty_report 是平铺键，不是分区，故不入此表）",
    "band": "generate_band      —— 带宽表与相邻职级重叠度",
    "diagnose": "analyze_current_state —— CR / 渗透率 / 红绿圈",
    "market": "market_benchmark  —— 市场分位对标与差距",
    "increase": "simulate_increase —— 调薪测算与双情景",
    "paymix": "simulate_pay_mix  —— 固浮比拆分与曲线",
    "jobeval": "calc_job_score    —— 海氏/美世岗位评分",
}


def meta_key(section: str) -> str:
    """
    取 meta 分区键（带存在性校验）。

    写入方与读取方都走这个函数，拼错键名会在**调用现场**立刻抛 KeyError，
    而不是等到生成报告时才发现整章是空的。这是把「静默降级」改成「快速失败」。
    """
    if section not in META_SECTION_KEYS:
        raise KeyError(
            f"未知的 meta 分区键 {section!r}；合法键：{sorted(META_SECTION_KEYS)}"
        )
    return section


# =============================================================================
# 九、列归一化与语义匹配
# =============================================================================

_PUNCT = re.compile(r"[\s_\-/\\（）()\[\]【】〔〕:：,，.。、*#·'\"`~!！?？]+")


def normalize_col(col: str) -> str:
    """
    归一化列名：去掉空格/标点/括号内容，全角转半角，英文转小写。
    目的：'月度工资(元)' 与 '月度工资' 与 'monthly salary' 都能被识别为同一列。
    """
    s = str(col)
    # 先删括号内容（括号里通常是单位或备注，如 "(元)"、"（月薪）"）
    s = re.sub(r"[（(][^（()）]*[)）]", "", s)
    # 全角数字/字母转半角
    s = s.translate(str.maketrans("０１２３４５６７８９ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺａｂｃｄｅｆｇｈｉｊｋｌｍｎｏｐｑｒｓｔｕｖｗｘｙｚ",
                                  "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"))
    s = _PUNCT.sub("", s)
    return s.strip().lower()


def _score_pair(norm_col: str, alias: str) -> int:
    """
    给 (归一化列名, 别名) 打分，越高越可信：
      100 = 完全相等
       80 = 列名以别名开头/结尾且长度接近
       60 = 别名是列名的子串（如 '月薪' ⊂ '当前月薪'）
       40 = 列名是别名的子串（如 '绩效' ⊂ '绩效等级'）
        0 = 无关
    """
    if not norm_col or not alias:
        return 0
    if norm_col == alias:
        return 100
    if alias in norm_col:
        # 别名越长、占列名比例越高，越可信
        ratio = len(alias) / max(len(norm_col), 1)
        return 60 + int(20 * ratio)
    if norm_col in alias:
        return 40
    return 0


# -----------------------------------------------------------------------------
# 十、表头匹配引擎 v2：词根路由（确定性、纯 Python、可单测）
# -----------------------------------------------------------------------------
# 三个置信度档位（下游 main.py / loader.py 靠它们分流，改数值前先看调用方）
#   ROUTE  90-100 ：路由表精确命中，词根与维度都齐 → 程序可自行拍板
#   ALIAS  60     ：词根表未命中、旧别名词典兜底 → 有答案但**强制** ambiguous
#   ROOT   55     ：词根命中但维度不足（路由表标 need_disambiguation）→ 必须消歧
# 之所以让 ROOT(55) < ALIAS(60)：main.py 的自动映射阈值是 60，
# 「词根级歧义」必须落到阈值以下，才能真正拦住流程去问人，而不是被静默自动确认。
_CONF_ROUTE_BASE = 90
_CONF_ALIAS_FALLBACK = 60
_CONF_ROOT_AMBIGUOUS = 55


def _build_root_index() -> List[Tuple[str, str]]:
    """把 SEMANTIC_ROOTS 摊平成 [(归一化词根, 语义名)]，按长度降序（最长优先）。"""
    idx: List[Tuple[str, str]] = []
    for root, tokens in SEMANTIC_ROOTS.items():
        for tok in tokens:
            t = normalize_col(tok)
            if t:
                idx.append((t, root))
    # 长度降序 → 同长按词根名 + 词根字面量，保证扫描结果**与字典迭代顺序无关**（可复现）
    idx.sort(key=lambda p: (-len(p[0]), p[1], p[0]))
    return idx


def _build_modifier_index() -> List[Tuple[str, str, str]]:
    """把 DIMENSION_MODIFIERS 摊平成 [(归一化写法, 维度名, 维度值)]，长度降序。"""
    idx: List[Tuple[str, str, str]] = []
    for dim, mapping in DIMENSION_MODIFIERS.items():
        for tok, val in mapping.items():
            t = normalize_col(tok)
            if t:
                idx.append((t, dim, val))
    idx.sort(key=lambda p: (-len(p[0]), p[1], p[0]))
    return idx


_ROOT_INDEX: List[Tuple[str, str]] = _build_root_index()
_MODIFIER_INDEX: List[Tuple[str, str, str]] = _build_modifier_index()

_ASCII_ALPHA = re.compile(r"^[a-z]+$")


def _anchor_ok(token: str, norm: str, start: int, end: int) -> bool:
    """
    短英文词根的**边界护栏**。

    归一化会把空格下划线全部抹掉（'emp_id' → 'empid'），英文词根因此失去词边界。
    像 id / no / bu / tc / job 这类 ≤3 字母的短词根若允许出现在任意位置，
    会在无关列名里乱命中（'valid' 里有 'id'、'budget' 开头是 'bu'）。
    规则：**纯字母且长度 ≤3 的词根，必须贴在列名首或尾**。
      · 'empid' 尾部 'id'  ✓      · 'jobscore' 首部 'job' ✓
      · 'valid' 中部 'id'  ✗（被拦）
    含数字的写法（'p25' / '25'）不受此限 —— 分位数字天然出现在列名中段
    （'市场25分位'），加锚点反而会把 fixture 4 的市场侧全部拦死。
    """
    if len(token) <= 3 and _ASCII_ALPHA.match(token):
        return start == 0 or end == len(norm)
    return True


def _scan_with_consumption(norm: str, index: List[tuple]) -> Tuple[List[tuple], List[bool]]:
    """
    最长优先 + 占位消费的扫描器。

    "占位消费"是这个引擎的关键：'绩效等级' 里 '绩效等级'(4字) 先命中并吃掉全部 4 个
    字符，'绩效'(2) 与 '等级'(2) 因区间重叠被跳过 —— 于是词根集合是干净的
    {performance}，而不是冲突的 {performance, level}。少了它，所有复合列名都会
    命中两个词根、在路由表里同分打平，最终退化成歧义。

    返回 (命中列表, 占位表)；命中项为 index 元组 + (start, end)。
    """
    consumed = [False] * len(norm)
    hits: List[tuple] = []
    for entry in index:
        token = entry[0]
        if not token or len(token) > len(norm):
            continue
        start = 0
        while True:
            i = norm.find(token, start)
            if i < 0:
                break
            j = i + len(token)
            if _anchor_ok(token, norm, i, j) and not any(consumed[i:j]):
                for k in range(i, j):
                    consumed[k] = True
                hits.append(tuple(entry) + (i, j))
                start = j
            else:
                start = i + 1
    return hits, consumed


_PAREN_RE = re.compile(r"[（(]([^（()）]*)[)）]")
_YEAR_RE = re.compile(r"\d{4}")

# 结构性噪声列：读表工具或模板自动生成的**占位列名**，本身不含任何业务语义。
# 必须在扫词根**之前**短路掉 —— 否则会被词根子串误命中：
#   'Unnamed: 12' → 归一化 'unnamed12' → 里面的 'name' 命中 person 词根
#   → 实测被判成 name（置信度 94），把一整列垃圾灌进"姓名"字段。
# 这里只拦**纯占位/纯序号**这类零语义列名，不做任何业务语义猜测：
# 像 '备注' / '审批意见' 这类中文噪声列本来就命中不到词根，自然返回 None，
# 不需要维护一张"业务噪声词黑名单"（那会变成第二个别名词典）。
_NOISE_COL_RE = re.compile(
    r"^(?:unnamed\d*|未命名\d*|空列\d*|(?:col|column|field|var|x)\d+|\d+)$"
)


def _unit_modifiers(col_name: str) -> Dict[str, str]:
    """
    从**括号内的单位备注**里补捞维度修饰。

    `normalize_col()` 会整段删掉括号内容（把 "(元)" 当噪声），但中文薪酬表的
    时间维度恰恰常年藏在单位里：'基本工资(元/月)'、'年度总现金(元/年)'。
    删括号后只剩 '基本工资' → 被判"薪资类但缺时间维度"歧义，属误伤
    （messy_salary.csv 的 '基本工资(元/月)' 实测踩到）。
    故这里单独扫括号文本，**只补 norm 里缺失的维度**，不参与词根匹配、不计覆盖率。

    护栏：括号内含 4 位数字（'(2025年)'、'(2024年度)'）一律跳过 ——
    那是**数据期间标注**而非单位，采信会把 '薪资(2025年)' 误读成年薪。
    """
    out: Dict[str, str] = {}
    for raw in _PAREN_RE.findall(str(col_name)):
        if _YEAR_RE.search(raw):
            continue
        seg = normalize_col(raw)
        if not seg:
            continue
        hits, _ = _scan_with_consumption(seg, _MODIFIER_INDEX)
        for _tok, dim, val, _i, _j in hits:
            out.setdefault(dim, val)
    return out


def _alias_scores(norm: str) -> List[Tuple[int, str]]:
    """旧别名词典（v1）打分，降级 fallback 用。返回 [(score, field)] 已排序。"""
    scored: List[Tuple[int, str]] = []
    for field, aliases in FIELD_ALIASES.items():
        best = 0
        for alias in aliases:
            best = max(best, _score_pair(norm, normalize_col(alias)))
        if best > 0:
            scored.append((best, field))
    scored.sort(key=lambda x: (-x[0], CANONICAL_ORDER.index(x[1])))
    return scored


def _entry_matches(entry_roots: set, entry_mods: Dict[str, str],
                   roots: set, mods: Dict[str, str]) -> bool:
    """路由条目是否可匹配：词根子集 + 要求的每个维度值都被满足（见路由表注释）。"""
    if not entry_roots.issubset(roots):
        return False
    for dim, val in entry_mods.items():
        if mods.get(dim) != val:
            return False
    return True


def _mk_candidate(field: str, score: int, reason: str) -> Dict[str, object]:
    return {"field": field, "label": CANONICAL_FIELDS[field][0],
            "score": int(score), "match_reason": reason}


def _reason_text(roots: set, mods: Dict[str, str]) -> str:
    """人读的命中理由，如 '词根compensation+修饰time=monthly'。"""
    parts = []
    if roots:
        parts.append("词根" + "|".join(sorted(roots)))
    if mods:
        parts.append("修饰" + ",".join(f"{d}={v}" for d, v in sorted(mods.items())))
    return "+".join(parts) if parts else "无命中"


def parse_column_semantics(col_name: str) -> Dict[str, object]:
    """
    解析**单个原始列名**的语义，返回标准字段建议 + 置信度 + 歧义原因。

    这是表头匹配的唯一入口，纯 Python、无 pandas、无 LLM、无随机性 ——
    同一个列名任何时候都返回同一个结果，因此可以被 fixture 逐条钉死。

    算法五步（对齐需求 §1.3）
    ------------------------------------------------------------------
    1. `normalize_col()` 归一化（去标点/括号内容、全角转半角、英文小写）；
    2. 最长优先 + 占位消费扫描 SEMANTIC_ROOTS → 命中的语义词根集合；
    3. 同法扫描 DIMENSION_MODIFIERS → 命中的维度修饰字典；
       ⚠️ 词根与修饰各用**独立**占位表：'年包' 既是 compensation 词根、
          又是 time=annual 修饰，共用占位表会让它只能算一次而丢掉时间维度。
    4. 查 FIELD_ROUTING_TABLE，按 (词根数, 修饰数, 表序) 取最具体条目：
       · 精确条目          → 标准字段，confidence 90-100；
       · need_disambiguation → ambiguous=True + AMBIGUITY_MODIFIERS 具体原因；
       · 无任何条目命中     → 退回 FIELD_ALIASES 子串匹配（confidence=60，
                              **仍标 ambiguous**，因为 v1 词典本身会静默猜错）。
    5. 组装返回结构。

    返回
    ------------------------------------------------------------------
    {
      "suggest": "monthly_salary" | None,
      "confidence": 0-100,
      "label": "当前月薪" | None,
      "candidates": [{"field","label","score","match_reason"}, ...],
      "ambiguous": True/False,
      "ambiguous_reasons": ["薪资类字段但缺少月/年时间维度"],
      "matched_roots": ["compensation"],
      "matched_modifiers": {"time": "monthly"},
      "normalized": "月薪",
      "match_source": "routing" | "routing_ambiguous" | "alias_fallback" | "none",
    }
    """
    norm = normalize_col(col_name)

    # --- 步骤 1-补：结构性噪声列短路（'Unnamed: 12' / '序号3' / 空列名）--------
    # 放在扫描之前，因为占位列名里可能"恰好"藏着词根子串（unNAMEd → name）。
    if not norm or _NOISE_COL_RE.match(norm):
        return {
            "suggest": None, "confidence": 0, "label": None, "candidates": [],
            "ambiguous": False, "ambiguous_reasons": [],
            "matched_roots": [], "matched_modifiers": {},
            "normalized": norm, "match_source": "noise",
        }

    # --- 步骤 2/3：词根与修饰各自扫描（独立占位表，理由见 docstring）------------
    root_hits, root_consumed = _scan_with_consumption(norm, _ROOT_INDEX)
    mod_hits, mod_consumed = _scan_with_consumption(norm, _MODIFIER_INDEX)

    matched_roots = {h[1] for h in root_hits}
    matched_modifiers: Dict[str, str] = {}
    modifier_conflicts: List[str] = []
    for _tok, dim, val, _i, _j in mod_hits:
        if dim not in matched_modifiers:
            matched_modifiers[dim] = val
        elif matched_modifiers[dim] != val:
            # 同一维度出现互斥取值（如既有"下限"又有"上限"）→ 程序不猜
            modifier_conflicts.append(f"{dim}: {matched_modifiers[dim]} vs {val}")

    # 步骤 3-补：括号单位里的维度（'基本工资(元/月)' → time=monthly），只填缺失维度
    unit_mods = _unit_modifiers(col_name)
    for dim, val in unit_mods.items():
        matched_modifiers.setdefault(dim, val)

    # 覆盖率：词根+修饰共同解释了列名的多少字符，用来在 90-100 之间给精确命中打分
    covered = sum(1 for k in range(len(norm)) if root_consumed[k] or mod_consumed[k])
    coverage = (covered / len(norm)) if norm else 0.0

    # --- 步骤 4：路由 ---------------------------------------------------------
    matches: List[Tuple[Tuple[int, int, int], tuple]] = []
    for order, (e_roots, e_mods, field, need_disambig) in enumerate(FIELD_ROUTING_TABLE):
        if _entry_matches(set(e_roots), e_mods, matched_roots, matched_modifiers):
            # 排序键：词根数 desc、修饰数 desc、表序 asc（取负实现 desc）
            matches.append(((-len(e_roots), -len(e_mods), order),
                            (set(e_roots), e_mods, field, need_disambig)))
    matches.sort(key=lambda p: p[0])

    alias_scored = _alias_scores(norm)
    ambiguous_reasons: List[str] = []
    candidates: List[Dict[str, object]] = []

    def _append_alias_candidates(cap: int) -> None:
        """把 v1 词典的命中作为补充候选（分数封顶），供模型/用户复核。"""
        seen = {c["field"] for c in candidates}
        for s, f in alias_scored[:5]:
            if f not in seen:
                candidates.append(_mk_candidate(f, min(int(s), cap),
                                                "别名词典fallback"))
                seen.add(f)

    # 4-a 精确命中
    exact = next((m for m in matches if not m[1][3]), None)
    if exact is not None and not modifier_conflicts:
        _, (e_roots, e_mods, field, _nd) = exact
        confidence = min(100, _CONF_ROUTE_BASE + int(round(10 * coverage)))
        candidates.append(_mk_candidate(field, confidence,
                                        _reason_text(e_roots, e_mods)))
        # 其余可匹配的精确条目按名次递减给分，方便 LLM 看到"第二可能"
        score = 70
        for _key, (r2, m2, f2, nd2) in matches:
            if nd2 or f2 == field or any(c["field"] == f2 for c in candidates):
                continue
            candidates.append(_mk_candidate(f2, max(40, score), _reason_text(r2, m2)))
            score -= 5
        _append_alias_candidates(_CONF_ALIAS_FALLBACK)
        return {
            "suggest": field,
            "confidence": confidence,
            "label": CANONICAL_FIELDS[field][0],
            "candidates": candidates[:5],
            "ambiguous": False,
            "ambiguous_reasons": [],
            "matched_roots": sorted(matched_roots),
            "matched_modifiers": dict(matched_modifiers),
            "normalized": norm,
            "match_source": "routing",
        }

    # 4-b 命中"需要消歧"条目（词根在、维度不足）
    disambig = [m for m in matches if m[1][3]]
    if disambig or modifier_conflicts:
        for _key, (e_roots, _m, _f, _nd) in disambig:
            for r in sorted(e_roots):
                reason = AMBIGUITY_MODIFIERS.get(r)
                cand_key = r
                # 歧义原因必须**说实话**：'25分位' 已经带了 p25 数值，它缺的是
                # "市场/带宽"语义前缀。此时若套用 percentile 的默认原因
                # "缺少 P25/P50/P75 数值"，模型会被引导去补一个本就存在的东西。
                # 候选也同步收窄到该数值对应的字段（只给 mkt_p25，而非三个都列）。
                if r == "percentile" and "percentile_num" in matched_modifiers:
                    reason = AMBIGUITY_MODIFIERS.get("percentile_num", reason)
                    cand_key = "percentile_num:" + matched_modifiers["percentile_num"]
                if reason and reason not in ambiguous_reasons:
                    ambiguous_reasons.append(reason)
                for f in AMBIGUOUS_CANDIDATE_FIELDS.get(cand_key, []):
                    if not any(c["field"] == f for c in candidates):
                        candidates.append(_mk_candidate(
                            f, _CONF_ROOT_AMBIGUOUS,
                            f"词根{r}命中但维度不足，需消歧"))
        for conf_desc in modifier_conflicts:
            ambiguous_reasons.append(f"同一维度出现互斥修饰（{conf_desc}），无法判定")
        # 别名词典若给出确定答案，采信其**字段**作为 suggest（给模型一个起点），
        # 但分数一律封在 ROOT(55)、**不得抬到 ALIAS(60)** ——
        # 60 正好是 main.py 的自动映射阈值，抬上去就等于架空"词根级歧义必须问人"
        # 这条设计（曾写在置信度档位注释里却被本分支自己破坏，2026-09-01 修正）。
        if alias_scored:
            top_alias = alias_scored[0][1]
            existing = next((c for c in candidates if c["field"] == top_alias), None)
            if existing is None:
                candidates.insert(0, _mk_candidate(top_alias, _CONF_ROOT_AMBIGUOUS,
                                                   "别名词典fallback"))
            else:
                existing["score"] = _CONF_ROOT_AMBIGUOUS
                candidates.remove(existing)
                candidates.insert(0, existing)
        _append_alias_candidates(_CONF_ROOT_AMBIGUOUS)
        candidates.sort(key=lambda c: -int(c["score"]))
        suggest = str(candidates[0]["field"]) if candidates else None
        confidence = int(candidates[0]["score"]) if candidates else 0
        if not ambiguous_reasons:
            ambiguous_reasons.append("词根命中但维度不足，程序拒绝拍板")
        return {
            "suggest": suggest,
            "confidence": confidence,
            "label": CANONICAL_FIELDS[suggest][0] if suggest else None,
            "candidates": candidates[:5],
            "ambiguous": True,
            "ambiguous_reasons": ambiguous_reasons,
            "matched_roots": sorted(matched_roots),
            "matched_modifiers": dict(matched_modifiers),
            "normalized": norm,
            "match_source": "routing_ambiguous",
        }

    # 4-c 只有维度修饰、没有语义词根（"下限" / "上限" / "中位值" / "P50"）
    # 这是需求里点名的"下限"型列名：**有维度没语义**，程序拒绝猜带宽还是市场。
    # percentile_num 与 position 同理（列名就叫 "P50"），一并按对称逻辑处理。
    _bare_dims = [d for d in ("position", "percentile_num") if d in matched_modifiers]
    if not matched_roots and _bare_dims:
        for dim in _bare_dims:
            val = matched_modifiers[dim]
            reason = AMBIGUITY_MODIFIERS.get(dim)
            if reason and reason not in ambiguous_reasons:
                ambiguous_reasons.append(reason)
            for f in AMBIGUOUS_CANDIDATE_FIELDS.get(f"{dim}:{val}", []):
                if not any(c["field"] == f for c in candidates):
                    candidates.append(_mk_candidate(
                        f, _CONF_ROOT_AMBIGUOUS, f"仅命中修饰{dim}={val}，缺语义前缀"))
        # 同 4-b：只借词典的"字段"，不借它的分数（封在 55，低于自动确认阈值 60）
        if alias_scored:
            top_alias = alias_scored[0][1]
            existing = next((c for c in candidates if c["field"] == top_alias), None)
            if existing is not None:
                candidates.remove(existing)
                existing["score"] = _CONF_ROOT_AMBIGUOUS
                candidates.insert(0, existing)
            else:
                candidates.insert(0, _mk_candidate(top_alias, _CONF_ROOT_AMBIGUOUS,
                                                   "别名词典fallback"))
        candidates.sort(key=lambda c: -int(c["score"]))
        suggest = str(candidates[0]["field"]) if candidates else None
        return {
            "suggest": suggest,
            "confidence": int(candidates[0]["score"]) if candidates else 0,
            "label": CANONICAL_FIELDS[suggest][0] if suggest else None,
            "candidates": candidates[:5],
            "ambiguous": True,
            "ambiguous_reasons": ambiguous_reasons,
            "matched_roots": [],
            "matched_modifiers": dict(matched_modifiers),
            "normalized": norm,
            "match_source": "routing_ambiguous",
        }

    # 4-d 词根表完全未命中 → 退回 v1 别名词典（confidence=60 且强制 ambiguous）
    if alias_scored:
        _append_alias_candidates(_CONF_ALIAS_FALLBACK)
        suggest = str(candidates[0]["field"])
        return {
            "suggest": suggest,
            "confidence": _CONF_ALIAS_FALLBACK,
            "label": CANONICAL_FIELDS[suggest][0],
            "candidates": candidates[:5],
            "ambiguous": True,
            "ambiguous_reasons": [
                "词根路由表未命中，结果来自旧别名词典子串匹配，需人工/模型复核"],
            "matched_roots": sorted(matched_roots),
            "matched_modifiers": dict(matched_modifiers),
            "normalized": norm,
            "match_source": "alias_fallback",
        }

    # 4-e 彻底无关列（"备注" / "数据状态" / "—"）：明确返回 None，不猜
    return {
        "suggest": None,
        "confidence": 0,
        "label": None,
        "candidates": [],
        "ambiguous": False,
        "ambiguous_reasons": [],
        "matched_roots": sorted(matched_roots),
        "matched_modifiers": dict(matched_modifiers),
        "normalized": norm,
        "match_source": "none",
    }


def auto_suggest_mapping(columns: List[str]) -> Dict[str, dict]:
    """
    对输入的每一列，给出「最可能的标准字段 + 置信度 + 全部候选 + 歧义原因」。

    v2 起本函数只是 `parse_column_semantics()` 的**批量壳**（逐列调用后按列名装配），
    真正的匹配逻辑全在词根路由引擎里 —— 这样单列行为可以被 fixture 单独钉死，
    不必每次都造一张表。

    返回结构（**向后兼容**：原有 5 个键一个不动，新增 4 个键）：
        {
          "<原始列名>": {
             # ---- v1 既有键，语义不变，现有调用方无需改 ----
             "suggest": "monthly_salary" | None,
             "confidence": 0-100,
             "label": "当前月薪",
             "ambiguous": True/False,
             "candidates": [{"field","label","score","match_reason"}, ...],
             # ---- v2 新增键 ----
             "ambiguous_reasons": ["薪资类字段但缺少月/年时间维度"],
             "matched_roots": ["compensation"],
             "matched_modifiers": {"time": "monthly"},
             "match_source": "routing" | "routing_ambiguous" | "alias_fallback" | "none",
          }, ...
        }

    分工不变：机器给候选与歧义原因，模型/用户只在 ambiguous=True 的列上拍板 ——
    非 ambiguous 的列**模型不得覆盖**（契约写在 registry.confirm_mapping 描述里）。
    """
    result: Dict[str, dict] = {}
    for col in columns:
        info = parse_column_semantics(col)
        result[col] = {
            "suggest": info["suggest"],
            "confidence": info["confidence"],
            "label": info["label"],
            "ambiguous": info["ambiguous"],
            "candidates": info["candidates"],
            "ambiguous_reasons": info["ambiguous_reasons"],
            "matched_roots": info["matched_roots"],
            "matched_modifiers": info["matched_modifiers"],
            "match_source": info["match_source"],
        }
    return result
