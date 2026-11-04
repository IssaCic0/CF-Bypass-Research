"""T-70 验收：VM 内建原语缺口扫描器（离线，全部合成输入）。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ANALYZE = Path(__file__).resolve().parents[1] / "tools" / "cfsolve-patches" / "analyze"
if str(ANALYZE) not in sys.path:
    sys.path.insert(0, str(ANALYZE))

import primitive_gap as pg  # noqa: E402


def write_fake_vm(root: Path, paths: list[str]) -> Path:
    """造一个只有 builtins! 注册表的最小 Rust VM 目录。"""
    vm = root / "vm"
    vm.mkdir()
    entries = "\n".join(f'    B{i} => "{p}",' for i, p in enumerate(paths))
    (vm / "builtin.rs").write_text(
        "//! fake vm registry\n"
        "macro_rules! builtins {}\n"
        "builtins! {\n"
        f"{entries}\n"
        "}\n",
        encoding="utf-8",
    )
    return vm


def test_vm_registry_extraction_counts_implemented_primitives(tmp_path):
    paths = [
        "Array.prototype.join",
        "Array.prototype.slice",
        "String.prototype.slice",
        "String.fromCharCode",
        "Math.pow",
    ]
    vm = write_fake_vm(tmp_path, paths)
    registry = pg.extract_registry(vm)
    # 独立正则计数（防手抄漂移）：`=> "..."` 条目数
    raw = (vm / "builtin.rs").read_text(encoding="utf-8")
    independent = len(re.findall(r'=>\s*"[^"]+"\s*,', raw))
    assert registry["implemented_count"] == len(paths) == independent
    assert sorted(registry["implemented"]) == sorted(paths)


def test_vm_registry_missing_dir_fails_loudly(tmp_path):
    with pytest.raises(pg.RegistryError):
        pg.extract_registry(tmp_path / "no-such-vm")


def test_gap_scanner_finds_known_missing_primitive(tmp_path):
    js = tmp_path / "iframe.deobfuscated.js"
    lines = ["function f(s) {"]
    lines += [f"  var pad{i} = {i};" for i in range(4)]
    lines.append("  return s.slice(2, 9);")  # 第 6 行
    lines += [f"  var tail{i} = {i};" for i in range(5)]
    lines.append("}")
    js.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # 假注册表里没有 String.prototype.slice（也没有其它 prototype 实现 slice）
    vm = write_fake_vm(tmp_path, ["Array.prototype.join", "String.fromCharCode"])
    report = pg.find_gaps(js, vm)
    prims = [g["primitive"] for g in report["gaps"]]
    assert any(p.endswith(".slice") for p in prims), report
    gap = [g for g in report["gaps"] if g["primitive"].endswith(".slice")][0]
    assert gap["occurrences"] == 1
    assert gap["first"]["line"] == 6
    assert gap["first"]["file"] == js.name
    assert len(gap["first"]["context"]) == pg.CONTEXT_LINES
    assert "s.slice(2, 9)" in "\n".join(gap["first"]["context"])


def test_gap_scanner_ignores_property_names_in_comments(tmp_path):
    js = tmp_path / "iframe.deobfuscated.js"
    js.write_text(
        "// s.slice(2, 9)\n"
        "/* JSON.parse(x); Math.pow(2, 3); */\n"
        'var s = "s.slice(1) JSON.parse(y)";\n',
        encoding="utf-8",
    )
    vm = write_fake_vm(tmp_path, ["Array.prototype.join"])
    report = pg.find_gaps(js, vm)
    assert report["gaps"] == []


def test_gap_scanner_is_deterministic(tmp_path):
    js = tmp_path / "iframe.deobfuscated.js"
    js.write_text(
        "var a = JSON.parse(x);\n"
        "var b = s.slice(1, 2);\n"
        "String.fromCharCode(65);\n"
        "fn.call(null, 1);\n",
        encoding="utf-8",
    )
    vm = write_fake_vm(tmp_path, ["String.fromCharCode", "Function.prototype.call"])
    r1 = pg.find_gaps(js, vm)
    r2 = pg.find_gaps(js, vm)
    b1 = json.dumps(r1, ensure_ascii=False, sort_keys=True).encode()
    b2 = json.dumps(r2, ensure_ascii=False, sort_keys=True).encode()
    assert hashlib.sha256(b1).hexdigest() == hashlib.sha256(b2).hexdigest()
