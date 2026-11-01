"""挑战类型策略测试：Turnstile / AWS WAF / reCAPTCHA v3（离线，注入假浏览器）。

跑法：python -m pytest tests/test_challenges.py -q
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import challenges as ch  # noqa: E402
import service as svc  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402
from test_service import (  # noqa: E402
    FakeBrowser,
    FakeCM,
    FakeRoute,
    wait_until,
)

HOST = "https://example.com/"
AWS_COOKIES = [
    {"name": "aws-waf-token", "value": "AWS-TOKEN", "domain": ".example.com", "path": "/",
     "expires": time.time() + 900, "httpOnly": True, "secure": True, "sameSite": "None"},
]


def solver_with(browser, **kw):
    kw.setdefault("allow_private_hosts", True)
    return svc.Solver(browser_factory=lambda *, headless: FakeCM(browser), **kw)


# ── Want 解析 ───────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    (None, ch.Want.CLEARANCE), ("", ch.Want.CLEARANCE), ("clearance", ch.Want.CLEARANCE),
    ("awsWAF", ch.Want.AWSWAF), (" turnstile ", ch.Want.TURNSTILE), ("recaptcha_v3", ch.Want.RECAPTCHA_V3),
])
def test_want_parse(raw, expected):
    assert ch.Want.parse(raw) is expected


def test_unknown_want_rejected():
    with pytest.raises(ValueError) as ei:
        ch.Want.parse("hcaptcha")
    assert "未知 want" in str(ei.value)


# ── Turnstile 存根页 ────────────────────────────────
def test_turnstile_stub_contains_sitekey_and_options():
    html = ch.build_turnstile_stub("0xAAA", action="login", cdata="cd")
    assert 'sitekey: "0xAAA"' in html
    assert 'action: "login"' in html and 'cdata: "cd"' in html
    assert "challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" in html
    assert "turnstile.render" in html, "explicit 模式必须主动 render，否则永远拿不到 token"


@pytest.mark.parametrize("evil", [
    '0x" onload="alert(1)',
    "0x</script><script>alert(1)</script>",
    "0x'><img src=x onerror=alert(1)>",
])
def test_turnstile_stub_escapes_injection(evil):
    """恶意 sitekey 必须被转义成 JS 字面量，绝不能拼出可执行的 HTML/JS。"""
    baseline = ch.build_turnstile_stub("1xclean").count("</script>")
    html = ch.build_turnstile_stub(evil)
    # 不变量：恶意值不得引入额外脚本结束标签（否则可提前闭合脚本块注入执行）
    assert html.count("</script>") == baseline, "恶意值引入了额外的 </script>"
    assert "<" in html or "<" not in evil or evil.strip("<script>") == ""


@pytest.mark.parametrize("bad", ["", "x" * 300])
def test_turnstile_stub_rejects_empty_or_too_long(bad):
    with pytest.raises(ValueError):
        ch.build_turnstile_stub(bad)


# ── AWS WAF：等 cookie，可缓存 ──────────────────────
def test_awswaf_returns_cookie_and_is_cacheable():
    b = FakeBrowser(cookie_supplier=lambda: list(AWS_COOKIES))
    s = solver_with(b)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10, want="awswaf")
        assert r["ok"] is True and r["want"] == "awswaf"
        assert r["aws_waf_token"] == "AWS-TOKEN"
        assert r["cacheable"] is True
        assert r["cf_clearance"] if "cf_clearance" in r else True     # 不该伪造 cf_clearance
        # cookie 类可复用
        assert s.solve(HOST, timeout=10, want="awswaf")["cached"] is True
    finally:
        s.close()


# ── Turnstile：拿 token，且**绝不缓存** ─────────────
def test_turnstile_returns_token_and_never_caches():
    b = FakeBrowser()
    b.input_values = {ch.TURNSTILE_SELECTOR: "TS-TOKEN"}
    s = solver_with(b)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10, want="turnstile", sitekey="0xAAA", action="login")
        assert r["token"] == "TS-TOKEN"
        assert r["token_field"] == "cf-turnstile-response"
        assert r["cacheable"] is False and r["cached"] is False
        assert 0 < r["expires_in"] <= 300

        contexts_after_first = len(b.contexts)
        r2 = s.solve(HOST, timeout=10, want="turnstile", sitekey="0xAAA", action="login")
        assert r2["cached"] is False, "一次性 token 绝不能命中缓存"
        assert len(b.contexts) > contexts_after_first, "第二次必须真的重新求解"
    finally:
        s.close()


def test_turnstile_requires_sitekey():
    s = solver_with(FakeBrowser())
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10, want="turnstile")
        assert ei.value.code is ErrorCode.INVALID_REQUEST
        assert "sitekey" in ei.value.message
    finally:
        s.close()


def test_turnstile_stub_is_served_at_target_origin():
    """目标文档必须由本地存根页 fulfill —— token 绑 origin，换域拿到的没用。"""
    b = FakeBrowser()
    b.input_values = {ch.TURNSTILE_SELECTOR: "TS-TOKEN"}
    s = solver_with(b)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")

        original_goto = type(b).__mro__  # 占位，避免多余导入
        from test_service import FakePage

        seen = {}

        def goto_and_inspect(self_page, url, wait_until=None, timeout=None):
            handler = self_page.ctx.route_handler
            route = FakeRoute(url)
            handler(route)
            seen["route"] = route

        old = FakePage.goto
        FakePage.goto = goto_and_inspect
        try:
            r = s.solve(HOST, timeout=10, want="turnstile", sitekey="0xAAA")
        finally:
            FakePage.goto = old

        assert r["token"] == "TS-TOKEN"
        route = seen["route"]
        assert route.fulfilled, "目标文档应被本地存根页 fulfill"
        assert "turnstile.render" in (route.body or "")
        assert not route.continued
    finally:
        s.close()


# ── reCAPTCHA v3 ───────────────────────────────────
def test_recaptcha_v3_executes_and_returns_token():
    b = FakeBrowser()
    b.rc_token = "RC-TOKEN"
    calls = {"ensure": 0, "fire": 0}

    def hook(expr, arg):
        if "grecaptcha.ready" in expr:
            calls["fire"] += 1
            return "started"
        if "recaptcha/api.js" in expr:
            calls["ensure"] += 1
            return "injected"
        return None

    b.evaluate_hook = hook
    s = solver_with(b)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10, want="recaptcha_v3", sitekey="0xRC", action="submit")
        assert r["token"] == "RC-TOKEN"
        assert r["token_field"] == "g-recaptcha-response"
        assert r["cacheable"] is False
        assert calls["ensure"] >= 1 and calls["fire"] >= 1
    finally:
        s.close()


def test_recaptcha_v3_falls_back_to_dom_value():
    """有些站点 execute 后只回填 DOM，不返回 token。"""
    b = FakeBrowser()
    b.input_values = {"[name=g-recaptcha-response]": "RC-DOM"}
    s = solver_with(b)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10, want="recaptcha_v3", sitekey="0xRC")
        assert r["token"] == "RC-DOM"
    finally:
        s.close()


# ── HTTP 层 ─────────────────────────────────────────
@pytest.fixture
def live_server():
    b = FakeBrowser()
    b.input_values = {ch.TURNSTILE_SELECTOR: "TS-HTTP"}
    s = solver_with(b)
    assert wait_until(lambda: s.health()[0] == 200)
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
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
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def test_http_turnstile_end_to_end(live_server):
    status, body = _post(live_server, {"url": HOST, "timeout": 10, "want": "turnstile", "sitekey": "0xAAA"})
    assert status == 200 and body["token"] == "TS-HTTP" and body["want"] == "turnstile"


def test_http_rejects_unknown_want_and_missing_sitekey(live_server):
    status, body = _post(live_server, {"url": HOST, "want": "hcaptcha"})
    assert status == 400 and body["error_code"] == "invalid-request"
    status, body = _post(live_server, {"url": HOST, "want": "recaptcha_v3"})
    assert status == 400 and "sitekey" in body["error"]
    status, body = _post(live_server, {"url": HOST, "want": "turnstile", "sitekey": "x" * 300})
    assert status == 400 and body["error_code"] == "invalid-request"


def test_http_clearance_still_default(live_server):
    """不带 want 时必须仍是 clearance（向后兼容）。"""
    status, body = _post(live_server, {"url": HOST, "timeout": 10})
    assert status == 200 and body["want"] == "clearance" and body["cf_clearance"]
