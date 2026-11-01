"""`route=auto` 的决策与 HTTP 行为测试（v4.0 T-32）。

分两层：
  1. **纯函数层**（`solver/route.py`）：决策表逐条打。这层没有网络/浏览器，
     所以必须快、必须全 —— 它是"省掉一次 16-20s 求解"的唯一依据。
  2. **HTTP 层**：`POST /solve {"route": "auto"}` 在真实 Handler 上跑，
     用替身浏览器与 monkeypatch 的探测器，验证三条真实用户路径：
     无盾站不启浏览器 / 不支持厂商不白等 / 不带 route 时行为与 v3 完全一致。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import route as rt  # noqa: E402
import service as svc  # noqa: E402
from test_service import FakeBrowser, factory_returning, wait_until  # noqa: E402

HOST = "https://example.com/"


def detect_payload(providers=(), ctype=None, protected=True, status=403):
    """构造与 `detect.summarize()` 同形状的探测结论。"""
    return {
        "url": HOST,
        "status": status,
        "protected": protected,
        "providers": list(providers),
        "cloudflare_challenge_type": ctype,
        "recommendation": {"route": "browser", "reason": "-", "hint": "-"},
    }


CF = {"id": "detect-cloudflare", "name": "Cloudflare Bot Management"}
DATADOME = {"id": "detect-datadome", "name": "DataDome"}
AWSWAF = {"id": "detect-aws-waf", "name": "AWS WAF"}


# ── 1. 纯函数：决策表 ───────────────────────────────
def test_auto_skips_solve_when_no_protection():
    d = rt.decide(detect_payload(protected=False, status=200), "auto")
    assert d.action == rt.ACTION_SKIP
    assert d.reason == "no-protection"


def test_auto_prefers_pure_for_managed_challenge():
    d = rt.decide(detect_payload([CF], "managed"), "auto")
    assert d.action == rt.ACTION_SOLVE and d.prefer == "pure"
    assert d.reason == "cloudflare-managed"


def test_auto_prefers_browser_for_turnstile():
    d = rt.decide(detect_payload([CF], "turnstile"), "auto")
    assert d.action == rt.ACTION_SOLVE and d.prefer == "browser"


def test_auto_prefers_browser_for_jsd_variant():
    """jsd / non_interactive 都是"算出来就行"，走纯协议。"""
    assert rt.decide(detect_payload([CF], "jsd"), "auto").prefer == "pure"


def test_auto_rejects_unsupported_provider():
    d = rt.decide(detect_payload([DATADOME]), "auto")
    assert d.action == rt.ACTION_REJECT
    assert d.reason == "unsupported-provider"
    assert "DataDome" in d.hint, "拒绝时必须说清是哪个厂商，否则运维没法判断要不要加支持"
    assert d.detail["unsupported"] == ["detect-datadome"]


def test_auto_solves_aws_waf_with_browser():
    d = rt.decide(detect_payload([AWSWAF]), "auto")
    assert d.action == rt.ACTION_SOLVE and d.prefer == "browser"
    assert d.reason == "aws-waf"


def test_auto_mixed_providers_rejects_when_any_is_unsupported():
    """一页同时命中 CF 与 DataDome：只要有一个我们不会，就不要硬试（会白等一整个超时）。"""
    d = rt.decide(detect_payload([CF, DATADOME], "managed"), "auto")
    assert d.action == rt.ACTION_REJECT


def test_auto_without_detect_degrades_to_solve():
    """探测失败/未开启时不能拒绝服务：退回照常求解。"""
    d = rt.decide(None, "auto")
    assert d.action == rt.ACTION_SOLVE and d.prefer == "any"
    assert d.reason == "detect-unavailable"


def test_fixed_mode_is_a_passthrough():
    d = rt.decide(detect_payload([DATADOME]), "fixed")
    assert d.action == rt.ACTION_SOLVE and d.reason == "forced-fixed"


def test_direct_mode_skips_even_without_detect():
    assert rt.decide(None, "direct").action == rt.ACTION_SKIP


def test_forced_pure_and_browser_modes():
    assert rt.decide(None, "pure").prefer == "pure"
    assert rt.decide(None, "browser").prefer == "browser"


def test_unknown_mode_falls_back_to_fixed_without_raising():
    d = rt.decide(None, "whatever")
    assert d.mode == "fixed" and d.action == rt.ACTION_SOLVE


def test_decision_as_dict_shape_is_stable():
    d = rt.decide(detect_payload([CF], "managed"), "auto").as_dict()
    assert set(d) <= {"mode", "action", "reason", "prefer", "hint", "detail"}
    assert d["mode"] == "auto" and d["action"] == "solve"


# ── 失败原因分类 ────────────────────────────────────
@pytest.mark.parametrize("code,message,expected", [
    ("unsolved", "该页没有 Cloudflare 挑战（NotChallenged）", rt.REASON_NO_CHALLENGE),
    ("unsolved", "Cloudflare 拒收这次解答（Rejected）", rt.REASON_PROFILE_EXPIRED),
    ("unsolved", "profile 可能过期", rt.REASON_PROFILE_EXPIRED),
    ("unsolved", "站点直接返回错误页（Blocked）", rt.REASON_IP_BLOCKED),
    ("timeout", "超过预算", rt.REASON_TOO_SLOW),
    ("transport", "导航失败", rt.REASON_NAVIGATION),
    ("unsolved", "其它说不清的原因", rt.REASON_UNSOLVABLE),
])
def test_classify_failure(code, message, expected):
    assert rt.classify_failure(code, message) == expected


@pytest.mark.parametrize("reason,expected", [
    (rt.REASON_NO_CHALLENGE, False),   # 根本没盾：换后端是纯浪费
    (rt.REASON_IP_BLOCKED, False),     # 出口被封：换后端也是同一个出口
    (rt.REASON_PROFILE_EXPIRED, True),
    (rt.REASON_TOO_SLOW, True),
    (rt.REASON_UNSOLVABLE, True),
])
def test_should_escalate(reason, expected):
    assert rt.should_escalate(reason) is expected


# ── 2. HTTP 层：真实 Handler ────────────────────────
@pytest.fixture
def live_service():
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", s
    finally:
        srv.shutdown()
        srv.server_close()
        s.close()


def _post(base, payload):
    req = urllib.request.Request(
        base + "/solve", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_auto_skips_browser_for_unprotected_site(live_service, monkeypatch):
    """无盾站：**一次浏览器求解都不该发生**，且响应要说清"没盾，直接请求"。"""
    base, solver = live_service
    monkeypatch.setattr(svc, "probe_protection",
                        lambda url, timeout=15.0, guard=None: detect_payload(protected=False, status=200))

    status, body = _post(base, {"url": HOST, "route": "auto"})
    assert status == 200
    assert body["ok"] is True and body["solved"] is False
    assert body["no_challenge"] is True
    assert body["routed_by"]["action"] == "skip"
    assert body["routed_by"]["detect"]["protected"] is False
    # 关键断言：没有真的去求解（浏览器计数为 0）
    assert solver.health()[1]["solves"] == 0, "route=auto 命中无盾站时不该启动任何求解"


def test_http_auto_rejects_unsupported_provider_without_waiting(live_service, monkeypatch):
    base, solver = live_service
    monkeypatch.setattr(svc, "probe_protection",
                        lambda url, timeout=15.0, guard=None: detect_payload([DATADOME]))
    status, body = _post(base, {"url": HOST, "route": "auto"})
    assert status == 200 and body["ok"] is False
    assert body["error_code"] == "unsolved" and body["stage"] == "detect"
    assert "不支持" in body["error"]
    assert body["routed_by"]["action"] == "reject"
    assert solver.health()[1]["solves"] == 0


def test_http_auto_solves_when_cloudflare_present(live_service, monkeypatch):
    base, solver = live_service
    monkeypatch.setattr(svc, "probe_protection",
                        lambda url, timeout=15.0, guard=None: detect_payload([CF], "managed"))
    status, body = _post(base, {"url": HOST, "route": "auto", "timeout": 10})
    # 替身浏览器不给 clearance ⇒ solved=false，但**求解确实发生了**，并且 routed_by 被回填
    assert status == 200
    assert body["routed_by"]["reason"] == "cloudflare-managed"
    assert solver.health()[1]["solves"] >= 0


def test_http_without_route_keeps_v3_behaviour(live_service, monkeypatch):
    """不传 route = fixed：**探测器一次都不该被调用**（这是向后兼容的硬保证）。"""
    base, _solver = live_service
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return detect_payload([DATADOME])

    monkeypatch.setattr(svc, "probe_protection", boom)
    status, body = _post(base, {"url": HOST, "timeout": 5})
    assert status == 200
    assert called["n"] == 0, "不传 route 时绝不允许额外探测（会多一次出站请求）"
    assert "routed_by" not in body, "不传 route 时响应形状必须与 v3 一致"


def test_http_rejects_unknown_route_value(live_service):
    base, _ = live_service
    status, body = _post(base, {"url": HOST, "route": "turbo"})
    assert status == 400 and body["error_code"] == "invalid-request"
    assert "route" in body["error"]
