"""万盾嫁接层（captchas/ 子包 + solver/multicaptcha.py）的契约测试。

覆盖而不重测的部分：
  · 不实测任何厂商求解器（要 cloakbrowser + 真实挑战页，属于真机验证范畴）；
    这里只固化**嫁接层自己的契约**：类型注册、参数校验、成功谓词归一、
    detect/recommend 的分发指向、route=auto 的 multicaptcha 决策。
  · captchas/ 是自有副本：测试断言的是"我们的"接口，不追上游行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "solver"))

import detect as detect_mod  # noqa: E402
import multicaptcha as mc  # noqa: E402
import route as route_mod  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402


# ── 类型注册 ─────────────────────────────────────────


def test_all_eleven_types_registered():
    assert set(mc.CAPTCHA_TYPES) == {
        "turnstile_x", "recaptcha_x", "hcaptcha", "cloudflare_x", "awswaf_x",
        "botguard", "datadome", "perimeterx", "akamai", "aliyun", "arkose",
    }


def test_extended_names_do_not_collide_with_main_wants():
    """CF 主线 want 名（turnstile/recaptcha_v3/clearance/awswaf）绝不能被扩展类型占用。"""
    from challenges import Want
    main = {w.value for w in Want}
    assert not (set(mc.CAPTCHA_TYPES) & main)


def test_is_multicaptcha_type():
    assert mc.is_multicaptcha_type("hcaptcha")
    assert mc.is_multicaptcha_type(" TURNSTILE_X ")
    assert not mc.is_multicaptcha_type("turnstile")      # 主链路
    assert not mc.is_multicaptcha_type("clearance")
    assert not mc.is_multicaptcha_type("")


# ── 参数校验 ─────────────────────────────────────────


def test_unknown_type_rejected():
    with pytest.raises(SolveError) as ei:
        mc.validate_request({"type": "kasada"})
    assert ei.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("type_", ["turnstile_x", "recaptcha_x", "hcaptcha"])
def test_widget_types_require_sitekey(type_):
    with pytest.raises(SolveError):
        mc.validate_request({"type": type_, "url": "https://a.test/"})


def test_url_required_except_self_url_types():
    with pytest.raises(SolveError):
        mc.validate_request({"type": "datadome"})
    # botguard/perimeterx/aliyun/arkose 自带规范 URL
    mc.validate_request({"type": "arkose", "public_key": "k"})
    with pytest.raises(SolveError):
        mc.validate_request({"type": "aliyun"})           # aliyun 还要 scene_id+prefix


def test_aliyun_and_arkose_params():
    mc.validate_request({"type": "aliyun", "scene_id": "s", "prefix": "p"})
    with pytest.raises(SolveError):
        mc.validate_request({"type": "aliyun", "scene_id": "s"})
    mc.validate_request({"type": "arkose", "public_key": "k"})


# ── 成功谓词归一 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "solved"),
    [
        ({"token": "t"}, True),
        ({"cf_clearance": {"value": "c"}}, True),
        ({"verify_success": True}, True),
        ({"success": True}, True),
        ({"error": "no widget"}, False),
        ({}, False),
        # 求解器自己带的 solved 不得覆盖归一结论
        ({"solved": True, "error": "x"}, False),
    ],
)
def test_normalize_single_success_predicate(raw, solved):
    out = mc._normalize("hcaptcha", raw, 1.234)
    assert out["solved"] is solved
    assert out["ok"] is True
    assert out["elapsed"] == 1.234


def test_normalize_keeps_type_and_error():
    out = mc._normalize("datadome", {"error": "blocked"}, 0.5)
    assert out["type"] == "datadome" and out["error"] == "blocked"


# ── detect/recommend 分发指向 ────────────────────────


@pytest.mark.parametrize(
    ("cookies", "want"),
    [
        ({"datadome": "x"}, "datadome"),
        ({"_abck": "x"}, "akamai"),
        ({"_px3": "x"}, "perimeterx"),
    ],
)
def test_recommend_points_at_multicaptcha(cookies, want):
    rec = detect_mod.recommend(detect_mod.detect(cookies=cookies))
    assert rec["route"] == "multicaptcha"
    assert rec["want"] == want


def test_recommend_unsupported_stays_for_unknown_vendor():
    """Incapsula 等没有求解器的厂商必须仍然如实说"不支持"，不能假承诺。"""
    det = [detect_mod.Detection(id="detect-incapsula", name="Incapsula (Imperva)",
                                category="Anti-Bot", difficulty="Medium",
                                confidence=100, hits=["cookie:incap_ses_"])]
    rec = detect_mod.recommend(det)
    assert rec["route"] == "unsupported_or_browser"
    assert "Incapsula" in rec["reason"]


# ── route=auto 的自动分发决策 ────────────────────────


def test_route_auto_dispatches_multicaptcha():
    det = {"protected": True, "providers": [{"id": "detect-datadome", "name": "DataDome"}],
           "cloudflare_challenge_type": None}
    d = route_mod.decide(det, "auto")
    assert d.action == route_mod.ACTION_SOLVE
    assert d.prefer == "multicaptcha"
    assert d.detail["multicaptcha_want"] == "datadome"


def test_route_auto_cf_wins_over_multicaptcha():
    """CF 与万盾同时在场时优先走主链路（CF 是主线能力，不能被动摇）。"""
    det = {"protected": True,
           "providers": [{"id": "detect-cloudflare", "name": "Cloudflare"},
                         {"id": "detect-datadome", "name": "DataDome"}],
           "cloudflare_challenge_type": "managed"}
    d = route_mod.decide(det, "auto")
    assert d.prefer != "multicaptcha"
    assert d.reason.startswith("cloudflare-")


def test_route_auto_still_rejects_vendors_without_solver():
    det = {"protected": True, "providers": [{"id": "detect-kasada", "name": "Kasada"}],
           "cloudflare_challenge_type": None}
    d = route_mod.decide(det, "auto")
    assert d.action == route_mod.ACTION_REJECT


# ── 子包可导入（拷贝后的 import 路径没断）────────────


def test_captchas_package_importable_without_cloakbrowser():
    """solve.py 顶层 import cloakbrowser——本机没装时应是 ImportError 而不是路径错误。

    断言的是**包结构正确**：common 已改成 captchas.common，不再依赖 cwd。
    """
    import importlib
    sys.path.insert(0, str(ROOT / "captchas"))
    try:
        importlib.import_module("captchas.common.browser")
    finally:
        sys.path.pop(0)

# ── 分发逻辑（用假求解器模块，不装 cloakbrowser）────────────────

import asyncio  # noqa: E402
import types  # noqa: E402


def _install_fake(monkeypatch, name: str, **funcs):
    """把 `name` 塞进 sys.modules 成一个假求解器模块（顶层 import 会命中它）。"""
    mod = types.ModuleType(name)
    for fn_name, fn in funcs.items():
        setattr(mod, fn_name, fn)
    monkeypatch.setitem(sys.modules, name, mod)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    ("type_", "body", "modname", "fname", "ret"),
    [
        ("turnstile_x", {"sitekey": "sk", "url": "https://a.test/"},
         "turnstile.solve", "solve_turnstile", {"token": "tok", "method": "route"}),
        ("hcaptcha", {"sitekey": "sk", "url": "https://a.test/"},
         "hcaptcha.solve", "solve_hcaptcha", {"token": "tok"}),
        ("cloudflare_x", {"url": "https://a.test/"},
         "cloudflare.solve", "solve_cf_clearance", {"cf_clearance": "abc", "success": True}),
        ("awswaf_x", {"url": "https://a.test/"},
         "awswaf.solve", "solve_aws_waf", {"success": True, "token": "awt"}),
        ("datadome", {"url": "https://a.test/"},
         "datadome.solve", "solve_datadome", {"success": True}),
        ("perimeterx", {"url": "https://a.test/"},
         "perimeterx.solve", "solve_perimeterx", {"success": True}),
        ("akamai", {"url": "https://a.test/"},
         "akamai.solve", "solve_akamai", {"success": True}),
        ("recaptcha_x", {"version": "v2", "sitekey": "sk", "url": "https://a.test/"},
         "recaptcha.solve", "solve_recaptcha_v2", {"token": "rc"}),
    ],
)
def test_dispatch_routes_to_right_solver(monkeypatch, type_, body, modname, fname, ret):
    """每种类型都必须落到自己的求解器函数上，且结果被归一（顶层 solved）。"""
    seen = {}

    async def fake(*a, **k):
        seen["args"] = (a, k)
        return dict(ret)

    _EXTRA = {
        "hcaptcha.solve": ["solve_hcaptcha", "solve_hcaptcha_invisible"],
        "recaptcha.solve": [
            "solve_recaptcha_v3", "solve_recaptcha_v3_realpage",
            "solve_recaptcha_invisible", "solve_recaptcha_invisible_realpage",
            "solve_recaptcha_v2", "solve_recaptcha_v2_realpage",
        ],
    }
    names = set(_EXTRA.get(modname, [])) | {fname}
    _install_fake(monkeypatch, modname, **{n: fake for n in names})
    body["type"] = type_
    out = _run(mc.solve_async(body, timeout_s=5))
    assert out["type"] == type_
    assert out["solved"] is True
    assert "args" in seen, f"{type_} 没有调到 {modname}.{fname}"


def test_dispatch_unknown_version_rejected(monkeypatch):
    async def fake(*a, **k):  # pragma: no cover - 不该被调用
        raise AssertionError("version 非法时不应调用求解器")

    _install_fake(monkeypatch, "recaptcha.solve",
                  **{n: fake for n in ("solve_recaptcha_v3", "solve_recaptcha_v3_realpage",
                                       "solve_recaptcha_invisible", "solve_recaptcha_invisible_realpage",
                                       "solve_recaptcha_v2", "solve_recaptcha_v2_realpage")})
    with pytest.raises(SolveError) as ei:
        _run(mc.solve_async({"type": "recaptcha_x", "version": "v9",
                             "sitekey": "sk", "url": "https://a.test/"}, timeout_s=5))
    assert ei.value.code is ErrorCode.INVALID_REQUEST


def test_dispatch_timeout_returns_unsolved(monkeypatch):
    """超时不抛异常：返回 200 + solved=false（与主服务错误契约一致）。"""
    async def slow(*a, **k):
        await asyncio.sleep(60)
        return {"token": "never"}  # pragma: no cover

    monkeypatch.setattr(mc, "_TIMEOUT_GRACE", 0.3)
    _install_fake(monkeypatch, "datadome.solve", solve_datadome=slow)
    out = _run(mc.solve_async({"type": "datadome", "url": "https://a.test/"}, timeout_s=1))
    assert out["solved"] is False and "timed out" in out["error"]
    assert out["ok"] is True


def test_dispatch_internal_error_wrapped(monkeypatch):
    """求解器内部崩溃 → SolveError(INTERNAL)，不把裸异常泄给 HTTP 层。"""
    async def boom(*a, **k):
        raise RuntimeError("cloak explodiu")

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=boom)
    with pytest.raises(SolveError) as ei:
        _run(mc.solve_async({"type": "datadome", "url": "https://a.test/"}, timeout_s=5))
    assert ei.value.code is ErrorCode.INTERNAL


def test_inflight_tracking_cleared_after_solve(monkeypatch):
    """在途表必须在结束时清空（否则 /status 会永远显示假任务）。"""
    async def fake(*a, **k):
        return {"success": True}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    _run(mc.solve_async({"type": "datadome", "url": "https://a.test/"}, timeout_s=5))
    assert mc.inflight_tasks() == []


def test_aliyun_runs_in_subprocess(monkeypatch):
    """aliyun 必须走子进程（CDP 鼠标时序对主线程事件循环敏感——实测 0/12 vs 3/3）。"""
    calls = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b'__ALIYUN_RESULT__{"solved": true, "token": "T001"}\n', b""

    async def fake_exec(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(mc.asyncio, "create_subprocess_exec", fake_exec)
    out = _run(mc.solve_async({"type": "aliyun", "scene_id": "s", "prefix": "p",
                               "url": "https://a.test/"}, timeout_s=5))
    assert calls["args"][1] == "-m" and calls["args"][2] == "aliyun._run"
    assert out["solved"] is True and out["token"] == "T001"


# ── detect.py 新增分支的覆盖 ─────────────────────────


def test_recommend_recognized_but_unsupported_vendor():
    """被识别、但确实没有求解器的厂商（如腾讯 turing）：如实说"解不了"。

    v6.2 起判断口径改为**先看全集**（`solver/vendors.py` 单一事实源）：
    只要命中里有任何一家解不了，这页整体就是过不去的 —— 拿得到 A 家的凭证
    也进不去 B 家。文案也从"暂不支持"改成"没有对应求解器 + 会白等一个超时"。
    """
    det = detect_mod.detect(
        url="https://turing.captcha.qcloud.com/cap_union_new_show",
        body='<script src="https://turing.captcha.qcloud.com/TCaptcha.js"></script>')
    rec = detect_mod.recommend(det)
    assert rec["route"] == "unsupported_or_browser"
    assert "解不了" in rec["reason"]
    assert "白等" in rec["hint"]


def test_header_signature_hits():
    """header 类特征也得命中（Amazon WAF 的 x-amzn-waf-action 等走这条路径）。"""
    hits = detect_mod.detect(headers={"X-Datadome-Cid": "abc"})
    assert any(d.id == "detect-datadome" for d in hits)


# ── HTTP 旁路（真实 Handler + 替身浏览器，求解器用假模块）─────────

import json as _json  # noqa: E402
import threading as _threading  # noqa: E402
import urllib.error as _urlerr  # noqa: E402
import urllib.request as _urlreq  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import service as svc  # noqa: E402
from test_service import FakeBrowser, factory_returning, wait_until  # noqa: E402


@pytest.fixture()
def live_service():
    browser = FakeBrowser()
    s = svc.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: s.health()[0] == 200)
    svc.Handler.solver = s
    svc.Handler.token = "t"
    svc.Handler.router = None
    srv = svc.Server(("127.0.0.1", 0), svc.Handler)
    _threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                      daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}", s
    finally:
        srv.shutdown()
        srv.server_close()
        s.close()


def _post(base, payload):
    req = _urlreq.Request(
        base + "/solve", data=_json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer t"}, method="POST")
    try:
        with _urlreq.urlopen(req, timeout=20) as r:
            return r.status, _json.loads(r.read().decode())
    except _urlerr.HTTPError as e:
        return e.code, _json.loads(e.read().decode())


def test_http_solve_dispatches_to_multicaptcha(live_service, monkeypatch):
    """want=datadome → 走 captchas/，且**不让主链路后端动一次**。"""
    base, solver = live_service

    async def fake(*a, **k):
        return {"success": True, "cookies": [{"name": "datadome", "value": "d"}]}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    status, body = _post(base, {"url": "https://example.com/", "want": "datadome"})
    assert status == 200 and body["solved"] is True
    assert body["routed_by"]["backend"] == "captchas"
    assert body["routed_by"]["type"] == "datadome"
    assert solver.health()[1]["solves"] == 0, "嫁接层不该启动主链路的浏览器求解"


def test_http_solve_multicaptcha_unsolved_still_200(live_service, monkeypatch):
    """求解失败 → 200 + solved:false + error（与主服务的错误契约一致）。"""
    base, _ = live_service

    async def fake(*a, **k):
        return {"error": "gate not rendered"}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    status, body = _post(base, {"url": "https://example.com/", "want": "datadome"})
    assert status == 200 and body["solved"] is False
    assert body["error"] == "gate not rendered"


def test_http_solve_multicaptcha_missing_param_400(live_service):
    """缺 sitekey → 400（不是 200 假失败）。"""
    base, _ = live_service
    status, body = _post(base, {"url": "https://example.com/", "want": "hcaptcha"})
    assert status == 400
    assert "sitekey" in body["error"]


def test_http_solve_multicaptcha_private_host_blocked(live_service, monkeypatch):
    """SSRF：私网目标必须被拒（与主链路同一道守卫）。"""
    base, _ = live_service
    s = svc.Handler.solver
    s._allow_private_hosts = False
    try:
        status, body = _post(base, {"url": "http://127.0.0.1/", "want": "datadome"})
    finally:
        s._allow_private_hosts = True
    assert status == 400


def test_http_solve_main_want_unaffected(live_service):
    """回归护栏：主链路 want=clearance 仍然走原来的路（routed_by 不应出现 captchas）。"""
    base, _ = live_service
    status, body = _post(base, {"url": "https://example.com/", "want": "clearance"})
    assert status == 200
    assert (body.get("routed_by") or {}).get("backend") != "captchas"


def test_missing_cloakbrowser_maps_to_not_configured(monkeypatch):
    """缺 cloakbrowser 时必须报 NOT_CONFIGURED + 安装提示，而不是 internal（"服务崩了"）。"""
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("cloakbrowser"):
            raise ImportError("No module named 'cloakbrowser'")
        return real_import(name, *a, **k)

    # 假模块在函数体内 import 一个不存在的依赖 → ImportError
    mod = types.ModuleType("datadome.solve")

    async def solve_datadome(*a, **k):  # pragma: no cover - 不会执行到
        raise AssertionError

    def module_level_import():
        import cloakbrowser  # noqa: F401  (fake_import 会拦下)

    mod.solve_datadome = lambda *a, **k: module_level_import()  # 同步抛
    monkeypatch.setitem(sys.modules, "datadome.solve", mod)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    try:
        with pytest.raises(SolveError) as ei:
            _run(mc.solve_async({"type": "datadome", "url": "https://a.test/"}, timeout_s=5))
    finally:
        monkeypatch.setattr(builtins, "__import__", real_import)
    assert ei.value.code is ErrorCode.NOT_CONFIGURED
    assert "cloakbrowser" in ei.value.hint


def test_http_self_url_type_allows_missing_url(live_service, monkeypatch):
    """自带规范 URL 的类型（datadome 之外的 botguard/arkose 等）不传 url 也必须受理。"""
    base, _ = live_service

    async def fake(*a, **k):
        return {"success": True, "px3": "x"}

    _install_fake(monkeypatch, "perimeterx.solve", solve_perimeterx=fake)
    status, body = _post(base, {"want": "perimeterx"})   # 无 url
    assert status == 200 and body["solved"] is True


def test_http_timeout_field_accepted(live_service, monkeypatch):
    """主链路的 `timeout` 字段在扩展通道也应生效（不必记两套字段名）。"""
    base, _ = live_service

    async def fake(*a, **k):
        return {"token": "t"}

    _install_fake(monkeypatch, "hcaptcha.solve",
                  solve_hcaptcha=fake, solve_hcaptcha_invisible=fake)
    status, body = _post(base, {"url": "https://example.com/", "want": "hcaptcha",
                                "sitekey": "sk", "timeout": 7})
    assert status == 200 and body["solved"] is True


def test_http_route_auto_dispatches_multicaptcha(live_service, monkeypatch):
    """route=auto 探测到 DataDome → 自动分发 captchas/，主链路后端一次不启。"""
    base, solver = live_service
    payload = {
        "url": "https://example.com/", "status": 403, "protected": True,
        "providers": [{"id": "detect-datadome", "name": "DataDome"}],
        "cloudflare_challenge_type": None,
        "recommendation": {"route": "multicaptcha", "want": "datadome", "reason": "-", "hint": "-"},
    }
    monkeypatch.setattr(svc, "probe_protection", lambda url, timeout=15.0, guard=None: payload)

    async def fake(*a, **k):
        return {"success": True}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    status, body = _post(base, {"url": "https://example.com/", "route": "auto"})
    assert status == 200 and body["solved"] is True
    assert body["routed_by"]["detail"]["multicaptcha_want"] == "datadome"
    assert solver.health()[1]["solves"] == 0, "自动分发不该经过主链路浏览器"


def test_same_type_concurrent_across_threads(monkeypatch):
    """回归：主服务每个请求在自己的线程里 asyncio.run 一个新事件循环。

    曾经的实现用 asyncio.Lock 做每类型串行锁 —— 锁会绑定首个事件循环，
    第二个同类型请求在另一个循环里 await 直接 RuntimeError 并**永久挂住**
    （实测 4 线程同时求解：2 个报错、进程要 kill）。必须是 threading.Lock。
    """
    import threading
    import time as _time

    async def fake(*a, **k):
        await asyncio.sleep(0.15)
        return {"success": True}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    results, errors = [], []

    def worker():
        try:
            results.append(_run(mc.solve_async(
                {"type": "datadome", "url": "https://a.test/"}, timeout_s=5)))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    t0 = _time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"跨事件循环使用锁/状态出错：{errors}"
    assert all(r["solved"] for r in results) and len(results) == 4
    # 同类型串行：4×0.15s 不应并发完成（并发的话 ≈0.15s）
    assert _time.monotonic() - t0 >= 0.5


def test_lock_contention_reports_busy_not_hang(monkeypatch):
    """等锁超时必须报"排队超时"，而不是无限挂住。"""
    async def slow(*a, **k):
        await asyncio.sleep(5)
        return {"success": True}  # pragma: no cover

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=slow)
    monkeypatch.setattr(mc, "_TIMEOUT_GRACE", 0.0)
    lock = mc._lock_for("datadome")
    assert lock.acquire(blocking=False)
    try:
        out = _run(mc.solve_async({"type": "datadome", "url": "https://a.test/"},
                                  timeout_s=0.2))
        assert out["solved"] is False and "排队超时" in out["error"]
    finally:
        lock.release()


def test_openapi_enum_covers_every_extended_type():
    """规格与实现必须一致：openapi.json 的 want 枚举要含全部扩展类型。

    这条是 validate-openapi.mjs 的补充——它只校验路由与鉴权声明，
    不校验**字段枚举**；枚举漏一个值，生成的客户端就会拒绝一个真实可用的类型。
    """
    import json
    spec = json.loads((ROOT / "docs" / "openapi.json").read_text(encoding="utf-8"))
    enum = spec["components"]["schemas"]["SolveRequest"]["properties"]["want"]["enum"]
    missing = set(mc.CAPTCHA_TYPES) - set(enum)
    assert not missing, f"openapi 的 want 枚举缺少：{sorted(missing)}"
    # 反向：枚举里的非空值必须真能受理（主链路 want + 扩展类型）
    from challenges import Want
    known = set(mc.CAPTCHA_TYPES) | {w.value for w in Want}
    extra = {v for v in enum if v and v not in known}
    assert not extra, f"openapi 枚举声明了不存在的 want：{sorted(extra)}"


def test_http_bypass_uses_sticky_egress_proxy(live_service, monkeypatch):
    """cookie 类产物绑出口 IP：嫁接层必须沿用主链路为该域粘住的出口。

    否则拿到的 datadome/_px3 cookie 在客户端换 IP 回放就是静默 403 ——
    而且响应要回传 egress_id，客户端才知道该从哪个出口回放。
    """
    base, solver = live_service
    from proxypool import ProxyPool
    pool = ProxyPool(["http://user:pw@10.9.9.9:3128"])
    solver._pool = pool          # 直接挂池（不改 bootstrap）
    seen = {}

    async def fake(*a, **k):
        seen["proxy"] = k.get("proxy") or (a[-1] if a else "")
        return {"success": True}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    try:
        status, body = _post(base, {"url": "https://example.com/", "want": "datadome"})
    finally:
        solver._pool = None
    assert status == 200 and body["solved"] is True
    assert "10.9.9.9" in seen["proxy"], f"没用粘性出口：{seen}"
    assert body["egress_id"].startswith(("ip:", "px:"))


def test_http_explicit_proxy_not_overridden(live_service, monkeypatch):
    """调用方显式给 proxy 时必须用它自己的（有人故意要多出口轮换）。"""
    base, solver = live_service
    from proxypool import ProxyPool
    solver._pool = ProxyPool(["http://pool:pw@10.9.9.9:3128"])
    seen = {}

    async def fake(*a, **k):
        seen["proxy"] = k.get("proxy") or ""
        return {"success": True}

    _install_fake(monkeypatch, "datadome.solve", solve_datadome=fake)
    try:
        status, _ = _post(base, {"url": "https://example.com/", "want": "datadome",
                                 "proxy": "http://mine:pw@1.2.3.4:8080"})
    finally:
        solver._pool = None
    assert status == 200
    assert "1.2.3.4" in seen["proxy"] and "10.9.9.9" not in seen["proxy"]


# ── 厂商求解器模块级锁（captchas.common.looplock）──────────────


def test_looplock_survives_multiple_event_loops(monkeypatch):
    """回归：8 个厂商求解器在模块级各持一把锁。上游是单事件循环（uvicorn）所以用
    asyncio.Lock 没问题；本服务每请求一个新循环 → 第二个请求 RuntimeError 并挂死。
    loop_lock() 必须跨循环可用（跨线程互斥由 multicaptcha 的 threading.Lock 负责）。
    """
    import importlib
    sys.path.insert(0, str(ROOT))
    try:
        mod = importlib.import_module("captchas.common.looplock")
    finally:
        sys.path.pop(0)

    lock = mod.loop_lock()
    seen = []

    async def use(tag):
        async with lock:
            seen.append(tag)
            await asyncio.sleep(0.01)

    # 三个独立事件循环，模拟三个请求线程
    for i in range(3):
        asyncio.run(use(i))
    assert sorted(seen) == [0, 1, 2]


def test_vendor_solvers_do_not_use_bare_asyncio_lock():
    """静态守门：厂商求解器模块级 `_solve_lock` 不许再是裸 asyncio.Lock。

    这条是给未来 cherry-pick 上游改动设的护栏：上游一更新就可能把 loop_lock()
    覆盖回 asyncio.Lock()，而那个 bug 只在"多请求并发"时才炸 —— 单测很难自然发现。
    """
    bad = []
    for p in (ROOT / "captchas").glob("*/solve.py"):
        text = p.read_text(encoding="utf-8")
        if "_solve_lock = asyncio.Lock()" in text:
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"这些文件仍在用模块级 asyncio.Lock（跨事件循环会挂死）：{bad}"


def test_looplock_serializes_within_one_loop():
    """同一事件循环内两个协程并发进入：必须串行，且各自正确释放（不能配错锁）。"""
    import importlib
    sys.path.insert(0, str(ROOT))
    try:
        mod = importlib.import_module("captchas.common.looplock")
    finally:
        sys.path.pop(0)
    lock = mod.loop_lock()
    order = []

    async def one(tag, delay):
        async with lock:
            order.append(("in", tag))
            await asyncio.sleep(delay)
            order.append(("out", tag))

    async def main():
        await asyncio.gather(one("a", 0.05), one("b", 0.01))

    asyncio.run(main())
    # 串行：a 完全结束后 b 才开始（若锁释放错误会交错）
    assert order == [("in", "a"), ("out", "a"), ("in", "b"), ("out", "b")], order
    assert lock.locked() is False, "退出后锁必须释放，否则后续请求永远排队"
