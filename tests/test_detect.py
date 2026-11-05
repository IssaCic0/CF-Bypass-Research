"""防护识别测试（离线：喂合成响应，不发网络请求）。

跑法：python -m pytest tests/test_detect.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

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


def test_recommend_multicaptcha_for_third_party():
    """v6.1：Akamai 等万盾厂商由 captchas/ 承接 → 必须给出可执行的 want，而不是 unsupported。"""
    rec = detect.recommend(_hits(status=200, cookies={"_abck": "x"}))
    assert rec["route"] == "multicaptcha"
    assert rec["want"] == "akamai"
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
class _StubSolver:
    """`_run_detect` 依赖的 Solver 表面：入口守卫 + 逐跳守卫 + 独立准入闸门。"""

    def __init__(self, max_detect: int = 4, blocked=("blocked.invalid",), blocked_hops=()):
        import threading

        from errors import ErrorCode, SolveError

        self._ErrorCode = ErrorCode
        self._SolveError = SolveError
        self._blocked = set(blocked)
        self._blocked_hops = set(blocked_hops)
        self.detect_inflight = threading.BoundedSemaphore(max(1, max_detect))
        self.guarded_hops: list[str] = []

    def _check_target(self, host):
        if host in self._blocked:
            raise self._SolveError(self._ErrorCode.INVALID_REQUEST, "受限地址", stage="validate")

    def guard_hop(self, url):
        self.guarded_hops.append(url)
        if urlsplit(url).hostname in self._blocked_hops:
            raise self._SolveError(self._ErrorCode.INVALID_REQUEST, "重定向到受限地址", stage="validate")

    def probe_user_agent(self):
        return "stub-ua"

    # T-34 的探测结果缓存（按域短 TTL）。替身默认不缓存：
    # 这两条用例要验证的是"准入闸门"与"逐跳守卫"本身，
    # 缓存命中会绕过闸门（这是正确的产品行为：命中不发任何出站请求，
    # 没必要占探测额度），但会让这里测不到闸门，所以替身直接返回未命中。
    def detect_cache_get(self, url):
        return None

    def detect_cache_put(self, url, result):
        return None


def _serve_handler():
    import threading

    import service as svc

    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_service_detect_endpoint(monkeypatch):
    import json
    import urllib.error
    import urllib.request

    import service as svc

    stub = _StubSolver()

    def fake_probe(url, timeout=15.0, *, guard=None, user_agent=""):
        # 契约：入口守卫由 _run_detect 负责，逐跳守卫必须传进来（X1 的核心修复）
        assert guard is not None, "probe_protection 必须收到逐跳守卫"
        return detect.summarize(url, status=403, headers={"cf-mitigated": "challenge"},
                                body="cf challenge", cookies={})

    monkeypatch.setattr(svc, "probe_protection", fake_probe)
    svc.Handler.solver = stub
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv, base = _serve_handler()

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


# ── X1：/detect 的重定向 SSRF 守卫（离线，本地两跳，真发 HTTP）────────
def _two_hop_servers(location_template: str | None = None):
    """entry 服务发 302 → 内层服务。返回 (entry_url, inner_url, inner_hits, srv列表)。"""
    import http.server
    import threading

    hits: list[str] = []

    class Inner(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    inner = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Inner)
    threading.Thread(target=inner.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    inner_url = f"http://127.0.0.1:{inner.server_address[1]}/secret"

    class Outer(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location_template or inner_url)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    outer = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Outer)
    threading.Thread(target=outer.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    entry_url = f"http://127.0.0.1:{outer.server_address[1]}/"
    return entry_url, inner_url, hits, (outer, inner)


def test_probe_blocks_redirect_to_guarded_host():
    """回归：旧的 probe_protection 用 urlopen 自动跟随重定向，守卫只看了入口 → 一跳到内网。"""
    import service as svc

    entry, inner_url, hits, servers = _two_hop_servers()
    try:
        def guard(url):
            if url == inner_url:
                raise svc.SolveError(svc.ErrorCode.INVALID_REQUEST, "受限地址", stage="validate")

        with pytest.raises(svc.SolveError) as ei:
            svc.probe_protection(entry, timeout=5, guard=guard)
        assert ei.value.code is svc.ErrorCode.INVALID_REQUEST
        assert hits == [], "被守卫拒绝的重定向目标一个字节都不该被请求到"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_probe_checks_every_hop_and_reports_final_url():
    """守卫必须被每一跳调用；放行时正常跟随并把 final_url 报出来。"""
    import service as svc

    entry, inner_url, hits, servers = _two_hop_servers()
    hops: list[str] = []
    try:
        out = svc.probe_protection(entry, timeout=5, guard=lambda u: hops.append(u))
        assert hits == ["/secret"], "放行后应真的请求到重定向目标"
        assert hops == [entry, inner_url], f"守卫调用轨迹不对：{hops}"
        assert out["final_url"] == inner_url
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_probe_aborts_on_redirect_loop():
    """重定向环不能把 handler 线程挂死：超过 PROBE_MAX_REDIRECTS 直接报 transport。"""
    import service as svc

    # Location 用相对路径指向自己 ⇒ 无限自跳
    entry, _inner, hits, servers = _two_hop_servers(location_template="SELF")
    try:
        with pytest.raises(svc.SolveError) as ei:
            svc.probe_protection(entry, timeout=5)
        assert ei.value.code is svc.ErrorCode.TRANSPORT
        assert "跳" in str(ei.value)
        assert hits == [], "不应真的打到内层服务"
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_guard_hop_rejects_non_http_scheme_and_private_host():
    """`guard_hop` 的两种拒绝：非 http(s) 协议、解析到受限地址；并放行 unresolvable。"""
    import service as svc

    class GuardOnly:
        _allow_private_hosts = False

        def _classify_host(self, host):
            if host == "internal.test":
                return "blocked"
            if host == "gone.test":
                return "unresolvable"
            return "allowed"

    g = GuardOnly()
    for bad in ("file:///etc/passwd", "gopher://x/", "ftp://x/"):
        with pytest.raises(svc.SolveError) as ei:
            svc.Solver.guard_hop(g, bad)
        assert ei.value.code is svc.ErrorCode.INVALID_REQUEST
        assert "协议" in str(ei.value)

    with pytest.raises(svc.SolveError) as ei:
        svc.Solver.guard_hop(g, "http://internal.test/admin")
    assert ei.value.code is svc.ErrorCode.INVALID_REQUEST

    # unresolvable 放行：与 /solve 的 route 守卫同语义，避免一条坏广告域废掉整次探测
    svc.Solver.guard_hop(g, "http://gone.test/x")
    svc.Solver.guard_hop(g, "https://public.test/x")

    # --allow-private-hosts 时全部放行
    g._allow_private_hosts = True
    svc.Solver.guard_hop(g, "http://internal.test/admin")


# ── X1：/detect 的独立准入闸门 + chunked 请求体 ─────────
def test_detect_endpoint_returns_429_when_gate_is_full(monkeypatch):
    import json
    import urllib.error
    import urllib.request

    import service as svc

    stub = _StubSolver(max_detect=1)
    monkeypatch.setattr(svc, "probe_protection",
                        lambda url, timeout=15.0, *, guard=None, user_agent="": {"ok": True})
    svc.Handler.solver = stub
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv, base = _serve_handler()
    try:
        stub.detect_inflight.acquire()          # 把唯一的额度占住
        req = urllib.request.Request(
            base + "/detect", data=json.dumps({"url": "https://x/"}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            raise AssertionError("应 429")
        except urllib.error.HTTPError as e:
            assert e.code == 429
            assert json.loads(e.read().decode())["error_code"] == "busy"
    finally:
        stub.detect_inflight.release()
        srv.shutdown()
        srv.server_close()


def test_detect_endpoint_closes_connection_on_chunked_body():
    """chunked 请求体不解析：必须 400 **并关闭连接**，否则残留字节会污染下一个请求。"""
    import json
    import socket

    import service as svc

    stub = _StubSolver()
    svc.Handler.solver = stub
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv, base = _serve_handler()
    port = srv.server_address[1]
    body = json.dumps({"url": "https://x/"}).encode()
    request = (
        b"POST /detect HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer t\r\n"
        b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
        + ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n"
    )
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
            sock.sendall(request)
            raw = b""
            closed = False
            sock.settimeout(5)
            while True:
                try:
                    chunk = sock.recv(4096)
                except TimeoutError:
                    break
                if not chunk:
                    closed = True     # 读到 EOF ⇒ 服务端关了连接
                    break
                raw += chunk
            assert raw.startswith(b"HTTP/1.1 400"), raw[:120]
            assert b"chunked" in raw, "错误信息应指出不支持 chunked"
            # 关键：连接必须被服务端关闭，否则残留的 chunk 字节会被当成下一个请求
            assert closed, "连接未被关闭，残留字节会污染 keep-alive 上的下一个请求"
    finally:
        srv.shutdown()
        srv.server_close()
