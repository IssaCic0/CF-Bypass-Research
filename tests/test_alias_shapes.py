"""T-71 Python 侧验收：计算属性形态签名与静态可求值实测（离线）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ANALYZE = Path(__file__).resolve().parents[1] / "tools" / "cfsolve-patches" / "analyze"
if str(ANALYZE) not in sys.path:
    sys.path.insert(0, str(ANALYZE))

import alias_shapes as a  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def test_alias_shape_normalization_is_stable():
    assert a.normalize_expr("obj") == "IDENT"
    assert a.normalize_expr("helper.key") == a.normalize_expr("foo.bar") == "MEMBER(IDENT,IDENT)"
    assert a.normalize_expr("getKey()") == "CALL(IDENT,[])"
    assert a.normalize_expr("getKey(1, x)") == "CALL(IDENT,[LIT,EXPR])"
    assert a.normalize_expr("a + b") == "BINOP(+)"
    assert a.normalize_expr("function(){return 1}") == "FUNC(...)"


def test_alias_rule_regression_on_synthetic():
    src = (
        'var helper = { key: "k" };\n'
        "var v1 = obj[helper.key];\n"
        "var v2 = obj[getKey()];\n"
    )
    report = a.analyze_source(src, "synthetic.js")
    static_sig = a.normalize_expr("helper.key")
    dynamic_sig = a.normalize_expr("getKey()")
    assert report[static_sig]["static_evaluable"] is True
    assert report[static_sig]["dynamic_instances"] == 0
    assert report[dynamic_sig]["static_evaluable"] is False
    assert report[dynamic_sig]["dynamic_instances"] == 1


def test_alias_shapes_on_corpus_has_no_unknown_static():
    corpus = REPO / "corpus"
    samples = sorted(p / "iframe.deobfuscated.js" for p in corpus.iterdir()
                     if (p / "iframe.deobfuscated.js").exists()) if corpus.is_dir() else []
    if not samples:
        pytest.skip("corpus empty: no real session snapshot; 见 plans/下一步改进指南.md §6")
    for js in samples:
        report = a.analyze_source(js.read_text(encoding="utf-8", errors="replace"), js.name)
        for sig, info in report.items():
            if info["static_evaluable"]:
                assert info["dynamic_instances"] == 0, f"{js.name} {sig} 被判静态但实测有动态"
