"""`detect` 签名表的 golden 差分回归（v5.0.1 P1-5）。

为什么需要：`solver/detect.py` 的 `SIGNATURES`（33 家厂商 + 5 条自有强特征）是**生成物** ——
`scripts/gen_detect_signatures.py` 可以从上游 `scrapfly/Antibot-Detector` 重新生成。
重新生成后，某条规则的字面量/置信度一变，`detect()` 的结论就会**静默变化**，
而 `detect.py` 97% 的覆盖率对此毫无防护：覆盖率只回答"这行代码跑过没有"，
不回答"跑出来的结论还是不是我们要的那个"。

做法：`tests/fixtures/detect/cases.json` 固定"输入 → 期望结论"。
断言粒度刻意选在 **(厂商 id, 置信度) 列表 + ctype + route** 这一层：
  · 更细（比如命中了哪几个 hits）会脆到没法维护；
  · 更粗（比如只看 route）会漏掉"置信度被调低导致优先级变化"这类真实回归。

改夹具里的期望值前必须先回答：这是**我想要的结论**，还是**现在恰好是这样**？
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from solver.detect import cf_challenge_type, detect, recommend

CASES_PATH = Path(__file__).parent / "fixtures" / "detect" / "cases.json"

with CASES_PATH.open(encoding="utf-8") as fh:
    _DOC = json.load(fh)

CASES = _DOC["cases"]


def _ids() -> list[str]:
    return [c["name"] for c in CASES]


def _run(case: dict) -> tuple[list[list], str, str]:
    inp = case["input"]
    detections = detect(
        status=inp.get("status", 0),
        headers=inp.get("headers"),
        body=inp.get("body", ""),
        url=inp.get("url", ""),
        cookies=inp.get("cookies"),
    )
    ctype = cf_challenge_type(inp.get("body", ""), inp.get("headers"), inp.get("url", ""))
    route = recommend(detections, ctype)["route"]
    pairs = [[d.id, d.confidence] for d in detections]
    return pairs, ctype, route


def test_fixtures_are_not_empty():
    """夹具被误删/清空时，下面的参数化会"零用例通过"——必须显式挡住。"""
    assert len(CASES) >= 8
    assert len({c["name"] for c in CASES}) == len(CASES), "夹具 name 必须唯一"


@pytest.mark.parametrize("case", CASES, ids=_ids())
def test_golden(case):
    pairs, ctype, route = _run(case)
    expect = case["expect"]

    # 逐项断言并给出可读 diff：一次就知道是哪一层变了
    assert pairs == [list(x) for x in expect["detections"]], (
        f"[{case['name']}] 命中厂商/置信度变了。\n"
        f"  期望 {expect['detections']}\n  实际 {pairs}\n"
        f"  场景说明：{case.get('why', '')}"
    )
    assert ctype == expect["ctype"], (
        f"[{case['name']}] cType 判定变了：期望 {expect['ctype']!r}，实际 {ctype!r}。\n"
        f"  这条决定「走纯协议还是走浏览器」，判错=白烧 16-20s。\n"
        f"  场景说明：{case.get('why', '')}"
    )
    assert route == expect["route"], (
        f"[{case['name']}] 路由结论变了：期望 {expect['route']!r}，实际 {route!r}。\n"
        f"  场景说明：{case.get('why', '')}"
    )


def test_every_case_declares_why():
    """每条夹具都必须写"为什么这个结论是对的"——防止后来者把现状当契约。"""
    for case in CASES:
        assert case.get("why"), f"{case['name']} 缺 why"
        assert len(case["why"]) >= 10, f"{case['name']} 的 why 太敷衍"


def test_clean_site_must_stay_direct():
    """单挑一条最贵的回归：无盾站被误判成有盾 → 每次多花 16-20s + 1.2GB。

    这条不放在参数化里，因为它守的是"route=auto 的全部收益"，值得有独立的名字。
    """
    pairs, ctype, route = _run(next(c for c in CASES if c["name"] == "clean-site"))
    assert (pairs, ctype, route) == ([], "", "direct")
