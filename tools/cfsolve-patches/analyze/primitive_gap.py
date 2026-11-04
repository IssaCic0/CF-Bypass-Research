"""primitive_gap.py — VM 内建原语缺口「自动发现」（T-70，Python 侧，stdlib only）。

做法（**字面量级**扫描，不做完整 JS 解析——CF 每版混淆不同，重解析器本身是负债）：
  1. 从 `iframe.deobfuscated.js` 收集形如 `X.prototype.Y`、`Y.call(`、
     `JSON.Z(`、`Math.Z(`、`String.Z(`、`Array.Z(`、以及裸 `recv.method(` 的标识符；
  2. VM 已实现的清单从 `D:\\cfsolve-upstream\\src\\vm\\` 的 `builtins!` 注册表
     **自动正则提取**（不手抄；目录缺失直接抛 `RegistryError`，绝不静默空集）；
  3. 求差集，输出 `{缺失原语, 出现次数, 首次出现 file:line, 上下文 8 行}`；同输入两次
     输出逐字节一致（无时间戳、排序稳定）。

**已知局限（必须写进 README，不许吹成全自动）**：字面扫描看不到「运行时拼出方法名」的
调用（新版 CF 的 computed-key 手法）——它消除的是 80% 的常见缺失，不是全自动适配。
注释与字符串里的属性名不算调用（防误报）。

VM 注册表口径（`静态确认`，src/vm/interpret/builtin.rs）：
  - `builtins! { Ident => "path", ... }` → `BUILTINS`，**这才是真正实现的清单**；
  - `STRING_MEMBERS`(access.rs) 是「识别但显式 unsupported」的名单，
    `CONSTRUCTORS`/`STATIC_FUNCTIONS` 是「可解析但常返回 Symbolic」的名单，
    两者都**不算实现**，故不纳入 implemented（否则会把真缺口算成已实现）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_VM_DIR = Path(r"D:\cfsolve-upstream\src\vm")
ENV_VM_DIR = "CFSOLVE_VM_DIR"

# 裸 `recv.method(` 的 receiver 若是这些全局命名空间，属静态调用而非实例方法
NAMESPACES = frozenset(
    "JSON Math String Array Object Number Reflect BigInt Symbol Date RegExp Error "
    "Set Map Promise Int32Array Uint8Array".split()
)
# Function.prototype 上的方法：`fn.call(...)` 等价于 `Function.prototype.call`
FUNCTION_PROTOCOL = ("call", "apply", "bind")

_BUILTINS_BLOCK_RE = re.compile(r"builtins!\s*\{(.*?)\}", re.S)
_BUILTINS_ENTRY_RE = re.compile(r'([A-Za-z_][\w]*)\s*=>\s*"([^"]+)"\s*,')
_PROTO_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\.\s*prototype\s*\.\s*([A-Za-z_$][\w$]*)")
_NAMESPACE_CALL_RE = re.compile(
    r"\b(" + "|".join(sorted(NAMESPACES, key=len, reverse=True)) + r")\s*\.\s*"
    r"([A-Za-z_$][\w$]*)\s*\("
)
_FN_PROTOCOL_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\s*\.\s*(" + "|".join(FUNCTION_PROTOCOL) + r")\s*\("
)
_BARE_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\.\s*([A-Za-z_$][\w$]*)\s*\(")

CONTEXT_LINES = 8


class RegistryError(Exception):
    """VM 注册表不可得（目录缺失 / 正则提取到 0 条）。"""


# ---------------------------------------------------------------- 掩码（去注释/去字符串）


def mask_source(text: str, mask_strings: bool = True) -> str:
    """把注释（以及可选的字符串内容）替换成空格，**保持长度与换行**。

    这样任何索引都能同时映射回原文，行号也不会漂移。
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] not in "\r\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] not in "\r\n":
                    out[i] = " "
                i += 1
            for _ in range(2):  # 吃掉 "*/"
                if i < n:
                    out[i] = " "
                    i += 1
        elif c in ("'", '"') and mask_strings:
            out[i] = " "
            i += 1
            while i < n:
                if text[i] == "\\":
                    out[i] = " "
                    if i + 1 < n:
                        out[i + 1] = " "
                    i += 2
                    continue
                if text[i] == c:
                    out[i] = " "
                    i += 1
                    break
                if text[i] in "\r\n":  # 未闭合：不跨行吞
                    break
                out[i] = " "
                i += 1
        else:
            i += 1
    return "".join(out)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _context(text: str, line: int) -> list[str]:
    """匹配行附近 8 行上下文（前后各 3 行起，越界就少给，不补假行）。"""
    lines = text.splitlines()
    start = max(1, line - 3)
    return lines[start - 1:start - 1 + CONTEXT_LINES]


# ---------------------------------------------------------------- VM 注册表


def resolve_vm_dir(explicit=None) -> Path:
    raw = explicit or os.environ.get(ENV_VM_DIR) or str(DEFAULT_VM_DIR)
    path = Path(raw)
    if not path.is_dir():
        raise RegistryError(
            f"VM 源码目录不存在: {path}\n"
            f"修复: git clone https://github.com/chris124567/cfsolve D:\\cfsolve-upstream"
            f" 或 set {ENV_VM_DIR}=<path>"
        )
    return path


def extract_registry(vm_dir=None) -> dict:
    """从 Rust 源码的 `builtins!` 注册表自动提取已实现原语（路径字符串）。"""
    root = resolve_vm_dir(vm_dir)
    sources: dict[str, int] = {}
    paths: list[str] = []
    for rs in sorted(root.rglob("*.rs")):
        try:
            text = rs.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "builtins!" not in text:
            continue
        count = 0
        for block in _BUILTINS_BLOCK_RE.finditer(text):
            for entry in _BUILTINS_ENTRY_RE.finditer(block.group(1)):
                paths.append(entry.group(2))
                count += 1
        if count:
            sources[str(rs.relative_to(root)).replace("\\", "/")] = count
    unique = sorted(set(paths))
    if not unique:
        raise RegistryError(
            f"从 {root} 未提取到任何 builtins! 条目：注册表格式可能已变，拒绝输出空集"
        )
    return {
        "vm_dir": str(root.resolve()),
        "implemented_count": len(unique),
        "implemented": unique,
        "sources": sources,
    }


def implemented_prototype_methods(implemented: list[str]) -> dict[str, list[str]]:
    """`*.prototype.method` 口径：方法名 → 已实现的完整路径列表。"""
    table: dict[str, list[str]] = {}
    for path in implemented:
        _, sep, tail = path.partition(".prototype.")
        if sep and tail and "." not in tail:
            table.setdefault(tail, []).append(path)
    for name in table:
        table[name].sort()
    return table


# ---------------------------------------------------------------- 扫描


def scan_primitives(text: str) -> list[dict]:
    """收集原语调用，返回 [{primitive, index, line}]，按出现位置排序、无重叠。"""
    masked = mask_source(text)
    spans: list[tuple[int, int, str]] = []

    def _covered(start: int, end: int) -> bool:
        return any(s <= start and end <= e for s, e, _ in spans)

    def _collect(pattern, build):
        for m in pattern.finditer(masked):
            if _covered(m.start(), m.end()):
                continue
            spans.append((m.start(), m.end(), build(m)))

    _collect(_PROTO_RE, lambda m: f"{m.group(1)}.prototype.{m.group(2)}")
    _collect(_NAMESPACE_CALL_RE, lambda m: f"{m.group(1)}.{m.group(2)}")
    _collect(_FN_PROTOCOL_RE, lambda m: f"Function.prototype.{m.group(2)}")

    def _bare(m):
        receiver, method = m.group(1), m.group(2)
        if receiver in NAMESPACES or receiver == "prototype":
            return None
        if method in FUNCTION_PROTOCOL:
            return None
        return f"*.prototype.{method}"

    for m in _BARE_CALL_RE.finditer(masked):
        if _covered(m.start(), m.end()):
            continue
        built = _bare(m)
        if built:
            spans.append((m.start(), m.end(), built))

    occurrences = sorted(
        ({"primitive": p, "index": s, "line": _line_of(text, s)} for s, _e, p in spans),
        key=lambda o: (o["index"], o["primitive"]),
    )
    return occurrences


def find_gaps(js_path, vm_dir=None) -> dict:
    """扫描 deobf 脚本 + 提取 VM 注册表 → 未实现原语清单（确定性输出）。"""
    path = Path(js_path)
    if not path.is_file():
        raise FileNotFoundError(f"deobf 脚本不存在: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    registry = extract_registry(vm_dir)
    implemented = registry["implemented"]
    implemented_set = set(implemented)
    proto_methods = implemented_prototype_methods(implemented)

    grouped: dict[str, list[dict]] = {}
    for occ in scan_primitives(text):
        grouped.setdefault(occ["primitive"], []).append(occ)

    gaps = []
    covered = 0
    for primitive in sorted(grouped):
        hits = grouped[primitive]
        if primitive in implemented_set:
            covered += 1
            continue
        if primitive.startswith("*."):
            method = primitive.rsplit(".", 1)[-1]  # `*.prototype.slice` → `slice`
            if method in proto_methods:
                covered += 1
                continue
            candidates = list(proto_methods.get(method, []))
        else:
            candidates = [p for p in implemented if p.endswith("." + primitive.split(".")[-1])]
            candidates = sorted(set(candidates))[:5]
        first = hits[0]
        line = first["line"]
        gaps.append({
            "primitive": primitive,
            "occurrences": len(hits),
            "first": {
                "file": path.name,
                "line": line,
                "context": _context(text, line),
            },
            "implemented_candidates": candidates,
        })

    return {
        "file": path.name,
        "vm_dir": registry["vm_dir"],
        "implemented_count": registry["implemented_count"],
        "implemented": implemented,
        "sources": registry["sources"],
        "call_sites": sum(len(v) for v in grouped.values()),
        "covered_primitives": covered,
        "gaps": gaps,
    }


def format_table(report: dict) -> str:
    lines = [
        f"文件: {report['file']}   VM: {report['vm_dir']}",
        f"已实现原语: {report['implemented_count']}   调用点: {report['call_sites']}   "
        f"缺口: {len(report['gaps'])}",
    ]
    if not report["gaps"]:
        lines.append("无缺口：样本调用的原语都在 VM 实现清单内。")
        return "\n".join(lines)
    lines.append(f"{'缺失原语':<32}{'次数':>4}  首次出现")
    for gap in report["gaps"]:
        first = gap["first"]
        lines.append(
            f"{gap['primitive']:<32}{gap['occurrences']:>4}  {first['file']}:{first['line']}"
        )
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="VM 内建原语缺口扫描（T-70）")
    ap.add_argument("js", help="iframe.deobfuscated.js 路径")
    ap.add_argument("--vm-dir", default=None, help=f"VM 源码目录（默认 {DEFAULT_VM_DIR}）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", default=None, help="JSON 落盘路径")
    args = ap.parse_args(argv)
    report = find_gaps(args.js, args.vm_dir)
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8", newline="\n")
    print(payload if args.json else format_table(report))
    return 0 if not report["gaps"] else 1


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
