#!/usr/bin/env python3
"""check_repo_hygiene.py — 仓库卫生门禁（v6.0 T-69，stdlib only）。

检查 `git ls-files` 的输出：任何被跟踪的运行产物（缓存库 / 盐 / pid / 日志 /
.env / __pycache__）都直接 exit 1，并打印应执行的 `git rm --cached` 命令。

背景（plans/下一步改进指南.md T-69）：2026-11-03 之前 plans/证据/soak-run/
跟踪过 7 个运行产物；它们既可再生，又让一次 `git add -A` 有带走 `.env`
（含 CF_SOLVE_TOKEN）的风险。本脚本是防再犯的闸门，接进 run.ps1 verify 与 CI。

规则（与 plans/证据/README.md「三条硬规则」对齐）：
  · *.sqlite3 / *.sqlite3-shm / *.sqlite3-wal —— 缓存库，可再生
  · salt.bin —— PBKDF2 盐，可再生
  · pid.txt —— 进程号，运行态
  · *.log —— 运行日志，可再生（git ls-files 本来就不会列未跟踪文件）
  · .env —— 凭证文件，绝不入库
  · __pycache__ —— 字节码缓存，可再生

用法：python scripts/check_repo_hygiene.py        # exit 0 = 干净 / 1 = 有命中
"""

from __future__ import annotations

import re
import subprocess
import sys

# 命中即违规的跟踪路径模式（按顺序匹配，命中即报）。
PATTERNS = (
    re.compile(r"(^|/)\.env$"),            # 凭证文件（.env 在任何层级）
    re.compile(r"\.sqlite3($|-shm$|-wal$)"),  # 缓存库及其附属
    re.compile(r"(^|/)salt\.bin$"),        # PBKDF2 盐
    re.compile(r"(^|/)pid\.txt$"),         # 进程号
    re.compile(r"\.log$"),                 # 运行日志
    re.compile(r"(^|/)__pycache__/"),      # 字节码缓存
)


def tracked_files() -> list[str]:
    """git ls-files -z → 路径列表。不在 git 仓库里时退出码 2。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout
    except FileNotFoundError:
        print("FATAL: 找不到 git 可执行文件", file=sys.stderr)
        raise SystemExit(2)
    except subprocess.CalledProcessError as e:
        print(f"FATAL: git ls-files 失败：{e.stderr.decode(errors='replace').strip()}",
              file=sys.stderr)
        raise SystemExit(2)
    return [p for p in out.decode("utf-8", errors="replace").split("\0") if p]


def main() -> int:
    files = tracked_files()
    hits: list[str] = []
    for f in files:
        if any(p.search(f) for p in PATTERNS):
            hits.append(f)

    if not hits:
        print(f"[OK] 仓库干净：{len(files)} 个跟踪文件中无运行产物")
        return 0

    print(f"FAIL：{len(hits)} 个被跟踪的运行产物（应为可再生/运行态文件，不该入库）：\n")
    for f in hits:
        print(f"  {f}")
    print("\n修复（--cached 只解除跟踪、保留工作区文件）：")
    print("  git rm --cached " + " ".join(f'"{f}"' for f in hits))
    print("\n并在提交信息里注明出库原因（参见 plans/证据/README.md 三条硬规则）。")
    return 1


if __name__ == "__main__":
    # Windows 控制台默认 GBK：中文提示会乱码。
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
