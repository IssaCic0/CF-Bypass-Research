"""防护识别测试（离线：喂合成响应，不发网络请求）。

跑法：python -m pytest tests/test_detect.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import detect  # noqa: E402


# ── 特征表本身 ──────────────────────────────────────
def test_signature_table_is_populated():
    assert len(detect.SIGNATURES) >= 15, "特征表应有 15+ 个厂商"
    ids = {s["id"] for s in detect.SIGNATURES}
    for expected in ("detect-cloudflare", "detect-akamai", "detect-datadome", "detect-aws-waf"):
        assert expected in ids, f"缺少 {expected}"
    for signature in detect.SIGNATURES:
        assert signature["signals"], f"{signature['id']} 没有信号"


# ── 单厂商识别 ──────────────────────────────────────
def test_detect_cloudflare_by_clearance_cookie():
    hits = detect.detect(status=200, cookies={"cf_clearance": "x"})
    assert hits and hits[0].id == "detect-cloudflare"
    assert hits[0].confidence == 100


def test_detect_cloudflare_by_mitigated_header():
    hits = detect.detect(status=403, headers={"cf-mitigated": "challenge"})
    assert any(d.id == "detect-cloudflare" for d in hits)
    assert max(d.confidence for d in hits) >= 90


def test_detect_akamai_by_cookie():
    hits = detect.detect(status=200, cookies={"_abck": "x", "bm_sz": "y"})
    assert hits[0].id == "detect-akamai"
    assert hits[0].confidence >= 60


def test_detect_aws_waf_by_cookie_and_body():
    hits = detect.detect(status=403, body="aws-waf-token challenge")
    assert any(d.id == "detect-aws-waf" for d in hits)


def test_detect_nothing_on_plain_page():
    assert detect.detect(status=200, body="<html>hello</html>", headers={"server": "nginx"}) == []


def test_wildcard_cookie_match():
    assert detect._cookie_matches("awswaf_session", "awswaf*") is True
    assert detect._cookie_matches("aws-waf-token", "aws-waf-token") is True
    assert detect._cookie_matches("other", "awswaf*") is False


# ── CF 挑战类型 ─────────────────────────────────────
@pytest.mark.parametrize("body,expected", [
    ("var o = {cType:'managed', cRay:'x'}", "managed"),
    ('{"cType":"jsd"}', "jsd"),
    ("...challenges.cloudflare.com/turnstile/v0/...", "turnstile"),
    ("<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>", "managed"),
])
def test_cf_challenge_type(body, expected):
    assert detect.cf_challenge_type(body) == expected


def test_cf_challenge_type_from_header_only():
    assert detect.cf_challenge_type("", {"cf-mitigated": "challenge"}) == "managed"


def test_cf_challenge_type_unknown():
    assert detect.cf_challenge_type("hello", {}) == ""


# ── 建议路由 ────────────────────────────────────────
def _hits(**kwargs):
    return detect.detect(**kwargs)


def test_recommend_direct_when_nothing_detected():
    rec = detect.recommend([])
    assert rec["route"] == "direct"


def test_recommend_browser_for_managed():
    rec = detect.recommend(_hits(status=403, headers={"cf-mitigated": "challenge"}), "managed")
    assert rec["route"] == "browser"


def test_recommend_pure_protocol_for_jsd():
    rec = detect.recommend(_hits(status=403, body="cType:'jsd'"), "jsd")
    assert rec["route"] == "pure_protocol_may_work"


def test_recommend_turnstile_want():
    rec = detect.recommend(_hits(status=403, body="challenges.cloudflare.com/turnstile"), "turnstile")
    assert rec["route"] == "browser_token" and rec["want"] == "turnstile"


def test_recommend_awswaf_want():
    rec = detect.recommend(_hits(status=403, body="aws-waf-token"))
    assert rec["route"] == "browser_cookie" and rec["want"] == "awswaf"


def test_recommend_unsupported_for_third_party():
    rec = detect.recommend(_hits(status=200, cookies={"_abck": "x"}))
    assert rec["route"] == "unsupported_or_browser"
    assert "Akamai" in rec["reason"]


def test_recommend_direct_when_cf_present_without_challenge():
    """有 CF 但当前没看到挑战标记 → 先直接抓，别浪费一次 16s 求解。"""
    rec = detect.recommend(_hits(status=200, cookies={"__cf_bm": "x"}), "")
    assert rec["route"] == "direct_unless_blocked"


# ── summarize 结构 ─────────────────────────────────
def test_summarize_shape_and_content():
    out = detect.summarize(
        "https://x/", status=403,
        headers={"cf-mitigated": "challenge"},
        body="Just a moment _cf_chl_opt = {cType:'managed'}",
        cookies={},
    )
    assert out["protected"] is True
    assert out["cloudflare_challenge_type"] == "managed"
    assert out["recommendation"]["route"] == "browser"
    assert out["providers"][0]["id"] == "detect-cloudflare"
    assert out["providers"][0]["hits"], "应给出命中证据"


def test_summarize_unprotected():
    out = detect.summarize("https://x/", status=200, headers={"server": "nginx"}, body="hi", cookies={})
    assert out["protected"] is False
    assert out["recommendation"]["route"] == "direct"
    assert out["cloudflare_challenge_type"] is None


# ── 服务端 /detect（离线：打桩 probe_protection，不发网络）────────
def test_service_detect_endpoint(monkeypatch):
    import json
    import threading
    import urllib.error
    import urllib.request

    import service as svc

    class StubSolver:
        def _check_target(self, host):
            if host == "blocked.invalid":
                from errors import ErrorCode, SolveError

                raise SolveError(ErrorCode.INVALID_REQUEST, "受限地址", stage="validate")

    def fake_probe(url, timeout=15.0):
        return detect.summarize(url, status=403, headers={"cf-mitigated": "challenge"},
                                body="cf challenge", cookies={})

    monkeypatch.setattr(svc, "probe_protection", fake_probe)
    svc.Handler.solver = StubSolver()
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(payload):
        req = urllib.request.Request(
            base + "/detect", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    try:
        status, body = post({"url": "https://x/"})
        assert status == 200 and body["protected"] is True
        assert body["cloudflare_challenge_type"] == "managed"
        # GET 入口
        req = urllib.request.Request(base + "/detect?url=https://y/", headers={"Authorization": "Bearer t"})
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 200
        # SSRF 守卫复用
        status, body = post({"url": "https://blocked.invalid/"})
        assert status == 400 and body["error_code"] == "invalid-request"
        # 缺 url
        status, body = post({})
        assert status == 400
        # 鉴权
        req = urllib.request.Request(base + "/detect?url=https://z/")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("应 401")
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        srv.shutdown()
        srv.server_close()
