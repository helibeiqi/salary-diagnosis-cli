# -*- coding: utf-8 -*-
"""
ci_security_gate.py — 安全闸门（Phase 5，自主优化架构师）
================================================================================

把「绝不泄露真实薪资」从"靠纪律"升级为"靠流水线硬失败"。

两种违规直接 fail（退出码 1）：
--------------------------------------------------------------------------------
1) **敏感路径入库**：PR 试图新增
     - ``src/data/**``              薪酬明细导出目录（含行级月薪/新月薪等）
     - ``assets/**.html``           图表（内嵌真实薪资数值）
     - ``report/**.html``           报告（内嵌真实薪资数值）
   这些本就被 .gitignore 挡住；本闸门是防御纵深，专门抓 ``git add -f`` 或
   .gitignore 被改坏的绕过情形。

2) **核心引入网络 import**：在 ``src/**`` 下新增/修改的 ``.py`` 文件中出现
   真实的远程 IO 模块（socket / urllib / requests / httpx / aiohttp / http /
   ftplib / telnetlib / smtplib / websocket(s) / paramiko / pysftp / grpc /
   zeep / xmlrpc）。
   ★ 例外：``subprocess`` 不在黑名单内——PTC 沙箱（sandbox.py / sandbox_worker.py）
     用它**本地**拉起子进程，不是远程网络，属合法。
   设计铁律：核心 Python **零网络调用**（已确认），任何新网络 import 都是回归。

用法
--------------------------------------------------------------------------------
    # 由 CI 显式传差异文件列表（推荐，最准）
    python ci_security_gate.py file1 file2 ...

    # 本地自测：自动用 git 收集（HEAD 差异 ∪ 未跟踪且未被 ignore 的文件）
    python ci_security_gate.py

退出码：0 = 干净；1 = 命中违规（CI 应据此 fail）。
"""
from __future__ import annotations

import os
import re
import sys

# 真实远程 IO 模块（命中即违规）。注意：subprocess / os / ssl / asyncio 不在内。
NETWORK_MODULES = {
    "socket", "urllib", "urllib3", "requests", "httpx", "aiohttp", "http",
    "ftplib", "telnetlib", "smtplib", "websocket", "websockets", "paramiko",
    "pysftp", "grpc", "zeep", "xmlrpc", "rpc",
}

# 敏感路径（新增即违规）
_FORBIDDEN_PREFIXES = ("src/data/",)            # 薪酬明细导出（行级薪资）
_FORBIDDEN_HTML_DIRS = ("assets/", "report/")   # 图表/报告 HTML（含真实薪资）

# import 行匹配（允许 `import x` / `import x.y` / `from x import ...`）
_IMPORT_RE = re.compile(
    r'^\s*(?:import\s+([A-Za-z_][\w.]*)|from\s+([A-Za-z_][\w.]*)\s+import\b)')


def _normalize(p: str) -> str:
    return p.replace("\\", "/")


def collect_files(explicit) -> list:
    """收集待检文件：优先用显式列表，否则用 git（HEAD 差异 ∪ 未跟踪）。"""
    if explicit:
        return list(explicit)
    files: list = []
    try:
        import subprocess
        # 1) 已跟踪文件的改动（A/C/M/R）
        out = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=ACMR", "HEAD"],
            capture_output=True, text=True, check=False)
        files += [l.strip() for l in out.stdout.splitlines() if l.strip()]
        # 2) 未跟踪且未被 .gitignore 排除的文件（抓 `git add -f` 风险）
        out2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=False)
        files += [l.strip() for l in out2.stdout.splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        pass
    # 去重保序
    seen = set()
    uniq = []
    for f in files:
        nf = _normalize(f)
        if nf not in seen:
            seen.add(nf)
            uniq.append(nf)
    return uniq


def scan_paths(files: list) -> list:
    """返回违规路径清单。"""
    violations = []
    for f in files:
        nf = _normalize(f)
        for pre in _FORBIDDEN_PREFIXES:
            if nf.startswith(pre):
                violations.append(
                    (nf, f"敏感目录新增：{pre} 含行级薪资明细，禁止入库"))
                break
        else:
            for d in _FORBIDDEN_HTML_DIRS:
                if nf.startswith(d) and nf.endswith(".html"):
                    violations.append(
                        (nf, f"敏感产物新增：{d}*.html 内嵌真实薪资，禁止入库"))
                    break
    return violations


def scan_network_imports(files: list) -> list:
    """扫描 src/** 下 .py 文件的新增网络 import。返回 (文件, 行号, 模块) 清单。"""
    hits = []
    for f in files:
        nf = _normalize(f)
        if not nf.startswith("src/") or not nf.endswith(".py"):
            continue
        if not os.path.exists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="ignore") as fh:
                for i, line in enumerate(fh.readlines(), 1):
                    m = _IMPORT_RE.match(line)
                    if not m:
                        continue
                    mod = (m.group(1) or m.group(2) or "").split(".")[0]
                    if mod in NETWORK_MODULES:
                        hits.append((nf, i, mod))
        except Exception:  # noqa: BLE001
            pass
    return hits


def main(argv=None) -> int:
    explicit = list(argv if argv is not None else sys.argv[1:])
    files = collect_files(explicit)

    print("=" * 72)
    print("CI 安全闸门 — 敏感路径 / 网络 import 扫描")
    print("=" * 72)
    print(f"待检文件数：{len(files)}")
    if not files:
        print("（无可检文件，视为干净）")
        return 0

    path_v = scan_paths(files)
    net_v = scan_network_imports(files)

    ok = True
    if path_v:
        ok = False
        print("\n[FAIL] 敏感路径入库：")
        for f, reason in path_v:
            print(f"  - {f}\n      {reason}")
    if net_v:
        ok = False
        print("\n[FAIL] 核心引入网络 import（违反零网络铁律）：")
        for f, ln, mod in net_v:
            print(f"  - {f}:{ln}  import {mod}")

    if ok:
        print("\n[PASS] 未检出敏感路径入库或网络 import。")
        print("=" * 72)
        return 0

    print("\n[RESULT] 安全闸门拦截：请移除上述文件/网络 import 后再提交。")
    print("=" * 72)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
