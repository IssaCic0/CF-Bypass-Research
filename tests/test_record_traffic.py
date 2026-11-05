"""真实浏览器会话流量录制（v6.3）的纯函数契约。

为什么单测只覆盖纯函数：录制本身要起浏览器（`--real`），属于真机动作、受预算约束。
但"录什么、怎么脱敏、怎么汇总"这三件事是**纯逻辑**，而且它们错了会直接导致
凭证泄露或汇总失真 —— 必须离线锁死。

三条最容易出错的（都真的错过一次）：
  1. `headers_array` 是方法不是属性（漏调用 → Set-Cookie 全丢）
  2. `headers_array()` 返回**全部** header，不是只有 set-cookie（漏过滤 → 把
     content-length/date/server 当成 cookie 名，实测拿到 ['28','text/html','Thu, 05 Nov…']）
  3. `Cookie`/`Authorization` 只许记"有没有"，值绝不许落盘（policy §4）
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "record_traffic", ROOT / "scripts" / "record_traffic.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["record_traffic"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rt = _load()


# ── 脱敏：凭证永不落盘 ──────────────────────────────


def test_cookie_and_auth_become_presence_only():
    raw = {
        "Cookie": "__cf_bm=SECRET_VALUE; cf_clearance=TOPSECRET",
        "Authorization": "Bearer sk-live-1234567890",
        "X-Api-Key": "very-secret-key",
        "Accept": "text/html",
    }
    got = rt.sanitize_headers(raw, rt.KEEP_REQUEST_HEADERS)
    dumped = str(got)
    assert "SECRET_VALUE" not in dumped and "TOPSECRET" not in dumped
    assert "sk-live" not in dumped and "very-secret-key" not in dumped
    assert got["cookie-present"] == "yes"
    assert got["authorization-present"] == "yes"
    assert got["x-api-key-present"] == "yes"
    assert got["accept"] == "text/html"


def test_unlisted_headers_are_dropped():
    raw = {"X-Internal-Token": "leak", "Accept": "text/html"}
    got = rt.sanitize_headers(raw, rt.KEEP_REQUEST_HEADERS)
    assert "x-internal-token" not in got
    assert got == {"accept": "text/html"}


def test_long_header_value_is_truncated():
    got = rt.sanitize_headers({"Accept": "x" * 500}, rt.KEEP_REQUEST_HEADERS)
    assert len(got["accept"]) == 200


def test_headers_accept_playwright_list_shape():
    """`headers_array()` 返回的是 `[{name,value}]` —— 两种形态都要能处理。"""
    rows = [{"name": "Content-Type", "value": "text/html"},
            {"name": "Set-Cookie", "value": "a=1"}]
    got = rt.sanitize_headers(rows, rt.KEEP_RESPONSE_HEADERS)
    assert got == {"content-type": "text/html"}


# ── Set-Cookie：只记名字 ────────────────────────────


def test_set_cookie_names_only():
    rows = [
        {"name": "Set-Cookie", "value": "__cf_bm=SECRETVALUE; Path=/; HttpOnly"},
        {"name": "Set-Cookie", "value": "cf_clearance=TOPSECRET; Path=/"},
    ]
    got = rt.sanitize_set_cookie(rows)
    assert got["set_cookie_names"] == ["__cf_bm", "cf_clearance"]
    assert got["set_cookie_count"] == 2
    assert "SECRETVALUE" not in str(got) and "TOPSECRET" not in str(got)


def test_set_cookie_ignores_non_cookie_headers():
    """回归：`headers_array()` 返回**全部** header。

    第一版漏了这层过滤，把 content-length / date / server 全当 cookie 名解析出来
    （实测拿到 `['28','text/html','Thu, 05 Nov 2026…','BaseHTTP/0.6']`）。
    """
    rows = [
        {"name": "Content-Length", "value": "28"},
        {"name": "Content-Type", "value": "text/html"},
        {"name": "Date", "value": "Thu, 05 Nov 2026 03:49:52 GMT"},
        {"name": "Server", "value": "BaseHTTP/0.6 Python/3.14.3"},
        {"name": "Set-Cookie", "value": "__cf_bm=v; Path=/"},
    ]
    got = rt.sanitize_set_cookie(rows)
    assert got["set_cookie_names"] == ["__cf_bm"], got


def test_set_cookie_dedupes_by_name():
    rows = [{"name": "Set-Cookie", "value": "a=1"}, {"name": "Set-Cookie", "value": "a=2"}]
    assert rt.sanitize_set_cookie(rows)["set_cookie_names"] == ["a"]


def test_set_cookie_empty_input():
    assert rt.sanitize_set_cookie([]) == {"set_cookie_names": [], "set_cookie_count": 0}
    assert rt.sanitize_set_cookie(None) == {"set_cookie_names": [], "set_cookie_count": 0}


# ── 汇总 ────────────────────────────────────────────


def _rec(**kw):
    base = {"ts": 1761000000.0, "type": "script", "url": "https://a.test/x.js",
            "status": 200, "body_bytes": 100}
    base.update(kw)
    return base


def test_summarize_counts_by_type_status_host():
    recs = [
        _rec(type="document", url="https://a.test/", status=403, body_bytes=1000),
        _rec(type="script", url="https://a.test/cdn-cgi/challenge-platform/x.js"),
        _rec(type="script", url="https://cdn.b.test/y.js", status=200),
    ]
    s = rt.summarize(recs)
    assert s["requests"] == 3
    assert s["by_resource_type"] == {"script": 2, "document": 1}
    assert s["by_status"]["200"] == 2 and s["by_status"]["403"] == 1
    assert s["by_host"] == {"a.test": 2, "cdn.b.test": 1}
    assert s["total_body_bytes"] == 1200


def test_summarize_collects_set_cookie_names_across_requests():
    recs = [_rec(set_cookie_names=["__cf_bm"]), _rec(set_cookie_names=["cf_clearance", "__cf_bm"])]
    s = rt.summarize(recs)
    assert s["set_cookie_names"] == ["__cf_bm", "cf_clearance"]


def test_summarize_reports_failures_and_span():
    recs = [_rec(ts=100.0), _rec(ts=103.5, status=None, error="net::ERR_ABORTED")]
    s = rt.summarize(recs)
    assert s["by_status"]["failed"] == 1
    assert s["span_sec"] == 3.5
    assert s["failures"] and "ERR_ABORTED" in s["failures"][0]


def test_summarize_empty_is_safe():
    s = rt.summarize([])
    assert s["requests"] == 0 and s["by_resource_type"] == {} and s["span_sec"] == 0.0


# ── 预算护栏 ────────────────────────────────────────


def test_budget_hard_cap_rejects(capsys):
    rc = rt.main(["--url", "https://x.test/", "--out", "/tmp/x",
                  "--max-requests", str(rt.HARD_BUDGET + 1)])
    assert rc == 2
    assert "硬上限" in capsys.readouterr().err


def test_dry_run_is_default_and_makes_no_request(capsys):
    rc = rt.main(["--url", "https://x.test/", "--out", "/tmp/x"])
    out = capsys.readouterr().out
    assert rc == 0 and "[dry-run]" in out
    assert "不会发出任何网络请求" in out


# ── 预算在**重入**下仍然严格（真实踩到两次的缺陷）──────────


class _FakeReq:
    def __init__(self, url: str, rtype: str = "script") -> None:
        self.url, self.resource_type, self.method = url, rtype, "GET"
        self.headers = {"Accept": "*/*"}


class _FakeResp:
    def __init__(self, page: "_FakePage", req: _FakeReq, status: int = 200) -> None:
        self.page, self.request, self.status = page, req, status
        self.headers = {"content-type": "text/html"}

    def headers_array(self):
        return [{"name": "Set-Cookie", "value": "cf_clearance=SECRET; Path=/"}]

    def body(self) -> bytes:
        # 这就是真机上的关键一步：同步 API 的阻塞调用会切 greenlet，
        # 把排队中的 request 事件在"本条响应还没入账"的中间隙派发进来。
        self.page.pump()
        return b"x" * 16


class _FakePage:
    """按脚本回放事件；`pump()` 模拟 Playwright 的**可重入**派发。

    Playwright 的同步 API 是 greenlet 驱动的：在事件回调里调用 `resp.body()` 这类
    阻塞方法时，调度器会把**排队中的事件就地派发**（嵌套在本次回调内部），
    返回后外层继续。这个 fake 复刻的正是这一点 —— 简单版（重入直接 return）会
    让旧缺陷消失，测试就白写了。
    """

    def __init__(self, script) -> None:
        self._handlers: dict = {}
        self._queue: list = []
        self._script = script
        self._reqs: dict = {}

    def on(self, event: str, cb) -> None:
        self._handlers[event] = cb

    def goto(self, url, **_kw) -> None:
        self._queue = list(self._script(self))
        self.pump()

    def wait_for_timeout(self, _ms) -> None:
        pass

    def evaluate(self, expr):
        """只实现 `navigator.userAgent` —— record() 用它记录**实测** UA。"""
        if "userAgent" in str(expr):
            return "FakeUA/1.0"
        raise AssertionError(f"fake page 不支持的 evaluate: {expr}")

    def pump(self) -> None:
        # 可重入：内层也会真实执行排队事件（与 Playwright 的 greenlet 调度一致）
        while self._queue:
            self._queue.pop(0)()

    # 同一个 URL 复用同一个请求对象 —— 真机上 `resp.request` 与 `request` 事件
    # 拿到的是同一个 impl，`id()` 必须相等，否则 pending 永远配不上对。
    def _req(self, url, rtype="script"):
        return self._reqs.setdefault(url, _FakeReq(url, rtype))

    def emit_req(self, url, rtype="script"):
        self._handlers["request"](self._req(url, rtype))

    def emit_resp(self, url, status=200, rtype="script"):
        self._handlers["response"](_FakeResp(self, self._req(url, rtype), status))

    def emit_failed(self, url, why="net::ERR_ABORTED", rtype="script"):
        req = self._req(url, rtype)
        req.failure = why
        self._handlers["requestfailed"](req)


class _FakeCtx:
    def __init__(self, page) -> None:
        self._page = page

    def new_page(self):
        return self._page

    def close(self) -> None:
        pass


class _FakeBrowser:
    def __init__(self, page) -> None:
        self._page = page

    def new_context(self, **_kw):
        return _FakeCtx(self._page)

    def close(self) -> None:
        pass


def _patched(monkeypatch, page):
    class _CM:
        def __enter__(self):
            return None, _FakeBrowser(page)

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rt, "_open_browser", lambda engine: _CM())


def _reentrant_script(page):
    """真机上观测到的顺序（本机 chromium 打一个 1 文档 + 4 子资源的页面）。

    document 响应在 `body()` 里切 greenlet，**那时**才把子资源请求派发出去；
    子资源的响应又在这之后才回来。第二次修复失败就是因为这个顺序。
    """
    return [
        lambda: page.emit_req("https://t.test/", "document"),
        lambda: page.emit_resp("https://t.test/", 200, "document"),   # ← body() 在这里重入
        lambda: page.emit_req("https://t.test/a.css", "stylesheet"),
        lambda: page.emit_req("https://t.test/a.js"),
        lambda: page.emit_req("https://t.test/b.png", "image"),
        lambda: page.emit_req("https://t.test/c.png", "image"),
        lambda: page.emit_resp("https://t.test/a.css", 200, "stylesheet"),
        lambda: page.emit_resp("https://t.test/a.js"),
        lambda: page.emit_resp("https://t.test/b.png", 200, "image"),
        lambda: page.emit_resp("https://t.test/c.png", 200, "image"),
    ]


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_budget_is_strict_under_reentrancy(monkeypatch, tmp_path, budget):
    """回归：预算曾经被**重入**打穿 —— `--max-requests 2` 实跑录到 3~5 条。

    两次错误都在这条路上：①只数 `len(records)`；②数 `len(records)+len(pending)`。
    后者的破绽是 `resp.body()` 切 greenlet 时，本条响应还没进 `records`，
    于是两个量都偏小，配额被多放。修法是放行瞬间自增的单调计数器。
    """
    page = _FakePage(_reentrant_script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://t.test/", tmp_path, engine="chromium",
                         ua="UA", timeout=1.0, max_requests=budget, wait_after=0)
    assert manifest["requests_recorded"] == budget, manifest
    assert manifest["truncated"] is True
    lines = (tmp_path / "traffic.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == budget


def test_no_false_truncation_when_page_fits_budget(monkeypatch, tmp_path):
    """页面正好用完预算 ≠ 被截断。`truncated` 是"有请求被拒"，不是"用满了"。"""
    page = _FakePage(_reentrant_script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://t.test/", tmp_path, engine="chromium",
                         ua="UA", timeout=1.0, max_requests=5, wait_after=0)
    assert manifest["requests_recorded"] == 5
    assert manifest["truncated"] is False


def test_record_survives_reentrancy_and_keeps_fields(monkeypatch, tmp_path):
    """重入发生在 `body()` 里，不能让已入账的那条丢掉字段或重复。"""
    page = _FakePage(_reentrant_script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://t.test/", tmp_path, engine="chromium",
                         ua="UA", timeout=1.0, max_requests=10, wait_after=0)
    assert manifest["requests_recorded"] == 5
    recs = [__import__("json").loads(ln) for ln in
            (tmp_path / "traffic.jsonl").read_text(encoding="utf-8").splitlines()]
    urls = [r["url"] for r in recs]
    assert urls == ["https://t.test/", "https://t.test/a.css", "https://t.test/a.js",
                    "https://t.test/b.png", "https://t.test/c.png"], urls
    assert all(r.get("body_bytes") == 16 for r in recs), [r.get("body_bytes") for r in recs]
    assert recs[0]["set_cookie_names"] == ["cf_clearance"]
    assert "SECRET" not in (tmp_path / "traffic.jsonl").read_text(encoding="utf-8")


def test_requestfailed_also_counts_against_budget(monkeypatch, tmp_path):
    """失败请求同样占一条记录 —— 它也是一次真实的网络尝试。"""
    def script(page):
        return [
            lambda: page.emit_req("https://t.test/", "document"),
            lambda: page.emit_req("https://t.test/x.js"),
            lambda: page.emit_resp("https://t.test/", 200, "document"),
            lambda: page.emit_failed("https://t.test/x.js"),
        ]

    page = _FakePage(script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://t.test/", tmp_path, engine="chromium",
                         ua="UA", timeout=1.0, max_requests=2, wait_after=0)
    assert manifest["requests_recorded"] == 2
    assert manifest["truncated"] is False
    recs = [__import__("json").loads(ln) for ln in
            (tmp_path / "traffic.jsonl").read_text(encoding="utf-8").splitlines()]
    assert recs[1]["status"] is None and "ABORTED" in recs[1]["error"]
    assert rt.summarize(recs)["by_status"]["failed"] == 1



# ── URL 里的口令同样不许落盘 ────────────────────────


def test_redact_url_strips_password_only():
    assert rt.redact_url("https://u:S3CRET@h.test/p?q=1") == "https://u:***@h.test/p?q=1"
    assert rt.redact_url("http://tok@h.test:8080/") == "http://tok:***@h.test:8080/"


def test_redact_url_leaves_ordinary_urls_alone():
    for u in ("https://h.test/cdn-cgi/challenge-platform/x.js?__cf_chl_tk=abc",
              "http://h.test:3128/", "not a url", ""):
        assert rt.redact_url(u) == u


def test_redact_url_keeps_host_for_summary():
    """抹口令不能把 host 带走 —— `by_host` 汇总靠它。"""
    assert rt.summarize([_rec(url=rt.redact_url("https://u:p@a.test/x"))])["by_host"] == {"a.test": 1}


def test_record_never_writes_url_password(monkeypatch, tmp_path, capsys):
    """`--url https://u:p@host/` 是公开参数，口令不许进 jsonl / manifest / dry-run 输出。"""
    def script(page):
        return [lambda: page.emit_req("https://usr:S3CRET@t.test/", "document"),
                lambda: page.emit_resp("https://usr:S3CRET@t.test/", 200, "document")]

    page = _FakePage(script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://usr:S3CRET@t.test/", tmp_path, engine="chromium",
                         ua="UA", timeout=1.0, max_requests=5, wait_after=0)
    blob = (tmp_path / "traffic.jsonl").read_text(encoding="utf-8") + \
           (tmp_path / "manifest.json").read_text(encoding="utf-8") + \
           (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert "S3CRET" not in blob, blob[:200]
    assert manifest["url"] == "https://usr:***@t.test/"


def test_dry_run_output_redacts_url_password(capsys):
    rt.main(["--url", "https://usr:S3CRET@t.test/", "--out", "/tmp/x"])
    out = capsys.readouterr().out
    assert "S3CRET" not in out, out
    assert "https://usr:***@t.test/" in out


# ── nav_error / 实测 UA（独立复审 R1、R2）──────────────


class _TimeoutPage(_FakePage):
    """`goto` 抛一个**内嵌完整 URL** 的超时异常 —— Playwright 的真实消息格式。"""

    def __init__(self) -> None:
        super().__init__(lambda p: [])

    def goto(self, url, **_kw):
        raise RuntimeError(
            'Page.goto: Timeout 30000ms exceeded.' + chr(10) + 'Call log:' + chr(10) +
            f'  - navigating to "{url}", waiting until "domcontentloaded"')


def test_nav_error_does_not_leak_url_password(monkeypatch, tmp_path):
    """挑战页导航超时是本工具最常走的分支，异常原文里内嵌完整 URL。

    回归：`nav_error` 曾经直接 `str(e)[:160]` 落进 `manifest.json` 并打到 stdout，
    于是 `traffic.jsonl` 干净、`manifest.json` 里却有口令。
    """
    page = _TimeoutPage()
    _patched(monkeypatch, page)
    manifest = rt.record("https://usr:S3CRET@t.test/", tmp_path, engine="chromium",
                         ua="", timeout=1.0, max_requests=5, wait_after=0)
    assert manifest["nav_error"], "超时分支没被走到，测试无效"
    assert "S3CRET" not in manifest["nav_error"], manifest["nav_error"]
    assert "usr:***@t.test" in manifest["nav_error"], manifest["nav_error"]
    assert "S3CRET" not in (tmp_path / "manifest.json").read_text(encoding="utf-8")


def test_failed_request_error_text_is_redacted(monkeypatch, tmp_path):
    """`requestfailed` 的原因文本里也可能带 URL。"""
    def script(page):
        return [lambda: page.emit_req("https://usr:S3CRET@t.test/x.js"),
                lambda: page.emit_failed("https://usr:S3CRET@t.test/x.js",
                                         "net::ERR_FAILED at https://usr:S3CRET@t.test/x.js")]

    page = _FakePage(script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://usr:S3CRET@t.test/", tmp_path, engine="chromium",
                         ua="", timeout=1.0, max_requests=5, wait_after=0)
    blob = (tmp_path / "traffic.jsonl").read_text(encoding="utf-8")
    assert "S3CRET" not in blob, blob[:300]
    assert manifest["requests_recorded"] == 1


def test_redact_any_handles_free_text():
    msg = 'boom: navigating to "https://u:S3CRET@h.test/p", waiting'
    out = rt.redact_any(msg)
    assert "S3CRET" not in out and "u:***@h.test" in out
    assert rt.redact_any("没有 URL 的普通消息") == "没有 URL 的普通消息"


def test_manifest_user_agent_is_the_observed_one(monkeypatch, tmp_path):
    """camoufox 的身份由 persona 决定：manifest 必须记**实测** UA，不是 `--ua`。"""
    page = _FakePage(_reentrant_script)
    _patched(monkeypatch, page)
    manifest = rt.record("https://t.test/", tmp_path, engine="camoufox",
                         ua="", timeout=1.0, max_requests=2, wait_after=0)
    assert manifest["user_agent"] == "FakeUA/1.0"
    assert manifest["user_agent_override"] == ""


def test_user_agent_override_only_when_asked(monkeypatch, tmp_path):
    seen = {}

    class _Ctx(_FakeCtx):
        def __init__(self, page, kw):
            super().__init__(page)
            seen.update(kw)

    class _Browser(_FakeBrowser):
        def new_context(self, **kw):
            return _Ctx(self._page, kw)

    class _CM:
        def __enter__(self):
            return None, _Browser(_FakePage(_reentrant_script))

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(rt, "_open_browser", lambda engine: _CM())
    rt.record("https://t.test/", tmp_path, engine="camoufox", ua="",
              timeout=1.0, max_requests=1, wait_after=0)
    assert "user_agent" not in seen, seen          # 不传 = 让 persona 决定
    seen.clear()
    rt.record("https://t.test/", tmp_path, engine="chromium", ua="Mozilla/5.0 X",
              timeout=1.0, max_requests=1, wait_after=0)
    assert seen.get("user_agent") == "Mozilla/5.0 X"
