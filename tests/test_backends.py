"""多后端接缝测试（后端协议 / 有序路由 / 错误码归一化 / FlareSolverr 契约）。

全部离线：用 http.server 打桩，不连任何真实服务。
跑法：python -m pytest tests/test_backends.py -q
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import backends as be  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402


class FakeBackend:
    """可编程的假后端：要么返回成功，要么抛指定错误码。"""

    def __init__(self, name: str, *, configured=True, result=None, error: SolveError | None = None):
        self._name = name
        self._configured = configured
        self._result = result
        self._error = error
        self.calls = 0

    def name(self) -> str:
        return self._name

    def is_configured(self) -> bool:
        return self._configured

    def solve(self, url: str, timeout: float, force: bool = False) -> dict:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._result or {})


OK_RESULT = {"ok": True, "solved": True, "cf_clearance": "C", "user_agent": "UA"}


# ── 白名单 ──────────────────────────────────────────
def test_backend_id_is_allowlist():
    assert be.BackendId.parse("camoufox") is be.BackendId.CAMOUFOX
    assert be.BackendId.parse(" FlareSolverr ") is be.BackendId.FLARESOLVERR
    with pytest.raises(ValueError):
        be.BackendId.parse("some.random.module:Class")     # 绝不允许配置值当类名用


# ── 路由 ────────────────────────────────────────────
def test_router_uses_first_healthy_backend():
    primary = FakeBackend("camoufox", result=OK_RESULT)
    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    out = be.BackendRouter([primary, fallback]).solve("https://e.com/", 10)
    assert out["backend"] == "camoufox"
    assert primary.calls == 1 and fallback.calls == 0
    assert out["attempts"] == [{"backend": "camoufox", "ok": True}]


def test_router_falls_back_on_failure_and_records_attempts():
    primary = FakeBackend("camoufox", error=SolveError(ErrorCode.UNSOLVED, "没解出来", stage="wait"))
    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    out = be.BackendRouter([primary, fallback]).solve("https://e.com/", 10)
    assert out["backend"] == "flaresolverr"
    assert out["attempts"][0] == {"backend": "camoufox", "ok": False, "error_code": "unsolved", "stage": "wait"}
    assert out["attempts"][1] == {"backend": "flaresolverr", "ok": True}


def test_router_skips_unconfigured_without_calling_it():
    primary = FakeBackend("camoufox", configured=False, result=OK_RESULT)
    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    out = be.BackendRouter([primary, fallback]).solve("https://e.com/", 10)
    assert primary.calls == 0, "未配置的后端不该被调用"
    assert out["attempts"][0]["skipped"] is True


def test_router_raises_last_error_with_attempt_trail():
    e1 = SolveError(ErrorCode.UNSOLVED, "a", stage="wait")
    e2 = SolveError(ErrorCode.BROWSER_UNAVAILABLE, "b", stage="admit")
    router = be.BackendRouter([
        FakeBackend("camoufox", error=e1),
        FakeBackend("flaresolverr", error=e2),
    ])
    with pytest.raises(SolveError) as ei:
        router.solve("https://e.com/", 10)
    assert ei.value.code is ErrorCode.BROWSER_UNAVAILABLE, "应抛最后一个错误"
    payload = ei.value.payload()
    assert len(payload["attempts"]) == 2, "必须回传 try 过谁、各自为什么失败"
    assert payload["attempts"][0]["error_code"] == "unsolved"


def test_router_swallows_unexpected_exceptions():
    class Boom(FakeBackend):
        def solve(self, url, timeout, force=False):
            raise RuntimeError("kaboom")

    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    out = be.BackendRouter([Boom("camoufox"), fallback]).solve("https://e.com/", 10)
    assert out["backend"] == "flaresolverr"
    assert out["attempts"][0]["error_code"] == "internal"


def test_router_requires_at_least_one_backend():
    with pytest.raises(ValueError):
        be.BackendRouter([])


# ── FlareSolverr 契约 ───────────────────────────────
def _serve(handler_func):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # noqa: A003
            pass

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode() or "{}")
            code, payload = handler_func(body)
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


FLARE_OK = {
    "status": "ok",
    "message": "Challenge solved!",
    "solution": {
        "url": "https://e.com/",
        "status": 200,
        "userAgent": "Firefox/156",
        "cookies": [
            {"name": "cf_clearance", "value": "FC", "domain": ".e.com", "path": "/",
             "expiry": 1999999999, "httpOnly": True, "secure": True, "sameSite": "None"},
            {"name": "__cf_bm", "value": "bm", "domain": ".e.com", "path": "/", "expiry": -1},
        ],
    },
}


def test_flaresolverr_backend_parses_solution():
    srv, base = _serve(lambda body: (200, FLARE_OK))
    try:
        backend = be.FlareSolverrBackend(base)
        out = backend.solve("https://e.com/", 30)
        assert out["ok"] and out["cf_clearance"] == "FC"
        assert out["user_agent"] == "Firefox/156"
        assert out["backend"] == "flaresolverr"
        names = [c["name"] for c in out["cookie_list"]]
        assert names == ["cf_clearance", "__cf_bm"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_flaresolverr_backend_maps_status_error_to_unsolved():
    srv, base = _serve(lambda body: (200, {"status": "error", "message": "Challenge not solved"}))
    try:
        with pytest.raises(SolveError) as ei:
            be.FlareSolverrBackend(base).solve("https://e.com/", 30)
        assert ei.value.code is ErrorCode.UNSOLVED
    finally:
        srv.shutdown()
        srv.server_close()


def test_flaresolverr_backend_missing_clearance_is_unsolved():
    payload = {"status": "ok", "solution": {"cookies": [{"name": "__cf_bm", "value": "x"}], "userAgent": "UA"}}
    srv, base = _serve(lambda body: (200, payload))
    try:
        with pytest.raises(SolveError) as ei:
            be.FlareSolverrBackend(base).solve("https://e.com/", 30)
        assert ei.value.code is ErrorCode.UNSOLVED
    finally:
        srv.shutdown()
        srv.server_close()


def test_flaresolverr_backend_http_error_is_transport():
    srv, base = _serve(lambda body: (500, {"status": "error"}))
    try:
        with pytest.raises(SolveError) as ei:
            be.FlareSolverrBackend(base).solve("https://e.com/", 30)
        assert ei.value.code is ErrorCode.TRANSPORT
    finally:
        srv.shutdown()
        srv.server_close()


def test_flaresolverr_backend_unreachable_is_browser_unavailable():
    with pytest.raises(SolveError) as ei:
        be.FlareSolverrBackend("http://127.0.0.1:1").solve("https://e.com/", 5)
    assert ei.value.code is ErrorCode.BROWSER_UNAVAILABLE


def test_flaresolverr_backend_sends_expected_fields():
    seen = {}

    def handler(body):
        seen.update(body)
        return 200, FLARE_OK

    srv, base = _serve(handler)
    try:
        be.FlareSolverrBackend(base, session="s1", proxy="http://p:8080").solve("https://e.com/", 12)
        assert seen["cmd"] == "request.get"
        assert seen["returnOnlyCookies"] is True
        assert seen["maxTimeout"] >= 11000
        assert seen["session"] == "s1"
        assert seen["proxy"] == {"url": "http://p:8080"}
    finally:
        srv.shutdown()
        srv.server_close()


# ── 与服务接线 ──────────────────────────────────────
def test_service_uses_router_when_configured():
    import service as svc
    from test_service import FakeBrowser, FakeCM, wait_until

    solver = svc.Solver(browser_factory=lambda *, headless: FakeCM(FakeBrowser()), allow_private_hosts=True)
    try:
        assert wait_until(lambda: solver.health()[0] == 200)
        primary = FakeBackend("camoufox", error=SolveError(ErrorCode.UNSOLVED, "no", stage="wait"))
        fallback = FakeBackend("flaresolverr", result=OK_RESULT)
        svc.Handler.solver = solver
        svc.Handler.token = "t"
        svc.Handler.router = be.BackendRouter([primary, fallback])

        srv = svc.Server(("127.0.0.1", 0), svc.Handler)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            import urllib.request

            req = urllib.request.Request(
                base + "/solve",
                data=json.dumps({"url": "https://example.com/", "timeout": 10}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer t"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                body = json.loads(r.read().decode())
            assert r.status == 200
            assert body["backend"] == "flaresolverr"
            assert body["attempts"][0]["backend"] == "camoufox"
        finally:
            srv.shutdown()
            srv.server_close()
            svc.Handler.router = None
    finally:
        solver.close()


# ── 终止性错误不得 fallback（Critic 抓到的 MAJOR）──────────────
@pytest.mark.parametrize("code", [ErrorCode.INVALID_REQUEST, ErrorCode.UNAUTHORIZED,
                                  ErrorCode.BUSY, ErrorCode.RATE_LIMITED])
def test_router_does_not_fallback_on_terminal_errors(code):
    """参数错/未鉴权/队列满换后端也没用：继续试等于绕过 SSRF 守卫与背压。"""
    primary = FakeBackend("camoufox", error=SolveError(code, "terminal", stage="validate"))
    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    with pytest.raises(SolveError) as ei:
        be.BackendRouter([primary, fallback]).solve("https://e.com/", 10)
    assert ei.value.code is code
    assert fallback.calls == 0, "终止性错误不允许落到下一个后端"
    assert ei.value.payload()["attempts"][0]["error_code"] == code.value


def test_router_still_falls_back_on_retryable_errors():
    primary = FakeBackend("camoufox", error=SolveError(ErrorCode.UNSOLVED, "no", stage="wait"))
    fallback = FakeBackend("flaresolverr", result=OK_RESULT)
    out = be.BackendRouter([primary, fallback]).solve("https://e.com/", 10)
    assert out["backend"] == "flaresolverr"


# ── 兜底后端的响应必须满足 API 契约（Critic 抓到的 ⑧）─────────
def test_flaresolverr_backend_returns_egress_and_ttl():
    srv, base = _serve(lambda body: (200, FLARE_OK))
    try:
        out = be.FlareSolverrBackend(base, egress_id="fallback-px").solve("https://e.com/", 30)
        assert out["egress_id"] == "fallback-px", "缓存键需要出口标识，兜底后端也必须回传"
        assert out["expires_in"] > 0 and out["expires_at"] > 0, \
            "expires_at=0 会让 Go 侧立刻判过期，导致每次回放都重新求解"
        import time as _t

        assert out["expires_at"] > _t.time()
    finally:
        srv.shutdown()
        srv.server_close()
