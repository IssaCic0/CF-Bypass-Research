"""T-75 验收：快照差分 Markdown 五段报告（离线、合成快照）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import research  # noqa: E402

SECTIONS = ("元信息", "AST 形态变更", "字段名集合变更", "断言矩阵变更", "建议动作")


def make_snapshot(root: Path, name: str, js: str, host: str = "example.com",
                  stage2: bool = True) -> Path:
    """造一份合成快照。默认带 `response.decoded.txt`（即"走到了 stage2"，
    stage2 结构断言才可判定；不带时它们按判据应为 skip 而非 fail）。"""
    d = root / name
    d.mkdir()
    (d / "iframe.deobfuscated.js").write_text(js, encoding="utf-8")
    if stage2:
        (d / "response.decoded.txt").write_text("probe response body", encoding="utf-8")
    (d / "manifest.json").write_text(
        json.dumps({"captured_utc": "2026-11-01T00:00:00Z", "target": {"host": host}}),
        encoding="utf-8",
    )
    return d


BASE_JS = (
    "var dispatcher = 1; var collection = 2; var probe = 3;\n"
    "var counter = 4; var token = 5; var input = 6;\n"
    'var helper = { key: "k" };\n'
    "var v = obj[helper.key];\n"
    'var alphaField = "alphaField";\n'
)


def test_diff_marks_stage2_assertions_skip_when_sample_never_reached_stage2(tmp_path):
    """样本没走到 stage2 时，差分报告的 stage2 断言必须是 skip，不是 fail。

    复审抓到的真缺陷：`_assertion_states` 曾经对"没有 stage2 程序"直接判 fail，
    于是报告会建议"为 dispatcher 新增补丁"——而那份样本压根没到 stage2。
    """
    a_dir = make_snapshot(tmp_path, "a", BASE_JS, stage2=False)
    b_dir = make_snapshot(tmp_path, "b", BASE_JS.replace("counter", "cnt_renamed"), stage2=False)
    rep = research.build_diff_report(a_dir, b_dir, generated_utc="2026-11-04T00:00:00Z")
    states = rep["assertions"]
    for name in research.STAGE2_ASSERTIONS:
        assert states["a"][name] == "skip", f"{name}: A={states['a'][name]}"
        assert states["b"][name] == "skip", f"{name}: B={states['b'][name]}"
    md = rep["markdown"]
    assert "fail→fail" not in md, "不可判定的断言不得报成 fail"


def test_diff_suggestion_never_says_add_patch_for_unjudgeable(tmp_path):
    """混合场景（A 走到 stage2 / B 没走到）：建议动作里不得出现「新增补丁」。"""
    a_dir = make_snapshot(tmp_path, "a", BASE_JS, stage2=True)
    b_dir = make_snapshot(tmp_path, "b", BASE_JS, stage2=False)
    rep = research.build_diff_report(a_dir, b_dir, generated_utc="2026-11-04T00:00:00Z")
    md = rep["markdown"]
    section5 = md.split("## ")[-1]
    assert "需新增补丁" not in section5, section5
    assert "不可判定" in section5


def test_diff_report_has_five_sections(tmp_path):
    a_dir = make_snapshot(tmp_path, "a", BASE_JS)
    b_dir = make_snapshot(tmp_path, "b", BASE_JS)
    rep = research.build_diff_report(a_dir, b_dir, generated_utc="2026-11-04T00:00:00Z")
    md = rep["markdown"]
    for sec in SECTIONS:
        assert sec in md
    assert md.count("## ") == 5


def test_diff_detects_field_name_set_change(tmp_path):
    b_js = BASE_JS.replace('var alphaField = "alphaField";',
                           'var betaField = "betaField";')
    a_dir = make_snapshot(tmp_path, "a", BASE_JS)
    b_dir = make_snapshot(tmp_path, "b", b_js)
    md = research.build_diff_report(a_dir, b_dir)["markdown"]
    field_sec = md.split("## 3.", 1)[1].split("## 4.", 1)[0]
    assert "alphaField" in field_sec and "betaField" in field_sec


def test_diff_suggestion_maps_assertion_to_patch(tmp_path):
    b_js = BASE_JS.replace("var counter = 4; ", "")
    a_dir = make_snapshot(tmp_path, "a", BASE_JS)
    b_dir = make_snapshot(tmp_path, "b", b_js)
    md = research.build_diff_report(a_dir, b_dir)["markdown"]
    suggestion = md.split("## 5.", 1)[1]
    assert "counter" in suggestion
    assert "newcf-computed-key-20261103" in suggestion


def test_diff_is_stable(tmp_path):
    a_dir = make_snapshot(tmp_path, "a", BASE_JS)
    b_dir = make_snapshot(tmp_path, "b", BASE_JS.replace("var counter = 4; ", ""))
    m1 = research.build_diff_report(a_dir, b_dir)["markdown"]
    m2 = research.build_diff_report(a_dir, b_dir)["markdown"]
    def strip(s):
        return re.sub(r"生成时间\(UTC\): .*", "生成时间(UTC): X", s)
    assert strip(m1) == strip(m2)


def test_diff_report_has_no_long_script_fragment(tmp_path):
    long_literal = "A" * 900
    a_dir = make_snapshot(tmp_path, "a", BASE_JS + f'var z = "{long_literal}";\n')
    b_dir = make_snapshot(tmp_path, "b", BASE_JS)
    md = research.build_diff_report(a_dir, b_dir)["markdown"]
    assert all(len(line) <= 200 for line in md.splitlines()), max(len(x) for x in md.splitlines())
