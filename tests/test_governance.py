"""治理层单元测试（v4.1：把治理绑到路由/入口，而不是绑在某个后端实现上）。

为什么单独一个文件：v4.0 的教训是"治理只写在 `Solver.solve` 里"，
于是所有不走 `Solver.solve` 的后端（cfsolve/pydoll/flaresolverr）全部裸奔——
不缓存、不准入、不进指标。这一组用例就是防止那个缺口再次打开：

  · `BackendRouter(governance=...)` 对**任意**后端都要施加缓存/准入/计数；
  · `/solve` 的 SSRF 守卫必须在**选后端之前**执行（与后端组合无关）；
  · `/metrics` 的数字必须与真实发生的调用一致（含失败）。

跑法：python -m pytest tests/test_governance.py -q
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import backends as be  # noqa: E402
import cache as cache_mod  # noqa: E402
import service as svc  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402
from test_service import FakeBrowser, factory_returning, wait_until  # noqa: E402

HOST = "https://example.com/"


def clearance_out(value: str = "C1", ttl: float = 1500.0) -> dict:
    return {
        "ok": True, "solved": True, "cf_clearance": value, "user_agent": "UA/1.0",
        "cookies": {"cf_clearance": value},
        "cookie_list": [{"name": "cf_clearance", "value": value, "domain": "", "path": "/", "expires": -1}],
        "expires_in": int(ttl),
        "expires_at": time.time() + ttl,
        "egress_id": "default",
    }


class FakeBackend:
    """可编程的假后端：记录调用次数、可抛错、可延迟。"""

    def __init__(self, *, raises: Exception | None = None, sleep: float = 0.0,
                 out: dict | None = None) -> None:
        self.calls = 0
        self._raises = raises
        self._sleep = sleep
        self._out = out

    def name(self) -> str:
        return "fake"

    def is_configured(self) -> bool:
        return True

    def solve(self, url, timeout, force=False, **kwargs):
        self.calls += 1
        if self._sleep:
            time.sleep(self._sleep)
        if self._raises is not None:
            raise self._raises
        return dict(self._out or clearance_out())


def make_solver(**kw):
    kw.setdefault("allow_private_hosts", True)
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()), **kw)
    assert wait_until(lambda: s.health()[0] == 200)
    return s


# ── 路由层治理 ──────────────────────────────────────
def test_router_caches_third_party_backend_result():
    """不走 Solver.solve 的后端，第二次也必须命中缓存（否则热路径命中率归零）。"""
    s = make_solver()
    try:
        fake = FakeBackend()
        router = be.BackendRouter([fake], governance=s)
        first = router.solve(HOST, timeout=10)
        second = router.solve(HOST, timeout=10)
        assert first.get("cached") in (False, None)
        assert second["cached"] is True, "第二次必须命中服务端缓存"
        assert fake.calls == 1, f"只应真的求解 1 次，实际 {fake.calls} 次"
        assert second["backend"] == "fake", "命中缓存时也要标出产出它的后端"
    finally:
        s.close()


def test_router_skips_cache_for_one_shot_token():
    """一次性 token（turnstile）复用必失败，绝不能进缓存。"""
    s = make_solver()
    try:
        fake = FakeBackend(out={"ok": True, "solved": True, "token": "T",
                                "token_field": "cf-turnstile-response", "user_agent": "UA",
                                "cookies": {}, "cookie_list": [], "expires_in": 300, "expires_at": 0.0})
        router = be.BackendRouter([fake], governance=s)
        router.solve(HOST, timeout=10, want="turnstile", sitekey="0xAAA")
        router.solve(HOST, timeout=10, want="turnstile", sitekey="0xAAA")
        assert fake.calls == 2, "一次性 token 不许被缓存复用"
    finally:
        s.close()


def test_router_enforces_inflight_gate():
    """路由侧后端也要受 inflight 约束，拿不到票就 429，不许无界排队。"""
    s = make_solver(max_inflight=1)
    try:
        fake = FakeBackend(sleep=0.4)
        router = be.BackendRouter([fake], governance=s)
        results: list[str] = []

        def call():
            try:
                router.solve(HOST, timeout=10)
                results.append("ok")
            except SolveError as e:
                results.append(e.code.value)

        threads = [threading.Thread(target=call) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert sorted(results) == ["busy", "ok"], f"应一个成功一个 busy，实际 {results}"
    finally:
        s.close()


def test_router_releases_inflight_on_failure():
    """失败路径必须归还门票，否则连失败几次就把服务永久卡成 busy。"""
    s = make_solver(max_inflight=1)
    try:
        fake = FakeBackend(raises=SolveError(ErrorCode.UNSOLVED, "boom", stage="wait"))
        router = be.BackendRouter([fake], governance=s)
        for _ in range(3):
            with pytest.raises(SolveError):
                router.solve(HOST, timeout=10)
        # 门票没泄漏才能继续用
        fake2 = FakeBackend()
        ok = be.BackendRouter([fake2], governance=s).solve(HOST, timeout=10)
        assert ok["ok"] is True
    finally:
        s.close()


def test_metrics_count_failures_and_backend_dimension():
    """`/metrics` 必须能看见"哪个后端失败了"，而原来的实现只在成功时 +1。"""
    s = make_solver()
    try:
        fake = FakeBackend(raises=SolveError(ErrorCode.UNSOLVED, "boom", stage="wait"))
        with pytest.raises(SolveError):
            be.BackendRouter([fake], governance=s).solve(HOST, timeout=10)
        text = s.metrics_text()
        assert re.search(r'cfsolve_backend_total\{backend="fake",result="error"\} 1', text), text
        solves = int(re.search(r"^cfsolve_solves_total (\d+)$", text, re.M).group(1))
        assert solves >= 1, "HELP 声明含失败，失败也必须计入"
        assert "cfsolve_failures_total 1" in text
    finally:
        s.close()


def test_metrics_host_dimension_is_capped(tmp_path):
    """按域指标必须截断：域名是调用方可控输入，全量当标签会打爆 Prometheus。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    limit = svc.HOST_METRICS_TOP_N
    for i in range(limit + 5):
        store.record_solve(f"h{i}.example.com", True)
    s = make_solver(store=store)
    try:
        text = s.metrics_text()
        hosts = re.findall(r'cfsolve_host_solves_total\{host="([^"]+)"\}', text)
        assert len(hosts) == limit, f"应只保留 top-{limit}，实际 {len(hosts)}"
    finally:
        s.close()


def test_solver_remember_skips_expired_result():
    """TTL=0 的响应写进缓存等于每次重解，必须在写入处就挡住。"""
    s = make_solver()
    try:
        expired = clearance_out()
        expired["expires_at"] = 0.0
        expired["expires_in"] = 0
        s.remember(HOST, expired)
        assert s.cache_lookup(HOST) is None
    finally:
        s.close()


# ── 入口守卫（与后端组合无关）────────────────────────
@pytest.fixture
def live_router_service():
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=False)
    assert wait_until(lambda: s.health()[0] == 200)
    fake = FakeBackend()
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = be.BackendRouter([fake], governance=s)
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", fake
    finally:
        srv.shutdown()
        srv.server_close()
        svc.Handler.router = None
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


def test_entry_guard_blocks_internal_url_before_any_backend(live_router_service):
    """配了路由后端时，内网 URL 仍必须在**入口**被拒，且后端一次都不该被调用。

    这是 v4.0 的 P0：守卫只在 Solver.solve 里，走 router 就整条绕过。
    """
    base, fake = live_router_service
    status, body = _post(base, {"url": f"{base}/health"})
    assert status == 400, f"应为 400，实际 {status}: {body}"
    assert body["error_code"] == "invalid-request"
    assert fake.calls == 0, f"后端不该被调用，实际 {fake.calls} 次"


# ── T-34：探测结果按域短 TTL 缓存 ────────────────────
def test_detect_cache_hit_skips_outbound_probe(monkeypatch):
    """命中缓存就不该再发出站请求（`route=auto` 的 +0.5s 就是这样摊掉的）。"""
    s = make_solver()
    try:
        s.detect_cache_ttl = 300
        s.detect_cache_put(HOST, {"protected": True, "providers": [{"id": "detect-cloudflare"}]})
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            raise AssertionError("命中缓存时不该再发探测请求")

        monkeypatch.setattr(svc, "probe_protection", boom)
        got = s.detect_cache_get(HOST)
        assert got is not None and got["cached"] is True
        assert calls["n"] == 0
    finally:
        s.close()


def test_detect_cache_respects_ttl(monkeypatch):
    s = make_solver()
    try:
        s.detect_cache_ttl = 0.05
        s.detect_cache_put(HOST, {"protected": False})
        assert s.detect_cache_get(HOST) is not None
        time.sleep(0.08)
        assert s.detect_cache_get(HOST) is None, "过期后必须当未命中"
    finally:
        s.close()


def test_detect_cache_disabled_by_zero_ttl():
    s = make_solver()
    try:
        s.detect_cache_ttl = 0
        s.detect_cache_put(HOST, {"protected": False})
        assert s.detect_cache_get(HOST) is None, "ttl=0 表示完全关闭缓存"
    finally:
        s.close()


def test_detect_cache_is_bounded():
    """域名是调用方可控输入：探测缓存必须有上限，否则能被撑爆内存。"""
    s = make_solver()
    try:
        s.detect_cache_ttl = 300
        for i in range(svc.DETECT_CACHE_MAX + 10):
            s.detect_cache_put(f"https://h{i}.example.com/", {"protected": False})
        assert len(s._detect_cache) <= svc.DETECT_CACHE_MAX
    finally:
        s.close()
