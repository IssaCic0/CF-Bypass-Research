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
import logging
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


def test_audit_log_write_failure_does_not_break_request(live_audit, capfd):
    """审计是旁路：写盘失败（磁盘满/文件被占）不许让请求失败。

    T-82 stdout 卫生：失败留痕走 logging（stderr 通道），**绝不能 print 到 stdout**——
    stdout 是 JSON lines 契约，混进一行非 JSON 文本会打挂消费方。

    留痕断言用自建 logging.Handler（挂在 cfsolve.audit 上）：不依赖 pytest 的
    捕获管道（caplog 在 --cov 下会被 pytest-cov 的 hook 顺序吞掉 record，实测可复现），
    线程安全，也不碰 Logger.warning 的 patch 语义。
    """
    base, _audit = live_audit
    svc.Handler.audit_path = "Z:/definitely-not-a-dir-8f3c/cannot/open/audit.jsonl"

    class _Catch(logging.Handler):
        def __init__(self):
            super().__init__()
            self.messages: list[str] = []

        def emit(self, record):
            self.messages.append(record.getMessage())

    catch = _Catch()
    audit_logger = logging.getLogger("cfsolve.audit")
    audit_logger.addHandler(catch)
    try:
        code, body = _post(base, {"url": HOST, "timeout": 10})
        assert code == 200 and body.get("ok") is True
        # handler 线程在响应发出**之后**才执行 _audit_write（T-79 实测确认）：
        # 这里必须等留痕落地再摘 handler，否则会把它未完成的写入判成"没留痕"。
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and not catch.messages:
            time.sleep(0.05)
    finally:
        audit_logger.removeHandler(catch)
    captured = capfd.readouterr()
    assert "audit-write-failed" not in captured.out, \
        f"audit-write-failed 泄漏进了 stdout（JSON lines 契约被破坏）：{captured.out!r}"
    assert catch.messages and "audit-write-failed" in catch.messages[0], \
        f"写失败必须经 cfsolve.audit logger 留痕（logging.warning），实际 {catch.messages}"
    assert "FileNotFoundError" in catch.messages[0]


def test_stdout_is_clean_during_full_flow(live_audit, capfd):
    """T-82 验收用例：完整请求流程跑完，stdout 只含合法 JSON lines。

    口径：把 sys.stdout 换成内存缓冲，按 `json.JSONDecoder.raw_decode` **顺序扫**——
    每个字符都必须属于某个可解析的 JSON 对象，扫完不剩任何非空白垃圾。
    这样既抓得住"裸 print 一行日志"（`[cfsolve] ...` 扫不过），
    又不会被 fd 级捕获的截断/交错伪影误伤（那是 capfd 的粒度问题，不是契约问题）。
    """
    import io

    base, _audit = live_audit
    svc.Handler.audit_path = "Z:/definitely-not-a-dir-8f3c/cannot/open/audit.jsonl"  # 触发写失败路径
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        code, body = _post(base, {"url": HOST, "timeout": 10})
        assert code == 200 and body.get("ok") is True
        # `_log` 在响应之后才写：等它落地再恢复 stdout
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and '"event": "solve"' not in buf.getvalue():
            time.sleep(0.05)
    finally:
        sys.stdout = real_stdout
    out = buf.getvalue()
    capfd.readouterr()                      # 清掉 fd 级残留，避免污染后续用例

    decoder = json.JSONDecoder()
    pos, parsed = 0, []
    while pos < len(out):
        while pos < len(out) and out[pos] in " \t\r\n":
            pos += 1
        if pos >= len(out):
            break
        try:
            obj, end = decoder.raw_decode(out, pos)
        except json.JSONDecodeError as e:
            pytest.fail(f"stdout 混入了非 JSON 内容（契约破坏）：{out[pos:pos + 120]!r} —— {e}")
        parsed.append(obj)
        pos = end
    assert parsed, "完整流程应至少产生一条 stdout JSON 行（_log 契约）"
    assert any(o.get("event") == "solve" for o in parsed if isinstance(o, dict)), \
        f"没抓到 solve 事件行：{parsed}"
    # 更强的负向断言：任何形状的日志文本都不许出现在 stdout
    for bad in ("[cfsolve]", "audit-write-failed", "WARNING", "Traceback"):
        assert bad not in out, f"stdout 出现非契约文本 {bad!r}：{out[:200]!r}"


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


# ── 跨进程去重：同一域同时被多个 worker 抢到 ──────────
def test_try_claim_is_atomic_and_expires(tmp_path):
    """两个"进程"（两个 store 实例）抢同一个 key：只能有一个成功；过期后可以再抢。"""
    a = cache_mod.SqliteStore(str(tmp_path), "claim-token")
    b = cache_mod.SqliteStore(str(tmp_path), "claim-token")

    assert a.try_claim("k|default", ttl=60) is True
    assert b.try_claim("k|default", ttl=60) is False, "同 key 只能有一个抢占者"
    a.release_claim("k|default")
    assert b.try_claim("k|default", ttl=60) is True, "释放后应能再抢"

    # 过期后可被重新抢占（对端崩了不能让这个域永久卡死）。
    # 注意 ttl 有 1.0s 下限（防止有人传 0 把抢占变成摆设），所以这里要等过 1s。
    b.release_claim("k|default")
    assert a.try_claim("k|default", ttl=0.5) is True
    time.sleep(1.2)
    assert cache_mod.SqliteStore(str(tmp_path), "claim-token").try_claim("k|default", ttl=60) is True


def test_solver_dedups_same_host_across_two_instances(tmp_path):
    """模拟"两个 worker 进程共享缓存目录同时收到同一个新域"：只允许真的解一次。

    实测背景：不抢占时 4 进程 burst 会各解一次（12/12）；这一步是那个问题的修复。
    """
    store_a = cache_mod.SqliteStore(str(tmp_path), "t")
    store_b = cache_mod.SqliteStore(str(tmp_path), "t")
    backend = FakeBackend(sleep=0.5)

    solvers = []
    for st in (store_a, store_b):
        s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                       allow_private_hosts=True, store=st)
        assert wait_until(lambda: s.health()[0] == 200)
        solvers.append(s)

    results: list[dict] = []
    lock = threading.Lock()

    def call(s):
        router = be.BackendRouter([backend], governance=s)
        try:
            out = router.solve(HOST, timeout=30)
        except SolveError as e:
            out = {"ok": False, "error": str(e)}
        with lock:
            results.append(out)

    threads = [threading.Thread(target=call, args=(s,)) for s in solvers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)

    try:
        assert backend.calls == 1, f"同域并发只该真解 1 次，实际 {backend.calls} 次（跨进程去重失效）"
        assert len(results) == 2 and all(r.get("ok") for r in results), results
        assert sum(1 for r in results if r.get("cached")) >= 1, "后到者应当命中缓存"
    finally:
        for s in solvers:
            s.close()


# ── /shutdown 的来源判定（容器场景的回归保护）──────────
def test_shutdown_allowed_matrix():
    """默认只放行回环；`--allow-remote-shutdown` 才放行任意来源。

    实测背景：容器 + 端口映射时客户端地址是 docker 网关（172.17.0.1），
    只放行回环会让"滚动重启"在容器里直接失败（本轮真跑容器时抓到）。
    """
    old = svc.Handler.allow_remote_shutdown
    try:
        svc.Handler.allow_remote_shutdown = False
        for peer in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"):
            assert svc.Handler.shutdown_allowed(peer) is True, peer
        for peer in ("172.17.0.1", "10.0.0.5", "203.0.113.9", ""):
            assert svc.Handler.shutdown_allowed(peer) is False, peer

        svc.Handler.allow_remote_shutdown = True
        for peer in ("172.17.0.1", "203.0.113.9"):
            assert svc.Handler.shutdown_allowed(peer) is True, peer
    finally:
        svc.Handler.allow_remote_shutdown = old


def test_claim_is_released_after_failure(tmp_path):
    """求解失败也必须放掉抢占 —— 否则同域的其它 worker 会白等一个永远不会出现的缓存。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                   allow_private_hosts=True, store=store)
    assert wait_until(lambda: s.health()[0] == 200)
    try:
        backend = FakeBackend(raises=SolveError(ErrorCode.UNSOLVED, "boom", stage="wait"))
        router = be.BackendRouter([backend], governance=s)
        with pytest.raises(SolveError):
            router.solve(HOST, timeout=10)

        # 失败后抢占应当已释放：另一个"进程"能立刻抢到
        other = cache_mod.SqliteStore(str(tmp_path), "t")
        entry = s._entry_for("example.com")
        key = s.cache_key("example.com", entry)
        assert other.try_claim(key, ttl=30) is True, "失败后抢占未释放，对端会白等"
    finally:
        s.close()


# ── 独立审查 P1/P2 的回归保护（v4.6）──────────────────
def test_failed_solve_is_counted_once(tmp_path):
    """一次失败的求解只能记 1 次（此前"无条件先记成功 + 再记失败"→ 失败请求显示 50% 成功率）。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                   allow_private_hosts=True, store=store)
    assert wait_until(lambda: s.health()[0] == 200)
    try:
        # 让 worker "跑完但没拿到 token"：这是 `if not token` 那条失败路径
        s._submit = lambda fn, budget: {"cookies": {}, "ua": "", "token": "", "cookie_list": []}  # type: ignore[assignment]
        with pytest.raises(SolveError):
            s.solve(HOST, timeout=10)
        code, body = s.health()
        assert body["solves"] == 1, f"失败只应记 1 次求解，实际 {body['solves']}"
        assert body["failures"] == 1
    finally:
        s.close()


def test_claim_released_when_admission_rejects(tmp_path):
    """准入被拒（BUSY）也必须释放抢占：否则同域的其它 worker 会白等一个不会来的缓存。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                   allow_private_hosts=True, store=store, max_inflight=1)
    assert wait_until(lambda: s.health()[0] == 200)
    try:
        assert s._inflight.acquire(blocking=False)      # 占满额度
        router = be.BackendRouter([FakeBackend()], governance=s)
        with pytest.raises(SolveError) as ei:
            router.solve(HOST, timeout=10)
        assert ei.value.code == ErrorCode.BUSY

        other = cache_mod.SqliteStore(str(tmp_path), "t")
        key = s.cache_key("example.com", None)
        assert other.try_claim(key, ttl=30) is True, "准入被拒后抢占未释放"
    finally:
        try:
            s._inflight.release()
        except ValueError:
            pass
        s.close()


def test_shutdown_denied_returns_401_with_body(monkeypatch):
    """来源不合法时必须**回 401 响应体**，而不是抛在 try 外（那会变成 traceback + 断连）。"""
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    old_allow = svc.Handler.allow_remote_shutdown
    monkeypatch.setattr(svc.Handler, "shutdown_allowed", staticmethod(lambda peer: False))
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.server_address[1]}/shutdown", data=b"{}", method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer t"})
        with pytest.raises(urllib.error.HTTPError) as ei:
            urllib.request.urlopen(req, timeout=10)
        assert ei.value.code == 401
        body = json.loads(ei.value.read().decode())
        assert body["error_code"] == "unauthorized" and "回环" in body["error"]
    finally:
        srv.shutdown()
        srv.server_close()
        svc.Handler.allow_remote_shutdown = old_allow
        s.close()


def test_claim_released_when_proxy_parse_fails(tmp_path, monkeypatch):
    """`_proxy_dict` 解析失败这条早退路径也要释放抢占（审查指出的覆盖缺口）。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                   allow_private_hosts=True, store=store)
    assert wait_until(lambda: s.health()[0] == 200)
    try:
        monkeypatch.setattr(s, "_proxy_dict", lambda entry: (_ for _ in ()).throw(ValueError("bad proxy")))
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code == ErrorCode.BROWSER_UNAVAILABLE

        other = cache_mod.SqliteStore(str(tmp_path), "t")
        assert other.try_claim(s.cache_key("example.com", None), ttl=30) is True, \
            "代理解析失败后抢占未释放"
    finally:
        s.close()


def test_claim_released_when_solver_admission_rejects(tmp_path):
    """Solver 自身的 inflight 被拒（不是路由侧）也要释放抢占。"""
    store = cache_mod.SqliteStore(str(tmp_path), "t")
    s = svc.Solver(browser_factory=factory_returning(FakeBrowser()),
                   allow_private_hosts=True, store=store, max_inflight=1)
    assert wait_until(lambda: s.health()[0] == 200)
    try:
        assert s._inflight.acquire(blocking=False)
        with pytest.raises(SolveError) as ei:
            s.solve(HOST, timeout=10)
        assert ei.value.code == ErrorCode.BUSY

        other = cache_mod.SqliteStore(str(tmp_path), "t")
        assert other.try_claim(s.cache_key("example.com", None), ttl=30) is True, \
            "Solver 准入被拒后抢占未释放"
    finally:
        try:
            s._inflight.release()
        except ValueError:
            pass
        s.close()


# ── 早退路径的审计完整性（v4.7：畸形 body 打穿了日志本身）────────────
def _post_raw(base, raw: bytes):
    """发**原始字节** body：`_post` 会先 json.dumps，构造不出畸形 JSON。"""
    req = urllib.request.Request(
        base + "/solve", data=raw,
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _solve_audits(audit):
    """只解析一次；非 JSON 行给出可读失败而不是裸 JSONDecodeError。"""
    recs = []
    for line in audit.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise AssertionError(f"审计文件里出现了非 JSON 行：{line[:120]!r}（{e}）") from e
        if isinstance(rec, dict) and rec.get("event") == "solve":
            recs.append(rec)
    return recs


def test_malformed_json_is_rejected_and_still_audited(live_audit):
    """畸形 body 也必须留下审计行。

    回归的是 `do_POST` 的早退路径：`url` 在 `_parse_json()` **之后**才赋值，
    而 `except SolveError` 里无条件读 `url` → `UnboundLocalError`。
    后果很隐蔽：响应仍是 400（所以只看到状态码的用例全绿），
    但 `_log` 那行没跑到 ⇒ **审计里一条都没有**，socketserver 还替每个
    这样的请求打一整段 traceback。畸形输入恰恰是最需要审计的那一类。
    """
    base, audit = live_audit
    code, body = _post_raw(base, b"{not json")
    assert code == 400 and body.get("error_code") == "invalid-request", (code, body)
    assert wait_until(lambda: audit.exists() and "solve" in audit.read_text(encoding="utf-8")), \
        "畸形请求没有留下任何审计行（日志路径被 UnboundLocalError 打穿）"
    recs = _solve_audits(audit)
    assert recs and recs[-1]["error_code"] == "invalid-request", recs


def test_empty_body_is_rejected_and_still_audited(live_audit):
    """空 body 走的是同一段早退路径（`body_error` 在 `url` 赋值前就抛出）。"""
    base, audit = live_audit
    code, body = _post_raw(base, b"")
    assert code == 400, (code, body)
    assert wait_until(lambda: audit.exists() and "solve" in audit.read_text(encoding="utf-8")), \
        "空 body 请求没有留下任何审计行"
    recs = _solve_audits(audit)
    assert recs and recs[-1]["error_code"] == "invalid-request", recs


def test_oversized_body_is_rejected_and_still_audited(live_audit):
    """超大 body 是第三条早退路径（`_read_body_raw` 也在 `url` 赋值之前返回错误）。

    独立审查指出：N-14 自述覆盖"空 body / 畸形 JSON / 超大 body **三**条"，用例却只有前两条。
    这条补齐第三条 —— 它同时验证审计不丢（`_read_body_raw` 抛错时同样会走 except 分支）。

    v5.0 T-51 契约变更：超大 body 从 400 invalid-request 改为 **413 payload-too-large**
    （400=请求坏了不该重试；413=可减小请求体后重试）。审计行为不变：这一行必须存在。
    """
    base, audit = live_audit
    big = b"{" + b"x" * (70 * 1024) + b"}"          # > MAX_BODY_BYTES(64KB)
    code, body = None, {}
    try:
        code, body = _post_raw(base, big)
    except (urllib.error.URLError, ConnectionError) as e:
        # 允许的一种平台差异：服务端 `_read_body_raw` 明确写了 close_connection（不读超大 body
        # 就直接消失），某些内核/缓冲区组合下客户端会在"发完 body"之前先拿到
        # ConnectionReset/RemoteDisconnected，而不是读到响应。这不是服务缺陷 ——
        # 所以这里退一步：只要求"异常确实是连接级重置"，状态码断言跳过；
        # 真正要钉的是下面那条「审计里必须有这一行」。
        msg = str(e).lower()
        assert any(k in msg for k in ("reset", "aborted", "10054", "remote end closed",
                                      "connection")), f"意外的客户端异常：{e}"
    assert code in (None, 413), (code, body)
    if code == 413:
        assert body.get("error_code") == "payload-too-large", body
        assert "请求体超过" in str(body.get("error", "")), body
    assert wait_until(lambda: audit.exists() and "solve" in audit.read_text(encoding="utf-8")), \
        "超大 body 请求没有留下审计行"
    recs = _solve_audits(audit)
    assert recs and recs[-1]["error_code"] == "payload-too-large", recs
