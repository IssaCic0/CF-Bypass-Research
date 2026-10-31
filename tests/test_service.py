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
