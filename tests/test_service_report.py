"""`scripts/service_report.py` 的形状归一契约（v6.2）。

为什么要单测这个脚本的核心函数：它产出的是**门禁本身**。
如果 `shape()` 把"环境差异"当成"契约漂移"，门禁会在 CI 上假红（真的发生过两次）；
如果它把"真实漂移"归一掉了，门禁就形同虚设。两个方向都要锁。

三次 CI 假红的来源（都在这里回归）：
  1. 测试改全局 `os.name` 让 pytest 崩（在 test_observability，不在此处但同源）
  2. `last_snapshot_at` 在有研究快照的机器上是 string、CI 上是 null → 类型漂移
  3. volatile 判断排在 `None` 之后 → 上一行修了也没用
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "service_report", ROOT / "scripts" / "service_report.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["service_report"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


sr = _load()


# ── shape()：只记类型，不记值 ────────────────────────


def test_shape_records_types_not_values():
    assert sr.shape("hello") == "string"
    assert sr.shape(42) == "number"
    assert sr.shape(True) == "bool"
    assert sr.shape(None) == "null"


def test_shape_object_key_order_is_deterministic():
    a = sr.shape({"b": 1, "a": 2})
    b = sr.shape({"a": 2, "b": 1})
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_shape_array_ignores_length_and_order():
    """数组只记**元素类型的并集**：长度和顺序都会随运行变化，不能进契约。"""
    one = sr.shape([1])
    three = sr.shape([1, 2, 3])
    assert one == three
    assert sr.shape([])["items"] == []


def test_shape_array_merges_distinct_element_shapes():
    got = sr.shape([{"a": 1}, "x"])
    kinds = sorted(json.dumps(i, sort_keys=True) for i in got["items"])
    assert len(kinds) == 2, kinds


# ── volatile：环境差异不能进契约 ────────────────────


def test_volatile_field_is_same_shape_for_null_and_string():
    """回归（CI 假红 #2/#3）：`last_snapshot_at` 本机是 ISO 字符串、CI 是 null。

    两者必须是同一个形状 —— volatile 判断要**排在 None 判断之前**，
    否则 `null` 会先返回 "null"，与 "<volatile>" 不等。
    """
    assert sr.shape(None, "last_snapshot_at") == sr.shape("2026-11-04T07:24:51Z",
                                                          "last_snapshot_at")
    assert sr.shape(None, "last_diff_at") == sr.shape(1234567890.0, "last_diff_at")


def test_volatile_field_type_is_not_asserted():
    """volatile 字段不该暴露具体类型（否则又绑回"取到什么值"）。"""
    assert sr.shape("x", "cf_clearance") == "<volatile>"
    assert sr.shape(123, "uptime_sec") == "<volatile>"


def test_counters_keep_their_type():
    """计数器**不能**标 volatile：它们类型恒定（number），值不同是正常的。

    把它们标 volatile 会连"这里是数字"这条检查一起丢掉 —— 等于自己削弱门禁。
    """
    assert sr.shape(7, "solves") == "number"
    assert sr.shape(0, "failures") == "number"
    assert sr.shape(3, "queue_depth") == "number"


# ── diff()：人类可读的漂移描述 ──────────────────────


def test_diff_reports_added_and_missing_fields():
    got = {"endpoints": {"a": {"fields": {"x": "string", "new": "bool"}}}}
    want = {"endpoints": {"a": {"fields": {"x": "string", "old": "string"}}}}
    problems = sr.diff(got, want)
    assert any("新增" in p and "new" in p for p in problems)
    assert any("缺少" in p and "old" in p for p in problems)


def test_diff_reports_type_change():
    problems = sr.diff({"k": "number"}, {"k": "string"})
    assert problems and "类型变化" in problems[0]


def test_diff_is_empty_for_identical_shapes():
    assert sr.diff({"a": {"b": "string"}}, {"a": {"b": "string"}}) == []


# ── 报告本身的形状 ──────────────────────────────────


def test_golden_is_committed_and_wellformed():
    golden = ROOT / "docs" / "golden" / "service-report.golden.json"
    assert golden.exists(), "缺少 golden —— 门禁没有基线"
    data = json.loads(golden.read_text(encoding="utf-8"))
    assert data["schema"] >= 2
    assert "health-auth" in data["endpoints"]
    assert "health-public" in data["endpoints"]


def test_endpoint_labels_are_unique():
    """标签重复会让同一 path 的多个用例互相覆盖，而报告看起来仍"全都测过"。"""
    labels = [row[0] for row in sr.ENDPOINTS]
    assert len(labels) == len(set(labels)), labels


def test_golden_contains_no_secrets():
    """golden 是对外交付物：不得出现 token / cookie 值。"""
    text = (ROOT / "docs" / "golden" / "service-report.golden.json").read_text(encoding="utf-8")
    assert "cf_clearance=" not in text
    assert "CF_SOLVE_TOKEN" not in text
