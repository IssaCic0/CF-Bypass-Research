"""v4.1 验收用例（**红灯先行**）：把"应该成立但实际不成立"的契约写成可执行断言。

这份文件是施工前的验收台账，不是"现状描述"：
  · 组 A（F5）—— 治理不随**后端**变化：配了任一路由后端后，`/solve` 的 SSRF 守卫不许被绕过；
  · 组 B（F9）—— **纯协议后端**必须享受与 Camoufox 同等的治理：缓存 / 指标 / 准入；
  · 组 C（F10）—— 指标不得说谎：`/metrics` 的计数必须与真实发生的求解一一对应；
  · 组 D —— 正常路径的**回归护栏**（现在就是绿的，用来防止修 A/B/C 时打破既有行为）。

基线口径：在 v4.0（commit f780b5c）上，组 A/B/C **全是红的**（对应 `plans/下一步改进指南.md`
§4 的 P0-1 / P1-1 / F10）；一旦 T-28 / T-29 / T-37 落地，它们应当转绿 —— 这份文件就是那道门禁。
**不要为了让它们变绿去改实现或放宽断言。**

装配纪律：router 一律按 `main()` 的方式装配（v4.1 起是 `BackendRouter(backends, governance=solver)`，
见 `solver/service.py` 的 `Handler.router = ...` 那行）。`_build_router()` 会自动适配旧签名，
所以在 v4.0 上本文件不会因为 `TypeError` 崩掉，而是以**行为断言**的方式红给你看。

离线纪律（与 `tests/test_service.py` 完全一致）：
  · 不联网、不起真浏览器：`FakeBrowser` + `factory_returning` 注入；
  · 后端用**假后端对象**代替（cfsolve/pydoll/flaresolverr 的真实实现都是子进程直连或自开 Chrome，
    一旦被调用就真的会去打那个 URL —— 所以"调用次数为 0"本身就是"没有出站请求"的证据）；
  · 服务端用真实的 `service.Server` + `service.Handler`（`Handler.router` 按组合注入）。

跑法：python -m pytest tests/test_acceptance_v41.py -q
"""

from __future__ import annotations

import json
import inspect
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
import service as svc  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402
from test_service import FakeBrowser, factory_returning, wait_until  # noqa: E402

HOST = "https://example.com/"
TOKEN = "acceptance-token"


# ── 假后端：记录"被调用了几次、收到了什么 URL" ─────────────
class RecordingBackend:
    """能成功、能抛错、能拖时间的假后端，同时记录调用次数与实参 URL。

    为什么"调用次数"是硬证据：`CfsolveBackend` 是 `subprocess.run` 直连，
    `PydollBackend` 自开 Chrome 直连，`FlareSolverrBackend` 是 urllib 直连，
    三者都**不经过** Camoufox context 上的 `_install_request_guard`（`solver/service.py:965`）。
    只要它们的 `solve()` 被调用，那个 URL 就已经真的被发出去了。
    """

    def __init__(self, name: str, *, result: dict | None = None,
                 error: SolveError | None = None, delay: float = 0.0) -> None:
        self._name = name
        self._result = result
        self._error = error
        self._delay = delay
        self.calls = 0
        self.urls: list[str] = []
        self._lock = threading.Lock()

    def name(self) -> str:
        return self._name

    def is_configured(self) -> bool:
        return True

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        with self._lock:
            self.calls += 1
            self.urls.append(url)
        if self._delay:
            time.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return dict(self._result or {})


def success_result() -> dict:
    """与 `/solve` 同形状（`solver/backends.py` 的 `SolverBackend` 协议）的成功结果。"""
    now = time.time()
    return {
        "ok": True,
        "solved": True,
        "cf_clearance": "C1",
        "user_agent": "UA",
        "cookies": {"cf_clearance": "C1"},
        "cookie_list": [
            {"name": "cf_clearance", "value": "C1", "domain": "", "path": "/", "expires": -1}
        ],
        "expires_in": 1500,
        "expires_at": now + 1500,
        "cached": False,
    }


def _calls_report(fakes: list[RecordingBackend]) -> str:
    """把"假后端实际被调用了什么"拼成一句人话，用在断言失败信息里。"""
    if not fakes:
        return "本组合链上没有额外后端（只有 camoufox）"
    return "；".join(
        f"{f.name()} 被调用 {f.calls} 次，收到的 url={f.urls}" for f in fakes
    )


def _build_router(solver, backends: list) -> be.BackendRouter:
    """**按生产装配方式**建 router，而不是按某个版本的便利签名建。

    为什么要在意这件事：v4.0 的 `BackendRouter(backends)` 里没有任何治理（缓存/准入/计数），
    所以"用 v4.0 的签名建链"必然测不到治理 —— 那等于用错误的装配方式去断言正确的行为。
    v4.1 起 `main()` 的装配是 `BackendRouter(backends, governance=solver)`
    （见 `solver/service.py` 里 `Handler.router = ...` 那一行），本函数跟随它；
    旧签名下退回 `BackendRouter(backends)`，让行为断言自己去红。
    """
    params = inspect.signature(be.BackendRouter.__init__).parameters
    if "governance" in params:
        return be.BackendRouter(list(backends), governance=solver)
    return be.BackendRouter(list(backends))


# ── 服务装配（真实 Server/Handler + 注入的 router）──────────
_MISSING = object()


@pytest.fixture
def make_service():
    """按需起一个真实的 `service.Server`，返回 (base_url, solver)。

    `chain` 是一个 `(solver) -> list[backend] | None` 的工厂：
    返回 None 表示"不配 router"（等价于 `solver/service.py:2044`
    `Handler.router = BackendRouter(backends) if len(backends) > 1 else None` 的单后端情形）。
    """
    started: list[tuple[svc.Server, svc.Solver]] = []
    snapshot = {
        "solver": svc.Handler.__dict__.get("solver", _MISSING),
        "token": getattr(svc.Handler, "token", _MISSING),
        "router": getattr(svc.Handler, "router", _MISSING),
    }

    def _make(*, allow_private_hosts: bool = False,
              max_inflight: int = svc.DEFAULT_MAX_INFLIGHT,
              chain=None):
        browser = FakeBrowser()
        solver = svc.Solver(
            browser_factory=factory_returning(browser),
            allow_private_hosts=allow_private_hosts,
            max_inflight=max_inflight,
        )
        assert wait_until(lambda: solver.health()[0] == 200), "替身浏览器应已就绪"
        svc.Handler.solver = solver
        svc.Handler.token = TOKEN
        # 必须与生产装配一致（`solver/service.py` 的 `main()` 里 `Handler.router = ...`）：
        # v4.1 起治理（缓存/准入/计数）绑在路由层，而不是绑在某个后端实现上。
        svc.Handler.router = _build_router(solver, chain(solver)) if chain is not None else None
        srv = svc.Server(("127.0.0.1", 0), svc.Handler)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()
        started.append((srv, solver))
        return f"http://127.0.0.1:{srv.server_address[1]}", solver

    try:
        yield _make
    finally:
        for srv, solver in started:
            srv.shutdown()
            srv.server_close()
            solver.close()
        # Handler 是类属性：不还原就会污染同进程里后面的测试文件
        for key, value in snapshot.items():
            if value is _MISSING:
                try:
                    delattr(svc.Handler, key)
                except AttributeError:
                    pass
            else:
                setattr(svc.Handler, key, value)


def _post(base: str, payload: dict, *, timeout: float = 30.0) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + "/solve", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _get(base: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def metric_value(text: str, name: str, *, labels: dict | None = None) -> float | None:
    """从 Prometheus 文本里取一条指标的值；不存在返回 None（**不是 0**，两者语义不同）。"""
    if labels:
        label_part = "".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        pattern = rf"^{re.escape(name)}\{{[^}}]*{re.escape(label_part)}[^}}]*\}}\s+([0-9.eE+-]+)$"
    else:
        pattern = rf"^{re.escape(name)}\s+([0-9.eE+-]+)$"
    m = re.search(pattern, text, re.MULTILINE)
    return float(m.group(1)) if m else None


def _metrics_snippet(text: str, needle: str) -> str:
    """把 /metrics 里相关的几行摘出来，方便看失败原因。"""
    return " | ".join(line for line in text.splitlines() if needle in line) or "（相关行不存在）"


# ═══════════════════════════════════════════════════════════
# 组 A（F5）｜治理不随后端变化：内网 URL 必须在**任何后端启动之前**被拒
# ═══════════════════════════════════════════════════════════
#: 四种后端组合。`[]` = 只有 camoufox（`main()` 只装配一个后端，`Handler.router` 为 None 的情形）；
#: 其余三种 = 多后端链上多出纯协议 / pydoll / 兜底后端的那一棒。
#: 生产装配顺序见 `solver/service.py:2029-2044`：cfsolve、pydoll 排在 Camoufox **之前**，
#: FlareSolverr 排在之后。这里一律把被测后端放在**链首**，因为要锁的是更强的命题——
#: "链首无论换成哪种后端，守卫都必须已经生效"，而当前实现恰好只在 Camoufox 那一棒里才有守卫。
BACKEND_COMBOS = [
    pytest.param([], id="only-camoufox"),
    pytest.param(["cfsolve"], id="cfsolve"),
    pytest.param(["pydoll"], id="pydoll"),
    pytest.param(["flaresolverr"], id="flaresolverr"),
]


@pytest.mark.parametrize("extra_backends", BACKEND_COMBOS)
def test_internal_url_rejected_before_any_backend_runs(make_service, extra_backends):
    """`POST /solve {"url": "http://127.0.0.1:<自己的端口>/health"}` 必须 400，且零后端调用。

    这是 P0-1 的自动化版本：v4.0 把守卫写在 `Solver.solve` 里（`service.py:1158`），
    而 handler 在配了 router 时根本不进 `Solver.solve`（`service.py:1625-1635`），
    于是链上排在 Camoufox 之前的后端**在建链之后就不再受任何治理**。
    修法（T-28）是把守卫上提到 handler 入口，与后端无关。
    """
    fakes = [RecordingBackend(name, result=success_result()) for name in extra_backends]
    base, _solver = make_service(chain=lambda s: list(fakes) + [be.CamoufoxBackend(s)])
    target = f"{base}/health"          # 本机回环地址：正是 SSRF 守卫该拦下的目标
    label = "仅 camoufox" if not extra_backends else "+".join(extra_backends)

    status, body = _post(base, {"url": target, "timeout": 10})

    assert status == 400, (
        f"[{label}] 内网 URL（{target}）必须在**任何后端启动之前**被拒并返回 400；"
        f"实际 HTTP {status}，响应={body}，{_calls_report(fakes)}。"
        "正确语义：SSRF 守卫是 handler 入口的职责，与链上有哪些后端无关"
    )
    assert body.get("error_code") == "invalid-request", (
        f"[{label}] 内网目标必须以 error_code=invalid-request 拒绝（与 /detect、Solver.solve 同一语义）；"
        f"实际 error_code={body.get('error_code')!r}，响应={body}"
    )
    assert all(f.calls == 0 for f in fakes), (
        f"[{label}] 内网 URL 不允许被派发给任何后端 —— 纯协议/pydoll 是子进程或自开 Chrome 直连，"
        f"调用即等于真实出站请求已发出；实际 {_calls_report(fakes)}"
    )


# ═══════════════════════════════════════════════════════════
# 组 B（F9）｜纯协议后端必须受治理：缓存 / 指标 / 准入
# ═══════════════════════════════════════════════════════════
def test_router_backend_result_is_cached(make_service):
    """同一「域名 + 出口」的凭证必须命中缓存：第二次请求不许再碰后端。"""
    fake = RecordingBackend("cfsolve", result=success_result())
    base, _solver = make_service(allow_private_hosts=True, chain=lambda s: [fake])

    s1, b1 = _post(base, {"url": HOST, "timeout": 10})
    s2, b2 = _post(base, {"url": HOST, "timeout": 10})

    assert s1 == 200 and b1.get("solved") is True, f"首次请求应解出凭证；实际 {s1} {b1}"
    assert b2.get("cached") is True, (
        f"第二次请求必须命中缓存（响应 cached=true），否则热路径每次都要重跑一次 18s 子进程；"
        f"实际 cached={b2.get('cached')!r}，响应={b2}"
    )
    assert fake.calls == 1, (
        f"命中缓存时后端 solve 只该被调用 1 次；实际 {fake.calls} 次（url={fake.urls}）。"
        "缓存是 Solver 的方法，router 路径（service.py:1625）绕过了它 —— F9"
    )


def test_router_backend_counts_appear_in_health_and_metrics(make_service):
    """走 router 的那一棒也必须进 `/health` 与 `/metrics`：观测数据不能说谎。"""
    fake = RecordingBackend("cfsolve", result=success_result())
    base, _solver = make_service(allow_private_hosts=True, chain=lambda s: [fake])

    status, body = _post(base, {"url": HOST, "timeout": 10})
    assert status == 200 and body.get("solved") is True, f"前置条件：应有 1 次成功求解；实际 {status} {body}"

    hcode, hraw = _get(base, "/health")
    health = json.loads(hraw)
    assert hcode == 200, f"/health 应可用；实际 {hcode} {hraw}"
    assert health.get("solves", 0) >= 1, (
        f"/health 的 solves 必须随每次真实后端求解递增（P1-1 实测过：一次真实后端尝试后仍是 0）；"
        f"实际 solves={health.get('solves')!r}，failures={health.get('failures')!r}"
    )

    _mcode, mtext = _get(base, "/metrics")
    n = metric_value(mtext, "cfsolve_solves_total")
    assert n is not None, f"/metrics 里应存在 cfsolve_solves_total；实际文本含这些行：{_metrics_snippet(mtext, 'solves')}"
    assert n >= 1, (
        f"cfsolve_solves_total 必须 >= 1（这次求解真的发生了，且是通过 cfsolve 后端）；实际 {n}。"
        f"/metrics 相关行：{_metrics_snippet(mtext, 'solves')}"
    )


def test_router_backend_respects_inflight_gate(make_service):
    """`max_inflight=1` 时并发 2 个请求：1 个成功，另 1 个必须立刻 429 busy（不许排队也不许放行）。"""
    fake = RecordingBackend("cfsolve", result=success_result(), delay=0.3)
    base, _solver = make_service(allow_private_hosts=True, max_inflight=1,
                                 chain=lambda s: [fake])

    results: list[tuple[int, dict]] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)      # 让两个请求真正同时在场
        except threading.BrokenBarrierError:
            pass
        out = _post(base, {"url": HOST, "timeout": 10})
        with results_lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(results) == 2, f"两个并发请求都应有响应；实际只拿到 {len(results)} 个：{results}"
    codes = sorted(status for status, _ in results)
    assert codes == [200, 429], (
        f"max_inflight=1 时并发 2 个请求必须一个成功、一个 429（准入拒绝是**立刻**返回，不是排队到超时）；"
        f"实际状态码={codes}，响应={results}。准入信号量只存在于 Solver.solve（service.py:1195），"
        "router 路径没有对应物 ⇒ 可被并发打穿（P1-1）"
    )
    rejected = [body for status, body in results if status == 429][0]
    assert rejected.get("error_code") == "busy", (
        f"被拒的那个必须是 error_code=busy（429 的契约码）；实际 {rejected.get('error_code')!r}"
    )
    accepted = [body for status, body in results if status == 200][0]
    assert accepted.get("ok") is True and accepted.get("solved") is True, (
        f"被放行的那个必须正常解出；实际 {accepted}"
    )
    assert fake.calls == 1, (
        f"闸门生效时后端只该被调用 1 次；实际 {fake.calls} 次 —— 说明两个请求都打到了后端"
    )


# ═══════════════════════════════════════════════════════════
# 组 C（F10）｜指标不得说谎：失败必须被计数
# ═══════════════════════════════════════════════════════════
def test_metrics_solves_total_counts_failures(make_service):
    """HELP 里写的是「真实求解次数（含失败）」，那失败就必须进 `solves_total`（或等价的失败计数器）。

    口径说明（两种都算通过，但**不许两者都为零**）：
      · 口径①：`cfsolve_solves_total` 含失败 —— 与当前 HELP 文本 `service.py:1402` 一致；
      · 口径②：新增 `cfsolve_backend_total{result="error"}` 单列失败，`solves_total` 只计成功
        —— 这时必须断言 `cfsolve_backend_total{result="error"} >= 1`。
    差别只在"失败算在总次数里还是单列"，共同点是**一次真实失败必须在指标里留下痕迹**；
    口径②必须同步改 HELP 文本，否则文档与行为再次不一致。
    """
    fake = RecordingBackend("cfsolve", error=SolveError(ErrorCode.UNSOLVED, "x", stage="wait"))
    base, _solver = make_service(allow_private_hosts=True, chain=lambda s: [fake])

    status, body = _post(base, {"url": HOST, "timeout": 10})
    assert fake.calls == 1, f"前置条件：后端应被调用 1 次；实际 {fake.calls} 次"
    assert status == 200 and body.get("error_code") == "unsolved", (
        f"前置条件：求解失败是 200 + solved:false + error_code=unsolved；实际 {status} {body}"
    )

    _mcode, mtext = _get(base, "/metrics")
    solves = metric_value(mtext, "cfsolve_solves_total")
    backend_errors = metric_value(mtext, "cfsolve_backend_total", labels={"result": "error"})
    counted = max(solves or 0.0, backend_errors or 0.0)

    assert counted >= 1, (
        "一次真实的后端失败之后，指标必须 >= 1（HELP 承诺 `cfsolve_solves_total` 含失败；"
        "若改用 `cfsolve_backend_total{result=\"error\"}` 单列，则该指标必须 >= 1）。"
        f"实际 cfsolve_solves_total={solves}，cfsolve_backend_total{{result=\"error\"}}={backend_errors}。"
        "观测数据撒谎比没有数据更坏：`_solves` 只在 Solver.solve 成功路径累加（service.py:1229），"
        f"router 路径与失败路径都不计。/metrics 相关行：{_metrics_snippet(mtext, 'total')}"
    )


# ═══════════════════════════════════════════════════════════
# 组 D｜正常路径回归护栏（**现在应为绿**）
# ═══════════════════════════════════════════════════════════
def test_no_route_field_does_not_probe(make_service, monkeypatch):
    """不传 `route` = fixed（v3 行为）：绝不允许额外探测，响应形状也不许多 `routed_by`。"""
    probed = {"n": 0, "urls": []}

    def spy(url, timeout=15.0, guard=None, **kwargs):
        probed["n"] += 1
        probed["urls"].append(url)
        return {
            "protected": True, "providers": [], "status": 403,
            "cloudflare_challenge_type": "managed",
            "recommendation": {"route": "browser", "reason": "-", "hint": "-"},
        }

    monkeypatch.setattr(svc, "probe_protection", spy)
    base, _solver = make_service(allow_private_hosts=True)

    status, body = _post(base, {"url": HOST, "timeout": 10})

    assert status == 200 and body.get("ok") is True, (
        f"不传 route 时应按 v3 行为正常求解并返回 200；实际 {status} {body}"
    )
    assert probed["n"] == 0, (
        f"不传 route 时绝不允许探测（每个 /solve 会白多一次出站请求，且可被打成放大器）；"
        f"实际探测 {probed['n']} 次，目标 {probed['urls']}"
    )
    assert "routed_by" not in body, (
        f"不传 route 时响应形状必须与 v3 完全一致（不含 routed_by）；实际响应={body}"
    )


def test_route_auto_skips_when_unprotected(make_service, monkeypatch):
    """无盾站：`route=auto` 必须**一次求解都不启动**，直接告诉调用方"没盾，直接请求"。"""
    monkeypatch.setattr(svc, "probe_protection", lambda url, timeout=15.0, guard=None, **kw: {
        "protected": False,
        "providers": [],
        "status": 200,
        "cloudflare_challenge_type": None,
        "recommendation": {"route": "direct", "reason": "-", "hint": "-"},
    })
    base, solver = make_service(allow_private_hosts=True)

    status, body = _post(base, {"url": HOST, "route": "auto"})

    assert status == 200, f"无盾站应 200（不是请求失败）；实际 {status} {body}"
    assert body.get("ok") is True and body.get("solved") is False and body.get("no_challenge") is True, (
        "无盾站必须返回 ok=true / solved=false / no_challenge=true（求解失败与'不需要求解'是两件事）；"
        f"实际响应={body}"
    )
    assert solver.health()[1]["solves"] == 0, (
        f"探测判定无盾时不许启动任何求解（省掉 16-20s 与 1.2GB 浏览器）；"
        f"实际 solves={solver.health()[1]['solves']}"
    )
