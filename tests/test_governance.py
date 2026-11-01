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


# ── 优雅退出：in-flight 请求的结局（此前完全没测过）──────────────
def test_close_with_inflight_request_does_not_hang():
    """`close()` 期间正在求解的请求必须有明确结局（报错或完成），不能永久挂住。

    这是"滚动重启会不会掉请求"的核心：旧进程收到退出信号时，如果直接杀线程，
    调用方拿到的是 connection reset；如果死等，进程永远退不掉。
    """
    import threading

    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True,
                   max_inflight=1, max_queue=4)
    assert wait_until(lambda: s.health()[0] == 200)

    outcome: dict[str, object] = {}

    def slow_call():
        try:
            # FakeBrowser 的求解会真跑一小段；这里用极长 timeout 也没关系，
            # 关键是 close() 之后它必须返回，而不是挂死。
            outcome["result"] = s.solve(HOST, timeout=5)
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = type(e).__name__

    t = threading.Thread(target=slow_call, daemon=True)
    t.start()
    s.close()                     # 优雅退出：发关闭信号 + 关闭浏览器
    t.join(timeout=15)
    assert not t.is_alive(), "close() 之后 in-flight 请求仍然挂着（会拖住重启）"
    assert "result" in outcome or "error" in outcome, "必须有明确结局（成功或异常），不能静默"

    # 关闭后新请求必须被明确拒绝（而不是尝试用一个已死的浏览器）
    with pytest.raises(SolveError) as ei:
        s.solve(HOST, timeout=5)
    assert ei.value.code in (ErrorCode.BROWSER_UNAVAILABLE, ErrorCode.BUSY), \
        f"关闭后应明确拒绝，实际 {ei.value.code}"


def test_close_is_idempotent():
    """重复 close 不许抛（预启动脚本/信号处理里很容易重复调用）。"""
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    s.close()
    s.close()


# ── 优雅退出：/shutdown 端点 + 在途请求排空 ──────────────────
@pytest.fixture
def live_server_slow():
    """起一个真实 Server，并挂一个"慢后端"（0.6s），用于测排空。"""
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    fake = FakeBackend(sleep=0.6)
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = be.BackendRouter([fake], governance=s)
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        svc.Handler.router = None
        s.close()


def test_shutdown_requires_token(live_server_slow):
    """能停服的端点必须鉴权 —— 少了它就是一个公网 DoS 开关。"""
    _srv, base = live_server_slow
    req = urllib.request.Request(base + "/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=10)
    assert ei.value.code == 401


def test_shutdown_from_loopback_returns_202_and_stops_server(live_server_slow):
    srv, base = live_server_slow
    req = urllib.request.Request(base + "/shutdown", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer t"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 202
        payload = json.loads(resp.read().decode())
    assert payload["state"] == "shutting-down"

    # 服务应当停止接收新请求
    deadline = time.time() + 5
    stopped = False
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            time.sleep(0.05)
        except Exception:  # noqa: BLE001
            stopped = True
            break
    assert stopped, "调用 /shutdown 后服务仍在接受新请求"


def test_drain_waits_for_inflight_request(live_server_slow):
    """排空必须真的等：在途请求要拿到 200，而不是被砍成连接重置。"""
    srv, base = live_server_slow
    got: dict = {}

    def call():
        try:
            code, body = _post(base, {"url": HOST, "timeout": 30})
            got["code"] = code
            got["ok"] = body.get("ok")
        except Exception as e:  # noqa: BLE001
            got["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=call, daemon=True)
    t.start()
    time.sleep(0.15)                     # 让请求确实进到"求解中"（后端 sleep 0.6s）
    srv.shutdown()                       # 停止接收新连接
    left = srv.drain(5.0)                # 等在途请求收尾
    t.join(timeout=10)

    assert left == 0, f"在途请求没有被排空（剩 {left} 个）"
    assert got.get("code") == 200 and got.get("ok") is True, f"在途请求的结局不对: {got}"


def test_routed_by_survives_failure(monkeypatch):
    """失败也要回传路由决策：线上只看到 unsolved 时，必须还能知道"它当时怎么判的"。

    实测背景：route=auto 打一个真实 CF 站 60s 未解出，响应里 routed_by 全空，
    排查时无法区分"探测说没盾但实际有盾"与"探测说有盾但解不出来"。
    """
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    # 后端直接失败，模拟"求解不出来"
    fake = FakeBackend(raises=SolveError(ErrorCode.UNSOLVED, "boom", stage="wait"))
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = be.BackendRouter([fake], governance=s)
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        monkeypatch.setattr(svc, "probe_protection", lambda *a, **k: {
            "url": HOST, "status": 403, "protected": True, "cloudflare_challenge_type": "managed",
            "providers": [{"id": "detect-cloudflare", "name": "Cloudflare"}],
            "recommendation": {"route": "browser"},
        })
        code, body = _post(f"http://127.0.0.1:{srv.server_address[1]}",
                           {"url": HOST, "route": "auto", "timeout": 10})
        assert body["ok"] is False
        assert "routed_by" in body, f"失败响应必须带 routed_by，实际 keys={list(body)}"
        assert body["routed_by"]["reason"] == "cloudflare-managed"
        assert body["routed_by"]["prefer"] == "pure"
    finally:
        srv.shutdown()
        srv.server_close()
        svc.Handler.router = None
        s.close()


# ── T-38 审计日志 ───────────────────────────────────
@pytest.fixture
def live_audit(tmp_path):
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    fake = FakeBackend(out=clearance_out("AUDIT-CLEARANCE-VALUE"))
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = be.BackendRouter([fake], governance=s)
    audit = tmp_path / "audit.jsonl"
    # 先把可能残留的句柄关掉：Handler 的审计句柄是类级缓存的，
    # 上一条用例没关就会把这次的内容写进上一条的临时文件（本用例第一版就栽在这）。
    if svc.Handler.audit_fp is not None:
        try:
            svc.Handler.audit_fp.close()
        finally:
            svc.Handler.audit_fp = None
    old_path, svc.Handler.audit_path = svc.Handler.audit_path, str(audit)
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", audit
    finally:
        srv.shutdown()
        srv.server_close()
        svc.Handler.router = None
        svc.Handler.audit_path = old_path
        if svc.Handler.audit_fp is not None:
            try:
                svc.Handler.audit_fp.close()
            finally:
                svc.Handler.audit_fp = None
        s.close()


def test_audit_log_records_request_without_credentials(live_audit):
    """审计要能回答"谁在什么时候打了哪个站、结果如何"，但**绝不能**留下凭证。"""
    base, audit = live_audit
    code, body = _post(base, {"url": HOST, "timeout": 10})
    assert code == 200 and body.get("ok") is True

    # 审计行是在响应发出**之后**写的（`_log` 在 `_send` 后调用），所以这里要等一小会儿。
    # 这不是测试妥协：线上同样存在"响应已回、审计还没落盘"的毫秒级窗口。
    assert wait_until(lambda: audit.exists() and "solve" in audit.read_text(encoding="utf-8")),         "审计行没有落盘"
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    solves = [r for r in lines if r.get("event") == "solve"]
    assert solves, f"应当有一条 solve 审计，实际 {lines}"
    rec = solves[-1]
    assert rec["host"] == "example.com"
    assert rec["ok"] is True and rec["backend"] == "fake"

    raw = audit.read_text(encoding="utf-8")
    assert "AUDIT-CLEARANCE-VALUE" not in raw, "审计文件里出现了 clearance 明文"
    assert "Bearer" not in raw and "cf_clearance" not in raw


def test_audit_log_drops_unknown_fields(tmp_path):
    """白名单之外的字段一律不落盘（防未来有人往 _log 里塞凭证）。"""
    import service as svc_mod
    audit = tmp_path / "a.jsonl"
    old_path, svc_mod.Handler.audit_path = svc_mod.Handler.audit_path, str(audit)
    try:
        h = svc_mod.Handler.__new__(svc_mod.Handler)     # 不真正起服务，只测写盘
        h._audit_write({"event": "x", "host": "h", "cf_clearance": "SECRET",
                        "token": "SECRET2", "cookies": {"cf_clearance": "SECRET3"}})
        raw = audit.read_text(encoding="utf-8")
        assert "SECRET" not in raw, f"白名单失效，写进了敏感字段: {raw}"
        assert '"host": "h"' in raw and '"event": "x"' in raw
    finally:
        svc_mod.Handler.audit_path = old_path
        if svc_mod.Handler.audit_fp is not None:
            svc_mod.Handler.audit_fp.close()
            svc_mod.Handler.audit_fp = None


def test_audit_log_write_failure_does_not_break_request(live_audit):
    """审计是旁路：写盘失败（磁盘满/文件被占）不许让请求失败。"""
    base, _audit = live_audit
    svc.Handler.audit_path = "/nonexistent-dir/cannot/open/audit.jsonl"
    try:
        code, body = _post(base, {"url": HOST, "timeout": 10})
        assert code == 200 and body.get("ok") is True
    finally:
        pass


# ── prefer 现在真的影响选路（此前只是提示字段）──────────────
def test_skip_backends_mapping():
    import route as rt

    assert rt.skip_backends("pure") == frozenset({"pydoll", "flaresolverr"})
    assert rt.skip_backends("browser") == frozenset({"cfsolve"})
    assert rt.skip_backends("") == frozenset()
    assert rt.skip_backends("any") == frozenset()


def test_router_skips_backends_by_prefer():
    s = make_solver()
    try:
        pure = FakeBackend()          # 用名字代表"纯协议"
        heavy = FakeBackend()         # 用名字代表"重后端"
        pure.name = lambda: "cfsolve"          # type: ignore[method-assign]
        heavy.name = lambda: "flaresolverr"    # type: ignore[method-assign]
        # 顺序很关键：把"被跳过"的那个放前面，才能证明它**真的被跳过**而不是
        # "还没轮到就已经成功了"（跳过是惰性的：前面的后端成功就不再看后面的）。
        router = be.BackendRouter([heavy, pure], governance=s)

        out = router.solve(HOST, timeout=10, skip={"flaresolverr"})
        assert out["backend"] == "cfsolve"
        assert pure.calls == 1 and heavy.calls == 0, "被 skip 的后端不该被调用"
        assert any(a.get("reason") == "prefer-skip" for a in out["attempts"]), \
            f"attempts 里要能看出跳过了谁（否则线上查不出路由决策）: {out['attempts']}"
    finally:
        s.close()


def test_router_ignores_skip_when_it_would_skip_everything():
    """安全阀：skip 把整条链都跳完时必须放弃跳过，而不是报"没有可用后端"。"""
    s = make_solver()
    try:
        only = FakeBackend()
        only.name = lambda: "cfsolve"          # type: ignore[method-assign]
        router = be.BackendRouter([only], governance=s)
        out = router.solve(HOST, timeout=10, skip={"cfsolve"})
        assert out["ok"] is True and out["backend"] == "cfsolve"
        assert only.calls == 1, "全跳过时应当忽略 skip 正常求解"
    finally:
        s.close()
