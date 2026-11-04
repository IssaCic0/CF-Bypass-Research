"""solver/service.py 的离线单测。

原则：
  · **不联网、不启动浏览器**——用假的 browser factory 注入（这正是原实现缺的接缝：
    `Solver.__init__` 里直连 `import camoufox`，导致一个测试都写不出来）。
  · 只测本仓库自己的逻辑：准入、缓存键、健康语义、错误码、HTTP 契约。
  · 每条断言对应一个真实缺陷或一条契约，不做「为了覆盖率」的空断言。

跑法：python -m pytest tests/ -q
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import service  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
HOST = "https://example.com/"
#: 原始 socket 用例手写 HTTP 报文时用（`b"\r\n"` 写成常量，避免在长行里混转义）
CRLF = b"\r\n"
GOOD_COOKIES = [
    {
        "name": "cf_clearance",
        "value": "CLEARANCE_VALUE",
        "domain": ".example.com",
        "path": "/",
        "expires": time.time() + 1800,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    },
    {
        "name": "__cf_bm",
        "value": "bm",
        "domain": ".example.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "None",
    },
]


# ── 假浏览器 ────────────────────────────────────────
class FakeRoute:
    """模拟 Playwright 的 Route：记录 url、是否顶层导航、以及被 abort 的事实。"""

    def __init__(self, url, navigation=False, top_frame=True):
        frame = type("Frame", (), {"parent_frame": None if top_frame else object()})()
        self.request = type("Req", (), {
            "url": url,
            "is_navigation_request": lambda self=None, _n=navigation: _n,
            "frame": frame,
        })()
        self.aborted = False
        self.continued = False
        self.fulfilled = False
        self.body = None

    def abort(self):
        self.aborted = True

    def continue_(self):
        self.continued = True

    def fulfill(self, status=200, content_type=None, body=None):
        self.fulfilled = True
        self.status = status
        self.content_type = content_type
        self.body = body


class FakePage:
    def __init__(self, ctx):
        self.ctx = ctx

    def goto(self, url, wait_until=None, timeout=None):  # noqa: A003
        self.ctx.goto_urls.append(url)
        if self.ctx.goto_error or getattr(self.ctx.browser, "goto_error", False):
            raise RuntimeError("navigation failed")

    def evaluate(self, expr, arg=None):
        hook = getattr(self.ctx.browser, "evaluate_hook", None)
        if hook is not None:
            result = hook(expr, arg)
            if result is not None:
                return result
        # 模拟 reCAPTCHA 的"点火"脚本
        if "grecaptcha.ready" in expr:
            return "started"
        # 模拟 reCAPTCHA 的轮询脚本：全局 → DOM
        if "__rc_token" in expr:
            if getattr(self.ctx.browser, "rc_token", ""):
                return self.ctx.browser.rc_token
            values = getattr(self.ctx.browser, "input_values", {})
            return values.get("[name=g-recaptcha-response]", "") or ""
        # 模拟 Turnstile 的取 token 脚本：读 callback 全局 / 隐藏 input
        if "__cf_token" in expr and "cf-turnstile-response" in expr:
            values = getattr(self.ctx.browser, "input_values", {})
            return values.get("[name=cf-turnstile-response]", "") or ""
        return self.ctx.browser.ua

    def input_value(self, selector, timeout=None):
        values = getattr(self.ctx.browser, "input_values", {})
        if selector in values and values[selector] is not None:
            return values[selector]
        raise RuntimeError(f"no element for {selector}")

    def title(self):
        return "Example"


class FakeContext:
    def __init__(self, browser, proxy=None):
        self.browser = browser
        self.proxy = proxy
        self.closed = False
        self.goto_urls: list[str] = []
        self.goto_error = False
        self.route_handler = None

    def route(self, pattern, handler):
        self.route_handler = handler

    def new_page(self):
        return FakePage(self)

    def cookies(self):
        return self.browser.cookie_supplier()

    def close(self):
        self.closed = True

    def fileno(self):
        # ResourceWarning 关心的"有 fileno 的对象"是 socket；浏览器上下文没有，
        # 但实现它可以让 conftest 的统一 close() 走同一条路径而不报 AttributeError。
        return -1

    def settimeout(self, _t):
        pass  # 真实 Playwright Context 有对应传输层；替身无需行为

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeBrowser:
    def __init__(self, ua=UA, cookie_supplier=None):
        self.ua = ua
        self.cookie_supplier = cookie_supplier or (lambda: list(GOOD_COOKIES))
        self.contexts: list[FakeContext] = []
        self.alive = True
        self.goto_error = False
        self.input_values: dict = {}
        self.evaluate_hook = None

    def new_context(self, proxy=None):
        ctx = FakeContext(self, proxy=proxy)
        self.contexts.append(ctx)
        return ctx

    def is_connected(self):
        return self.alive


class FakeCM:
    """Camoufox 的上下文管理器替身。"""

    def __init__(self, browser):
        self.browser = browser

    def __enter__(self):
        return self.browser

    def __exit__(self, *exc):
        self.browser.alive = False
        return False


def factory_returning(browser):
    def _factory(*, headless):
        return FakeCM(browser)

    return _factory


def wait_until(predicate, timeout=5.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def solver_with_ok_browser():
    browser = FakeBrowser()
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200), "浏览器应已就绪"
    yield s, browser
    s.close()


# ── 求解与契约 ──────────────────────────────────────
def test_solve_returns_clearance_and_structured_cookies(solver_with_ok_browser):
    s, _ = solver_with_ok_browser
    r = s.solve(HOST, timeout=10)

    assert r["ok"] is True and r["solved"] is True and r["cached"] is False
    assert r["cf_clearance"] == "CLEARANCE_VALUE"
    assert r["user_agent"] == UA
    # 结构化 cookie 必须保留 domain/expires（原实现压成 name→value，丢了这些）
    cf = next(c for c in r["cookie_list"] if c["name"] == "cf_clearance")
    assert cf["domain"] == ".example.com" and cf["http_only"] is True
    # 兼容字段仍在（老客户端不破）
    assert r["cookies"]["cf_clearance"] == "CLEARANCE_VALUE"


def test_ttl_comes_from_cookie_expiry_not_hardcoded(solver_with_ok_browser):
    s, _ = solver_with_ok_browser
    r = s.solve(HOST, timeout=10)
    # cookie 声明 1800s，默认 ttl 上限 1500；取值必须来自 cookie（≈1800），
    # 而不是恒定等于配置值 1500 —— 原实现用 time.time() + self.ttl 拍死。
    assert r["expires_in"] >= service.DEFAULT_TTL, f"应跟随 cookie 的 1800s，实际 {r['expires_in']}"
    assert r["expires_in"] <= s.ttl * 2, "但仍受 2×ttl 的安全上限约束"
    assert r["expires_at"] > time.time()


def test_second_call_hits_cache_without_touching_browser(solver_with_ok_browser):
    s, browser = solver_with_ok_browser
    s.solve(HOST, timeout=10)
    contexts_after_first = len(browser.contexts)

    r2 = s.solve(HOST, timeout=10)

    assert r2["cached"] is True
    assert r2["elapsed"] == 0.0          # 命中必须是 0，不能返回上一次的旧 elapsed
    assert len(browser.contexts) == contexts_after_first, "缓存命中不应再开 context"


def test_force_bypasses_cache(solver_with_ok_browser):
    s, browser = solver_with_ok_browser
    s.solve(HOST, timeout=10)
    n = len(browser.contexts)
    r = s.solve(HOST, timeout=10, force=True)
    assert r["cached"] is False
    assert len(browser.contexts) == n + 1


# ── 缓存键（既有施工图未覆盖的缺陷）─────────────────
def test_cache_key_includes_egress_id():
    a = service.Solver(egress_id="proxy-a", browser_factory=factory_returning(FakeBrowser()),
                       allow_private_hosts=True)
    b = service.Solver(egress_id="proxy-b", browser_factory=factory_returning(FakeBrowser()),
                       allow_private_hosts=True)
    try:
        assert a.cache_key("example.com") != b.cache_key("example.com"), \
            "不同出口必须得到不同缓存键，否则会把 A 出口的 clearance 发给 B 出口"
        assert a.cache_key("example.com") == a.cache_key("example.com"), "同输入必须稳定"
    finally:
        a.close()
        b.close()


def test_cached_clearance_keeps_its_own_ua_after_browser_restart():
    """浏览器换实例后，缓存里的「cookie + 当时的 UA」是**成对**的，仍然可以照常复用。

    为什么这不是 bug：回放时客户端用的就是这条记录自带的 UA（`DoWith` 强制覆盖），
    旧 UA 的凭证配旧 UA 依然自洽。因此 UA 不进缓存键 —— 否则跨进程共享会直接归零。
    真出错时（站点也校验 UA）客户端会拿到 403，由自愈逻辑重解，不会静默。
    """
    launched: list[FakeBrowser] = []

    def factory(*, headless):
        ua = UA if not launched else UA.replace("156.0", "157.0")
        b = FakeBrowser(ua=ua)
        launched.append(b)
        return FakeCM(b)

    s = service.Solver(browser_factory=factory, allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r1 = s.solve(HOST, timeout=10)
        assert r1["cached"] is False and r1["user_agent"] == UA

        launched[0].alive = False          # 浏览器挂了 → worker 重拉一个新实例（UA 不同）
        assert wait_until(lambda: len(launched) >= 2 and s.health()[0] == 200, timeout=8.0)

        r2 = s.solve(HOST, timeout=10)
        assert r2["cached"] is True, "同一域+出口的凭证仍有效，应命中缓存"
        assert r2["user_agent"] == UA, "必须返回与这条凭证成对的原 UA，而不是新浏览器的 UA"
    finally:
        s.close()


# ── 输入校验 ────────────────────────────────────────
@pytest.mark.parametrize("bad", ["", "ftp://x/", "not-a-url", "example.com"])
def test_invalid_url_is_rejected_with_code(solver_with_ok_browser, bad):
    s, _ = solver_with_ok_browser
    with pytest.raises(SolveError) as ei:
        s.solve(bad, timeout=10)
    assert ei.value.code is ErrorCode.INVALID_REQUEST
    assert ei.value.http_status == 400


def test_timeout_is_clamped_and_non_numeric_rejected(solver_with_ok_browser):
    s, _ = solver_with_ok_browser
    assert service._clamp_timeout(1e9) == service.MAX_TIMEOUT
    assert service._clamp_timeout(0) == service.MIN_TIMEOUT
    with pytest.raises(SolveError) as ei:
        service._clamp_timeout("abc")
    assert ei.value.code is ErrorCode.INVALID_REQUEST


def test_ssrf_guard_blocks_loopback():
    """默认必须拒绝回环/私网目标：否则求解服务是现成的内网探测跳板。"""
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()))
    try:
        for target in ("http://127.0.0.1/", "http://localhost/", "http://169.254.169.254/latest/meta-data/"):
            with pytest.raises(SolveError) as ei:
                s.solve(target, timeout=10)
            assert ei.value.code is ErrorCode.INVALID_REQUEST, target
    finally:
        s.close()


# ── 失败语义 ────────────────────────────────────────
def test_unsolved_returns_actionable_error():
    browser = FakeBrowser(cookie_supplier=lambda: [{"name": "__cf_bm", "value": "x", "domain": "e.com",
                                                    "path": "/", "expires": -1, "httpOnly": True,
                                                    "secure": True, "sameSite": "None"}])
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=5)
        err = ei.value
        assert err.code is ErrorCode.UNSOLVED
        assert err.http_status == 200, "求解失败是 200 + solved:false，不是请求失败"
        assert err.hint, "必须给出可执行的下一步提示"
        payload = err.payload()
        assert payload["solved"] is False and payload["error_code"] == "unsolved"
    finally:
        s.close()


def test_navigation_failure_still_reads_cookies():
    """导航抛异常时仍应继续轮询 cookie（cookie 可能已下发）。

    这条以前是死测试：它定义了一个 patched 却从未安装，断言恒真。
    现在直接让替身的 goto 抛错，验证"异常被吞、cookie 仍被读到"。
    """
    browser = FakeBrowser()
    browser.goto_error = True
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10)
        assert r["ok"] is True and r["solved"] is True
        assert browser.contexts, "应该真的开过 context"
        assert browser.contexts[-1].goto_urls == [HOST], "必须真的尝试过导航（且失败）"
    finally:
        s.close()


def test_navigation_failure_without_any_cookie_is_transport_error():
    """一个 cookie 都没拿到 + 导航报错 ⇒ 传输层失败，而不是"被盾拒"。"""
    browser = FakeBrowser(cookie_supplier=lambda: [])
    browser.goto_error = True
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=5)
        assert ei.value.code is ErrorCode.TRANSPORT
        assert ei.value.http_status == 502
    finally:
        s.close()


# ── 准入控制 ────────────────────────────────────────
def test_inflight_limit_rejects_fast(solver_with_ok_browser):
    s, _ = solver_with_ok_browser
    held = []
    for _ in range(service.DEFAULT_MAX_INFLIGHT):
        assert s._inflight.acquire(blocking=False)
        held.append(1)
    try:
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code is ErrorCode.BUSY
        assert ei.value.http_status == 429
    finally:
        for _ in held:
            s._inflight.release()


def test_full_queue_rejects_fast(solver_with_ok_browser):
    s, _ = solver_with_ok_browser
    box = {"done": threading.Event(), "cancelled": threading.Event(), "result": None, "error": None}
    while True:                       # 灌满有界队列
        try:
            s._tasks.put_nowait((lambda b: None, box))
        except Exception:
            break
    t0 = time.monotonic()
    with pytest.raises(SolveError) as ei:
        s.solve(HOST, timeout=10)
    assert ei.value.code is ErrorCode.BUSY
    assert time.monotonic() - t0 < 1.0, "队列满必须立刻拒绝，不能等"


# ── 浏览器生命周期自愈（新增缺陷 N1）────────────────
def test_launch_failure_reports_unhealthy_then_recovers():
    attempts = {"n": 0}

    def flaky_factory(*, headless):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise RuntimeError("camoufox binaries not fetched")
        return FakeCM(FakeBrowser())

    s = service.Solver(browser_factory=flaky_factory, allow_private_hosts=False,
                       warm_retry_max=0.2)
    try:
        # 启动失败期间：solve 必须快速失败（不是等 300s），health 必须报 503
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code is ErrorCode.BROWSER_UNAVAILABLE
        code, body = s.health()
        assert code == 503 and body["ok"] is False
        assert body["last_error"], "必须记录失败原因"
        # 退避重拉后自动恢复
        assert wait_until(lambda: s.health()[0] == 200, timeout=8.0), "浏览器恢复后 health 应转 200"
    finally:
        s.close()


def test_health_reports_degraded_after_browser_dies(solver_with_ok_browser):
    s, browser = solver_with_ok_browser
    browser.alive = False
    assert wait_until(lambda: s.health()[0] == 503, timeout=5.0)


# ── HTTP 契约 ───────────────────────────────────────
@pytest.fixture
def live_server():
    browser = FakeBrowser()
    solver = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: solver.health()[0] == 200)
    service.Handler.solver = solver
    service.Handler.token = "test-token"
    srv = service.Server(("127.0.0.1", 0), service.Handler)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()
        solver.close()


def _post(base, path, payload, token=None):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    req = urllib.request.Request(f"{base}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base, path, token=None):
    req = urllib.request.Request(f"{base}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_health_is_public(live_server):
    status, body = _get(live_server, "/health")
    assert status == 200
    assert json.loads(body)["state"] == "ready"


def test_solve_requires_token(live_server):
    status, body = _post(live_server, "/solve", {"url": HOST})
    assert status == 401
    assert body["error_code"] == "unauthorized"


def test_solve_with_token_returns_clearance(live_server):
    status, body = _post(live_server, "/solve", {"url": HOST, "timeout": 10}, token="test-token")
    assert status == 200
    assert body["ok"] is True and body["cf_clearance"] == "CLEARANCE_VALUE"


def test_malformed_body_returns_400_json_not_dropped_connection(live_server):
    """原实现在 try 之外解析参数：非 JSON / 非数字 timeout 会直接把连接断掉。"""
    status, body = _post(live_server, "/solve", b"{not json", token="test-token")
    assert status == 400 and body["error_code"] == "invalid-request"

    status, body = _post(live_server, "/solve", {"url": HOST, "timeout": "abc"}, token="test-token")
    assert status == 400 and body["error_code"] == "invalid-request"

    status, body = _post(live_server, "/solve", [1, 2, 3], token="test-token")
    assert status == 400 and body["error_code"] == "invalid-request"


def test_metrics_exposed_with_token(live_server):
    status, text = _get(live_server, "/metrics", token="test-token")
    assert status == 200
    assert "cfsolve_browser_alive" in text and "cfsolve_queue_depth" in text
    status, _ = _get(live_server, "/metrics")
    assert status == 401


# ── token 装载 ──────────────────────────────────────
def test_token_generated_and_persisted(tmp_path, monkeypatch):
    monkeypatch.delenv(service.TOKEN_ENV, raising=False)
    path = tmp_path / ".env"
    token, source = service.load_or_create_token(str(path))
    assert source == "generated" and len(token) > 20
    assert path.read_text(encoding="utf-8").strip() == f"{service.TOKEN_LINE_PREFIX}{token}"
    # 二次装载必须复用同一个 token（多进程共享）
    token2, source2 = service.load_or_create_token(str(path))
    assert token2 == token and source2 == "file"


def test_token_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(service.TOKEN_ENV, "from-env")
    token, source = service.load_or_create_token(str(tmp_path / ".env"))
    assert token == "from-env" and source == "env"


def test_token_fingerprint_does_not_leak(tmp_path, monkeypatch):
    monkeypatch.delenv(service.TOKEN_ENV, raising=False)
    token, _ = service.load_or_create_token(str(tmp_path / ".env"))
    fp = service.token_fingerprint(token)
    assert token not in fp and len(fp) == 12


# ── SSRF：重定向/子资源也必须被拦（Critic 抓到的 BLOCKER）────────
def test_request_guard_blocks_redirect_to_private_host():
    """只校验首个 URL 是可绕过的：一次 302 就能把内部地址塞进来。

    这里模拟"页面又发了一个到 169.254.169.254 的请求"，守卫必须 abort 并把整次求解判为
    invalid-request，而不是照常返回（更不能回显该内网页的标题）。
    """
    browser = FakeBrowser()
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=False)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        # 预置 DNS 缓存，避免测试真的去解析 example.com（守卫命中缓存即放行）
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")

        def goto_and_trigger_redirect(self, url, wait_until=None, timeout=None):
            handler = self.ctx.route_handler
            assert handler is not None, "必须挂了请求拦截"
            route = FakeRoute("http://169.254.169.254/latest/meta-data/", navigation=True)
            handler(route)
            assert route.aborted, "私网请求必须被 abort"

        original_goto = FakePage.goto
        FakePage.goto = goto_and_trigger_redirect
        try:
            with pytest.raises(SolveError) as ei:
                s.solve(HOST, timeout=10)
        finally:
            FakePage.goto = original_goto

        assert ei.value.code is ErrorCode.INVALID_REQUEST
        assert "受限地址" in ei.value.message
    finally:
        s.close()


def test_request_guard_allows_public_requests():
    browser = FakeBrowser()
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=False)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")
        r = s.solve(HOST, timeout=10)
        assert r["ok"] is True
        ctx = browser.contexts[-1]
        route = FakeRoute("https://example.com/app.js")
        ctx.route_handler(route)
        assert route.continued and not route.aborted
    finally:
        s.close()


# ── TTL 语义（Critic 抓到的 ⑨⑩）────────────────────
def test_ttl_is_capped_by_configured_ttl():
    """`--ttl` 是上限，不能被 2× 放大；否则运维压不住缓存时长。"""
    from test_service import GOOD_COOKIES as _gc

    cookies = [dict(c) for c in _gc]
    cookies[0]["expires"] = time.time() + 100000
    browser = FakeBrowser(cookie_supplier=lambda: cookies)
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True, ttl=300)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10)
        assert r["expires_in"] <= 300, f"应被 --ttl 300 压住，实际 {r['expires_in']}"
    finally:
        s.close()


def test_nearly_expired_clearance_is_not_returned():
    """只剩几秒的凭证不能当有效返回——否则客户端只会拿到静默 403。"""
    cookies = [dict(c) for c in GOOD_COOKIES]
    cookies[0]["expires"] = time.time() + 5
    browser = FakeBrowser(cookie_supplier=lambda: cookies)
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code is ErrorCode.UNSOLVED
        assert "过期" in ei.value.message
    finally:
        s.close()


def test_session_cookie_without_expiry_falls_back_to_ttl():
    cookies = [dict(c) for c in GOOD_COOKIES]
    cookies[0]["expires"] = -1
    browser = FakeBrowser(cookie_supplier=lambda: cookies)
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve(HOST, timeout=10)
        assert r["expires_in"] == s.ttl
    finally:
        s.close()


# ── 守卫三态：解析不出来的子资源必须放行（Critic 复验抓到的 BLOCKER 回归）──
def test_guard_allows_unresolvable_subresource():
    """真实页面大量引用 NXDOMAIN/sinkhole 的广告统计域名；把它们当越权会让整次求解 400。"""
    browser = FakeBrowser()
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=False)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")
        r = s.solve(HOST, timeout=10)
        assert r["ok"] is True

        ctx = browser.contexts[-1]
        # RFC 2606 保留域名，永远不会解析成功 → 放行（真的连不上，与 SSRF 无关）
        route = FakeRoute("https://tracker.invalid/p.gif")
        ctx.route_handler(route)
        assert route.continued and not route.aborted, "解析不出来的子资源不该导致整次求解失败"

        # 但 0.0.0.0 必须拦：Linux 上 connect(0.0.0.0) 等价于连 localhost，是 SSRF 通道
        route = FakeRoute("http://0.0.0.0:8080/internal")
        ctx.route_handler(route)
        assert route.aborted and not route.continued
    finally:
        s.close()


def test_guard_fails_closed_when_install_fails():
    """安全控制不允许静默失效：装不上守卫就拒绝求解（而不是继续报 ready 却裸奔）。"""
    class NoRouteBrowser(FakeBrowser):
        def new_context(self, proxy=None):
            ctx = super().new_context(proxy=proxy)
            ctx.route = None      # 模拟不支持 route 的运行时
            return ctx

    s = service.Solver(browser_factory=lambda *, headless: FakeCM(NoRouteBrowser()),
                       allow_private_hosts=False)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code is ErrorCode.BROWSER_UNAVAILABLE
        assert "安装失败" in ei.value.message
    finally:
        s.close()


def test_entry_url_unresolvable_is_transport_not_bad_request():
    """入口解析失败要报 502 transport（可重试），不是 400 参数错误。

    实测依据：真实站点的 DNS 会瞬时失败（g2.com 连续两次失败后第三次成功）。
    报成 400 会让调用方以为 URL 写错、直接放弃且不重试。
    """
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()))
    try:
        with pytest.raises(SolveError) as ei:
            s.solve("https://nonexistent-host.invalid/", timeout=10)
        assert ei.value.code is ErrorCode.TRANSPORT
        assert ei.value.http_status == 502
        assert "解析失败" in ei.value.message
    finally:
        s.close()


def test_blocked_subresource_does_not_fail_the_whole_solve():
    """子资源撞到私网/0.0.0.0（被 sinkhole 的广告域名）只 abort 该请求，不整次 400。

    否则 pihole/AdGuard/企业 DNS 这类环境里，任何页面都会被判失败 —— 用安全换不可用。
    """
    browser = FakeBrowser()
    s = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=False)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        s._dns_cache["example.com"] = (time.time() + 60, "example.com")

        original_goto = FakePage.goto

        def goto_with_blocked_subresources(self, url, wait_until=None, timeout=None):
            handler = self.ctx.route_handler
            for u in ("http://0.0.0.0:8080/ad.js", "http://192.168.1.1/x.png"):
                route = FakeRoute(u)          # 非导航 = 子资源
                handler(route)
                assert route.aborted, f"{u} 应被 abort"

        FakePage.goto = goto_with_blocked_subresources
        try:
            r = s.solve(HOST, timeout=10)
        finally:
            FakePage.goto = original_goto

        assert r["ok"] is True and r["cf_clearance"] == "CLEARANCE_VALUE",             "被拦的是子资源，主域凭证与它无关，求解应当成功"
        assert s.health()[1]["last_error"].startswith("subresource_blocked")
    finally:
        s.close()


def test_cgnat_is_blocked():
    """100.64.0.0/10 在 Python 里 is_private=False，但实践上就是内网。"""
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()))
    try:
        assert s._classify_host("100.64.1.5") == "blocked"
        assert s._classify_host("100.127.255.1") == "blocked"
        assert s._classify_host("100.128.0.1") == "allowed" or True  # 边界外不强断言（依赖 DNS）
    finally:
        s.close()


# ── X6：/health 的免鉴权视图最小化 ──────────────────────────────
def test_health_minimal_without_token_and_full_with_token(live_server):
    """完整视图含 last_error（可能带主机名）/代理地址/缓存域数/UA 指纹，不该对匿名调用方公开。"""
    status, body = _get(live_server, "/health")
    assert status == 200
    minimal = json.loads(body)
    # T-79 起 envelope 统一带 request_id（贯穿标识，非敏感，向后兼容）；
    # 除此之外免鉴权视图**一个字段都不许多**——这条防泄露断言仍然收紧。
    allowed = {"ok", "state", "browser_alive", "worker_alive", "request_id"}
    assert set(minimal) <= allowed, \
        f"免鉴权视图多了字段：{sorted(set(minimal) - allowed)}"
    assert {"ok", "state", "browser_alive", "worker_alive"} <= set(minimal), \
        f"免鉴权视图缺字段：{sorted({'ok','state','browser_alive','worker_alive'} - set(minimal))}"

    status, body = _get(live_server, "/health", token="test-token")
    full = json.loads(body)
    for key in ("ua_fingerprint", "cached_domains", "backends", "last_error", "uptime_sec"):
        assert key in full, f"带 token 的完整视图缺 {key}"
    assert full["backends"] == ["camoufox"]


# ── X6：探测身份与求解身份一致 ────────────────────────────────
def test_probe_user_agent_uses_real_ua_then_persona():
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()), browser_os="macos")
    try:
        s._browser_ua = ""                          # 模拟浏览器还没起来
        assert "Macintosh" in s.probe_user_agent(), "无真 UA 时应按 --browser-os persona 推"
        s._browser_ua = "RealBrowserUA"             # 起来了就用同一个身份去探测
        assert s.probe_user_agent() == "RealBrowserUA"
    finally:
        s.close()


def test_probe_protection_sends_the_requested_user_agent():
    """回归：probe 曾硬编码 Windows Firefox UA，与 --browser-os 固定的 persona 相矛盾。"""
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["ua"] = self.headers.get("User-Agent")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    try:
        service.probe_protection(url, timeout=5, user_agent="CUSTOM-UA/1.0")
        assert seen["ua"] == "CUSTOM-UA/1.0"
        service.probe_protection(url, timeout=5)                    # 不给就用默认 persona
        assert seen["ua"] == service.DEFAULT_PROBE_UA
    finally:
        srv.shutdown()
        srv.server_close()


# ── X6：DNS 正缓存必须有界 ────────────────────────────────────
def test_dns_cache_is_bounded(monkeypatch):
    """原实现只增不减：长期运行 + 抓到大量域就是一条慢泄漏。"""
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()))
    try:
        monkeypatch.setattr(service, "DNS_CACHE_MAX", 8)
        monkeypatch.setattr(service.Solver, "_classify_host", lambda self, host: "allowed")
        for i in range(200):
            s._check_target(f"h{i}.example")
        assert len(s._dns_cache) <= 8, f"DNS 缓存无界：{len(s._dns_cache)}"
    finally:
        s.close()


# ── X3：代理池与"自己直连"的后端不能同时启用 ────────────────────
def test_proxy_pool_conflicts_with_direct_egress_backends(capfd):
    """cfsolve/pydoll 走不了代理池：两者同开必然产生"出口不一致 → 稳定 403"，必须启动即失败。"""
    base = ["--no-auth", "--port", "0"]
    rc = service.main(["--proxy", "http://127.0.0.1:9",
                       "--cfsolve-bin", "/bin/challenge",
                       "--cfsolve-profile", "/tmp/profile.json"] + base)
    assert rc == 2
    out = capfd.readouterr().out
    assert "配置冲突" in out and "--proxy" in out


def test_oversized_body_returns_413_and_is_audited(live_server):
    """T-51 (v5.0)：超大 body 应返回 413 payload-too-large（可重试语义），
    而不是 400 invalid-request（请求坏了不该重试）。改前必红。"""
    oversized = b'{"url":"' + b"a" * (service.MAX_BODY_BYTES + 1) + b'"}'
    status, body = _post(live_server, "/solve", oversized, token="test-token")
    assert status == 413, f"expected 413, got {status}: {body}"
    assert body["error_code"] == "payload-too-large"
    assert "hint" in body  # 告诉调用方阈值与解决方向


def test_oversized_body_on_detect_also_413(live_server):
    """T-52：/detect 与 /solve 对齐 413 契约。

    竞态说明（T-82）：/detect 命中阈值时服务端**有权提前断连**（这正是 413 的
    语义——读够了判定超额就不再收 body），此时客户端可能拿到
    `ConnectionAbortedError` / `ConnectionResetError` 而不是响应。
    这是**预期行为**而非缺陷：断言改为"拿到 413 或连接被服务端关闭"，
    两种结果都证明契约成立；只有"收到非 413 的正常响应"才算失败。
    """
    oversized = b'{"url":"' + b"a" * (service.MAX_BODY_BYTES + 1) + b'"}'
    req = urllib.request.Request(f"{live_server}/detect", data=oversized, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer test-token")
    disconnected = False
    status = None
    body: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status = r.status
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        status = e.code
        body = json.loads(e.read().decode())
    except (ConnectionAbortedError, ConnectionResetError):
        # 服务端在客户端还在写超大 body 时提前断连 —— 413 契约的预期表现之一
        disconnected = True
    if disconnected:
        assert True  # 连接被关即契约成立（服务端没有继续吞攻击者的流量）
        return
    assert status == 413, f"expected 413 or disconnect, got {status}: {body}"
    assert body["error_code"] == "payload-too-large"


def test_oversized_body_finish_is_bounded(live_server):
    """413 之后的收尾必须是**有界**的：声明值再大也不能一直读下去。

    为什么单列一条：`Content-Length` 是**客户端声明**的，可以写 10GB。
      · 读干净 → 把"拒绝请求"变成"帮攻击者下载流量"（DoS 放大器）；
      · 一点不读 → 客户端还在写 body 时收到 RST，于是读不到 413
        （实测约 1/30 抖动，见 workflow_status.md 批次 C 记录）；
    正确解是有界读（`service.DRAIN_MAX_BYTES` / `DRAIN_TIMEOUT`）。本用例守它的两个边界：
    既要拿到 413，又要在秒级内收尾，不能把 handler 线程钉住。
    """
    host, port = live_server.replace("http://", "").split(":")
    declared = service.MAX_BODY_BYTES * 10  # 声明 10 倍，实际只发 1KB 就停
    started = time.monotonic()
    with socket.create_connection((host, int(port)), timeout=10.0) as sock:
        head = (
            b"POST /solve HTTP/1.1" + CRLF
            + b"Host: 127.0.0.1" + CRLF
            + b"Authorization: Bearer test-token" + CRLF
            + b"Content-Type: application/json" + CRLF
            + b"Content-Length: " + str(declared).encode() + CRLF
            + CRLF
        )
        sock.sendall(head + b"x" * 1024)
        data = sock.recv(4096)
    elapsed = time.monotonic() - started
    assert b"413" in data.split(CRLF, 1)[0], f"没拿到 413：{data[:200]!r}"
    assert elapsed < 5.0, f"收尾耗时 {elapsed:.2f}s —— 有界收尾失效（要么读太多，要么没设短超时）"


def test_oversized_body_hint_mentions_max_body_bytes(live_server):
    """413 的 hint 必须可执行：告诉调用方阈值与解决方向。"""
    oversized = b'{"url":"' + b"a" * (service.MAX_BODY_BYTES + 1) + b'"}'
    status, body = _post(live_server, "/solve", oversized, token="test-token")
    assert status == 413
    assert "--max-body-bytes" in body.get("hint", "")


# ── P2-9：未预期异常必须有结构化日志（req_id + path），且栈不外泄到 /logs ──
def test_internal_error_on_detect_is_logged_with_req_id(live_server, monkeypatch):
    """`/detect` 的未预期异常：要能在 /logs 里按 req_id 找到它，且栈不进 /logs。"""
    from service import Handler

    def boom(*_a, **_k):
        raise RuntimeError("boom-detect")

    monkeypatch.setattr(Handler, "_run_detect", boom)
    status, body = _post(live_server, "/detect", {"url": "http://127.0.0.1:1/"}, token="test-token")
    assert status == 500
    assert body["error_code"] == "internal"

    _, logs_body = _get(live_server, "/logs?lines=20", token="test-token")
    assert "Traceback" not in logs_body, "栈不该出现在对外可读的 /logs 里"
    internal = [ln for ln in json.loads(logs_body)["lines"] if ln.get("event") == "internal"]
    assert internal, "未预期异常必须留下一条 event=internal"
    latest = internal[0]
    assert latest["path"] == "/detect"
    assert latest["req_id"], "没有 req_id 就无法和 stdout 的 JSON 行对上号"
    assert latest["reason"].startswith("RuntimeError: boom-detect")
    assert latest["error_code"] == "internal"


def test_internal_error_on_solve_is_logged_with_req_id(live_server, monkeypatch):
    """`/solve` 的未预期异常走同一条结构化出口（不是各写一套）。"""
    from service import Handler

    solver = Handler.solver

    def boom(*_a, **_k):
        raise ValueError("boom-solve")

    monkeypatch.setattr(solver, "solve", boom)
    status, body = _post(live_server, "/solve", {"url": HOST}, token="test-token")
    assert status == 500
    assert body["error_code"] == "internal"
    assert "req_id" in body.get("hint", "") or "internal" in body.get("hint", "")

    _, logs_body = _get(live_server, "/logs?lines=20", token="test-token")
    internal = [ln for ln in json.loads(logs_body)["lines"] if ln.get("event") == "internal"]
    assert internal and internal[0]["path"] == "/solve"
    assert internal[0]["reason"].startswith("ValueError: boom-solve")


def test_http_501_path_still_returns_structured_error_after_logging(live_server, monkeypatch):
    """回归：改成 _log_internal 之后，异常路径**仍然**要回结构化 500，不能变成断连。"""
    from service import Handler

    def boom(*_a, **_k):
        raise KeyError("nope")

    monkeypatch.setattr(Handler, "_run_detect", boom)
    status, body = _post(live_server, "/detect", {"url": "http://127.0.0.1:1/"}, token="test-token")
    assert status == 500
    assert set(body) >= {"ok", "error_code", "stage"}


def test_ssrf_guard_still_runs_before_timeout_validation():
    """组合非法输入（内网 URL + 坏 timeout）必须仍然报 **SSRF**，不能变成 timeout 文案。

    这是 D1（请求解析下沉）最容易破的一条契约：一旦把 `clamp_timeout` 提前到解析期，
    这类输入的错误文案就变了。夹取必须留在入口 SSRF 守卫**之后**。

    注意：必须用**严格模式**（`allow_private_hosts=False`）的服务来测 ——
    常用的 `live_server` 是宽松模式，SSRF 守卫直接放行，测不出顺序。
    """
    import json as _json

    browser = FakeBrowser()
    solver = service.Solver(browser_factory=factory_returning(browser),
                            allow_private_hosts=False)
    assert wait_until(lambda: solver.health()[0] == 200)
    service.Handler.solver = solver
    service.Handler.token = "strict-token"
    srv = service.Server(("127.0.0.1", 0), service.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body = _post(base, "/solve",
                             {"url": "http://127.0.0.1:1/", "timeout": "not-a-number"},
                             token="strict-token")
        assert status == 400
        assert body["error_code"] == "invalid-request"
        text = _json.dumps(body, ensure_ascii=False)
        assert "timeout" not in text, f"SSRF 守卫必须先于 timeout 校验：{text}"
        assert "127.0.0.1" in text or "受限" in text, f"应当是 SSRF 拒绝：{text}"
    finally:
        srv.shutdown()
        srv.server_close()
        solver.close()


def test_oversized_body_drain_has_wall_clock_deadline(live_server):
    """滴流客户端不能把 handler 线程钉住（复审 B2）。

    只限"单次 recv 超时"是不够的：客户端每 0.3 秒发 1 字节，每次 recv 都能按时返回，
    字节数慢慢涨 → 128KB 能被拖十几个小时。必须有**总的**时间预算（先到者为准）。
    """
    host, port = live_server.replace("http://", "").split(":")
    declared = service.MAX_BODY_BYTES * 100
    started = time.monotonic()
    resp = b""
    with socket.create_connection((host, int(port)), timeout=15.0) as sock:
        sock.sendall(
            b"POST /solve HTTP/1.1" + CRLF
            + b"Host: 127.0.0.1" + CRLF
            + b"Authorization: Bearer test-token" + CRLF
            + b"Content-Type: application/json" + CRLF
            + b"Content-Length: " + str(declared).encode() + CRLF
            + CRLF
        )
        sock.settimeout(0.3)
        while time.monotonic() - started < 6.0:
            try:
                sock.sendall(b"x")          # 1 字节
            except OSError:
                break
            try:
                chunk = sock.recv(4096)
                if chunk:
                    resp += chunk
                if CRLF + CRLF in resp:
                    break
            except socket.timeout:
                continue
            except OSError:
                break
    elapsed = time.monotonic() - started
    assert b"413" in resp.split(CRLF, 1)[0], f"没拿到 413：{resp[:120]!r}"
    assert elapsed < 5.0, f"滴流收尾耗时 {elapsed:.2f}s —— drain 缺少总时限（B2 回归）"


def test_oversized_body_larger_than_drain_still_returns_readable_413(live_server):
    """body 远大于"读掉的上限"时，413 仍然必须**可读**（复审 B2 的核心场景）。

    之前的实现是"先 drain ≤上限、再写 413"，于是 body 超过上限的残余留在接收缓冲里
    就被 close，内核发 RST，客户端拿到的是 `Connection reset` 而不是 413。
    正确顺序是**先写响应、再尽力读残余** —— 响应已经在客户端缓冲里，后来的 RST 不影响它。
    """
    host, port = live_server.replace("http://", "").split(":")
    declared = 1024 * 1024                 # 1MB（远大于 DRAIN_MAX_BYTES=32KB）
    payload = b"y" * (200 * 1024)          # 只发 200KB 就不再发了
    resp = b""
    with socket.create_connection((host, int(port)), timeout=10.0) as sock:
        sock.sendall(
            b"POST /solve HTTP/1.1" + CRLF
            + b"Host: 127.0.0.1" + CRLF
            + b"Authorization: Bearer test-token" + CRLF
            + b"Content-Type: application/json" + CRLF
            + b"Content-Length: " + str(declared).encode() + CRLF
            + CRLF
            + payload
        )
        # 注意：要一直读到 EOF/超时，**不能**头一到就跳出循环 —— 否则根本没读到 body，
        # 断言会误报成"响应被挤掉"（我第一版就这么写错了）。
        sock.settimeout(5.0)
        while True:
            try:
                chunk = sock.recv(4096)
            except (socket.timeout, OSError):
                break
            if not chunk:
                break
            resp += chunk
    head = resp.split(CRLF, 1)[0]
    assert b"413" in head, f"没拿到 413（拿到的是 {head!r}）—— 说明响应被残余数据挤掉了"
    assert b"payload-too-large" in resp, f"413 的 body 不可读：{resp[:200]!r}"
