"""万盾嫁接层：把 captchas/ 子包（源自 captcha-solver 的 11 厂商求解器）接入主服务。

设计约束（用户明确的架构决策）：
  · CF 主线（Want 枚举 / backends 回落链 / 缓存 / Go 客户端）**一行不动**。
  · 本层是**旁路**：主 /solve 的 want 命中主项目四类 → 走原链路；
    命中扩展类型 → 分发到 captchas/*，结果归一成主服务的响应形状。
  · captchas/ 是我们自己的副本，不追上游——上游更新自行 cherry-pick。

归一契约（与主 /solve 对齐）：
  · 成功 → {"ok": True, "solved": True, "type": ..., <产物字段>, "elapsed": ...}
  · 跑了但没解出 → {"ok": True, "solved": False, "error": ..., "elapsed": ...}（HTTP 200）
  · 参数错 → 抛 SolveError(INVALID_REQUEST)；内部崩溃 → SolveError(INTERNAL)
  · 一次性 token 绝不进缓存（与主项目 TOKEN_WANTS 同一纪律）

CF 类型不经过本层：turnstile/clearance/awswaf/recaptcha_v3 由主链路处理
（本层的 turnstile/recaptcha 是**另一套实现**——CloakBrowser 引擎 + route 拦截，
用于非 CF 站点上的同类 widget；对 CF 站点请用主链路，别在这里重复求解）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

try:
    from .errors import ErrorCode, SolveError
except ImportError:
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]

log = logging.getLogger("multicaptcha")

#: captchas/ 子包根目录（solve.py 以 cwd/import-path 方式依赖包内相对导入，
#: aliyun 还要求以子进程方式在包根目录跑——见 server.py 的实证注释）
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CAPTCHAS_ROOT = _REPO_ROOT / "captchas"

#: 扩展求解类型 → captchas 子模块。**刻意避开**主链路 want 名：
#: 主链路已占 turnstile / recaptcha_v3 / clearance / awswaf；
#: 扩展类型带 `_x` 后缀命名（eXtended / 嫁接），永不冲突。
CAPTCHA_TYPES: dict[str, str] = {
    "turnstile_x": "turnstile",
    "recaptcha_x": "recaptcha",
    "hcaptcha": "hcaptcha",
    "cloudflare_x": "cloudflare",
    "awswaf_x": "awswaf",
    "botguard": "botguard",
    "datadome": "datadome",
    "perimeterx": "perimeterx",
    "akamai": "akamai",
    "aliyun": "aliyun",
    "arkose": "arkose",
}

#: 页面级 cookie 采集类（无 sitekey，产物是 cookie 记录）
_PAGE_LEVEL = ("cloudflare_x", "awswaf_x", "botguard", "datadome", "perimeterx", "akamai")
#: 自带规范 URL、调用方可不传 url 的类型
SELF_URL_TYPES = _SELF_URL = ("botguard", "perimeterx", "aliyun", "arkose")

#: 每类型并发锁：不同类型可并行，同类型串行（与原 server.py 同语义）。
#:
#: ⚠️ 必须是 **threading.Lock**，不能是 asyncio.Lock：主服务是 ThreadingHTTPServer，
#: 每个请求在**自己的线程里 asyncio.run 一个新事件循环**。asyncio.Lock 会把自身绑定到
#: 首次使用它的事件循环，第二个同类型请求在另一个循环里 await 它 → RuntimeError
#: "bound to a different event loop" 并**永久挂住**（已实测复现：4 线程同时 datadome，
#: 2 个直接报错、进程需要 kill）。同一把 threading.Lock 跨循环跨线程都安全。
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()

#: 在途表：ThreadingHTTPServer 每请求一线程，增删与 /status 的快照遍历会并发。
#: 不加锁时 `list(dict.values())` 可能撞上"dictionary changed size during iteration"。
_inflight_guard = threading.Lock()
_inflight: dict[int, dict] = {}
_task_ids = 0

#: 默认超时（秒），按类型可覆盖（与原 server.py 一致）
_DEFAULT_TIMEOUTS = {"perimeterx": 200, "arkose": 120, "botguard": 90, "akamai": 90}

#: 求解器自己该在 deadline 内超时返回；这条外层宽限只兜"它自己卡死"的情况
#: （多等 30s 后强制取消，把锁释放掉）。测试里可覆盖以缩短等待。
_TIMEOUT_GRACE = 30.0


def is_multicaptcha_type(want: str) -> bool:
    return (want or "").strip().lower() in CAPTCHA_TYPES


def supported_types() -> list[str]:
    return sorted(CAPTCHA_TYPES)


def _lock_for(type_: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(type_)
        if lock is None:
            lock = _locks[type_] = threading.Lock()
        return lock


def _normalize(type_: str, result: dict, elapsed: float) -> dict:
    """把 captchas/ 各求解器的五花八门成功信号归一成主服务响应形状。

    单一成功谓词（源自原 server.py 的 _is_solved）：token / cf_clearance /
    verify_success / success 任一为真 = 解出。统一加顶层 solved bool。
    """
    result = dict(result or {})
    solved = bool(result.get("token") or result.get("cf_clearance")
                  or result.get("verify_success") or result.get("success"))
    out = {"type": type_, "solved": solved, "ok": True, "elapsed": round(elapsed, 3)}
    out.update(result)
    out["solved"] = solved          # 防止 result 里带 solved 覆盖归一结果
    # 失败必须带一句可行动的原因：求解器可能返回 {"error": None} 或干脆不给 error，
    # setdefault 对"键存在但值为 None"无效，所以显式判空。
    if solved:
        out.setdefault("error", None)
    elif not out.get("error"):
        out["error"] = "no product harvested"
    return out


def validate_request(body: dict) -> None:
    """参数校验（移植自原 server.py /solve 的前置检查）。失败抛 SolveError。"""
    type_ = (body.get("type") or body.get("want") or "").strip().lower()
    if type_ not in CAPTCHA_TYPES:
        raise SolveError(
            ErrorCode.INVALID_REQUEST,
            f"未知扩展类型 {type_!r}；可选：{supported_types()}",
            stage="validate",
            hint="扩展类型带 _x 后缀以避开主链路 want 名；不带后缀的（hcaptcha/datadome 等）无冲突直接用",
        )
    url = body.get("url") or ""
    if not url and type_ not in _SELF_URL:
        raise SolveError(
            ErrorCode.INVALID_REQUEST, f"type={type_} 需要 url", stage="validate")
    if type_ in ("turnstile_x", "recaptcha_x", "hcaptcha") and not body.get("sitekey"):
        raise SolveError(
            ErrorCode.INVALID_REQUEST, f"type={type_} 需要 sitekey", stage="validate")
    if type_ == "aliyun" and not (body.get("scene_id") and body.get("prefix")):
        raise SolveError(
            ErrorCode.INVALID_REQUEST, "type=aliyun 需要 scene_id + prefix", stage="validate")
    if type_ == "arkose" and not body.get("public_key"):
        raise SolveError(
            ErrorCode.INVALID_REQUEST, "type=arkose 需要 public_key", stage="validate")


async def solve_async(body: dict, timeout_s: float = 0) -> dict:
    """分发到 captchas/ 对应求解器并归一结果。同类型串行，跨类型并行。"""
    type_ = (body.get("type") or body.get("want") or "").strip().lower()
    module = CAPTCHA_TYPES[type_]
    # 求解器内部用 `from captchas.common.x import ...`（绝对包名），同时各自以顶层
    # 包名 `turnstile.solve` / `aliyun.solve` 被导入 —— 两个根都要在 path 上：
    #   · _REPO_ROOT  → `captchas.common` 可解析
    #   · _CAPTCHAS_ROOT → `turnstile` / `aliyun` 等顶层包名可解析
    with _locks_guard:      # 复用同一把守卫：都是"一次性全局副作用"
        for _p in (str(_CAPTCHAS_ROOT), str(_REPO_ROOT)):
            if _p not in sys.path:
                sys.path.insert(0, _p)
    deadline = timeout_s or body.get("timeout_s") or _DEFAULT_TIMEOUTS.get(type_, 60)
    proxy = body.get("proxy") or ""
    url = body.get("url") or ""
    sitekey = body.get("sitekey") or ""

    task_id = id(body)
    with _inflight_guard:
        _inflight[task_id] = {"type": type_, "url": (url or "")[:60], "started": time.time()}
    t0 = time.monotonic()
    # 等锁的时间也算进 deadline：同类型排队过久应当明确报忙，而不是无限挂住。
    lock = _lock_for(type_)
    if not lock.acquire(timeout=deadline + _TIMEOUT_GRACE):
        with _inflight_guard:
            _inflight.pop(task_id, None)
        return {"type": type_, "solved": False, "ok": True,
                "error": f"同类求解排队超时（>{deadline}s）", "elapsed": 0.0}
    try:
        return await asyncio.wait_for(
            _dispatch(module, type_, body, url, sitekey, proxy, deadline),
            timeout=deadline + _TIMEOUT_GRACE)
    except (TimeoutError, asyncio.TimeoutError):
        elapsed = time.monotonic() - t0
        return {"type": type_, "solved": False, "ok": True,
                "error": f"solve timed out after {deadline}s", "elapsed": round(elapsed, 3)}
    except SolveError:
        raise
    except ImportError as e:
        # 缺 cloakbrowser / onnxruntime 等依赖：这是**环境级**错误，给 NOT_CONFIGURED
        # 而不是 internal —— 调用方看到 5xx + internal 只会以为服务崩了，看不到"该装什么"。
        raise SolveError(
            ErrorCode.NOT_CONFIGURED, f"万盾求解依赖缺失：{e}", stage="admit",
            hint="pip install cloakbrowser（自托管反检测 Chromium）；"
                 "aliyun/arkose 另需 onnxruntime + opencv-python-headless",
        ) from e
    except Exception as e:
        log.error("multicaptcha solve failed: %s", e, exc_info=True)
        raise SolveError(ErrorCode.INTERNAL, str(e), stage="solve") from e
    finally:
        lock.release()
        with _inflight_guard:
            _inflight.pop(task_id, None)


def inflight_tasks() -> list[dict]:
    with _inflight_guard:
        return list(_inflight.values())


async def _dispatch(module, type_, body, url, sitekey, proxy, deadline) -> dict:
    """与原 server.py._dispatch 同构的分发；超时语义靠外层 wait_for 兜底。"""
    real_page = bool(body.get("real_page"))
    pre = body.get("pre_actions")
    post = body.get("post_fetch")
    action = body.get("action")

    if type_ == "turnstile_x":
        from turnstile.solve import solve_turnstile
        r = await solve_turnstile(sitekey, url, action, body.get("cdata"), proxy=proxy)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "hcaptcha":
        from hcaptcha.solve import solve_hcaptcha, solve_hcaptcha_invisible
        if action == "invisible":
            r = await solve_hcaptcha_invisible(sitekey, url, proxy=proxy)
        else:
            r = await solve_hcaptcha(sitekey, url, proxy=proxy)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "cloudflare_x":
        from cloudflare.solve import solve_cf_clearance
        r = await solve_cf_clearance(url, proxy, deadline, pre, post)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "awswaf_x":
        from awswaf.solve import solve_aws_waf
        r = await solve_aws_waf(url, proxy, deadline, pre, post)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "botguard":
        from botguard.solve import solve_botguard
        r = await solve_botguard(url=url, email=body.get("email"),
                                 password=body.get("password"), proxy=proxy,
                                 timeout_s=deadline, pre_actions=pre)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "datadome":
        from datadome.solve import solve_datadome
        r = await solve_datadome(url=url, referer=body.get("referer"),
                                 proxy=proxy, timeout_s=deadline)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "perimeterx":
        from perimeterx.solve import solve_perimeterx
        r = await solve_perimeterx(url=url, render_flow=body.get("render_flow") or "outlook_signup",
                                   proxy=proxy, timeout_s=deadline)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "akamai":
        from akamai.solve import solve_akamai
        r = await solve_akamai(url, proxy, deadline, pre, post)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "aliyun":
        # 原实证结论：拖动轨迹的 CDP 时序对主线程事件循环敏感，必须子进程跑
        # （asyncio.to_thread 也失败）。保留该模式（见 captchas/aliyun/）。
        import json as _json
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "aliyun._run",
            body.get("scene_id") or "", body.get("prefix") or "",
            body.get("region") or "sgp", str(deadline), proxy,
            cwd=str(_CAPTCHAS_ROOT),
            env=_subprocess_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, _err = await asyncio.wait_for(proc.communicate(), timeout=deadline + 30)
        except asyncio.TimeoutError:
            proc.kill()
            return {"type": type_, "solved": False, "ok": True,
                    "error": "subprocess deadline", "elapsed": deadline}
        r = {"error": "no result from runner"}
        for line in (out or b"").decode(errors="replace").splitlines():
            if line.startswith("__ALIYUN_RESULT__"):
                r = _json.loads(line[len("__ALIYUN_RESULT__"):])
                break
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    if type_ == "arkose":
        from arkose.solve import solve_arkose
        r = await solve_arkose(public_key=body.get("public_key") or "",
                               page_url=url, game_type=body.get("game_type") or "4",
                               proxy=proxy, timeout_s=deadline, pre_actions=pre)
        return _normalize(type_, r, time.monotonic() - _t0_of(body))

    # recaptcha_x：v2/v3/invisible × real_page
    from recaptcha.solve import (
        solve_recaptcha_v3, solve_recaptcha_v3_realpage, solve_recaptcha_invisible,
        solve_recaptcha_invisible_realpage, solve_recaptcha_v2, solve_recaptcha_v2_realpage,
    )
    version = body.get("version") or "v2"
    enterprise = bool(body.get("enterprise"))
    classifier = body.get("classifier")
    a = body.get("action") or "submit"
    if version == "v3":
        if real_page:
            r = await solve_recaptcha_v3_realpage(url, sitekey, a, enterprise=enterprise,
                                                  timeout_s=deadline, pre_actions=pre, proxy=proxy)
        else:
            r = await solve_recaptcha_v3(sitekey, url, a, body.get("secret"),
                                         enterprise=enterprise, proxy=proxy)
    elif version == "invisible":
        if real_page:
            r = await solve_recaptcha_invisible_realpage(url, sitekey, a, enterprise=enterprise,
                                                         timeout_s=deadline, pre_actions=pre,
                                                         proxy=proxy, classifier=classifier)
        else:
            r = await solve_recaptcha_invisible(sitekey, url, a, enterprise=enterprise, proxy=proxy)
    elif version == "v2":
        if real_page:
            r = await solve_recaptcha_v2_realpage(url, sitekey, pre, post,
                                                  timeout_s=deadline, proxy=proxy,
                                                  classifier=classifier)
        else:
            r = await solve_recaptcha_v2(sitekey, url, enterprise=enterprise,
                                         proxy=proxy, classifier=classifier)
    else:
        raise SolveError(ErrorCode.INVALID_REQUEST,
                         f"未知 version: {version}（v2|v3|invisible）", stage="validate")
    return _normalize(type_, r, time.monotonic() - _t0_of(body))


def _subprocess_env() -> dict:
    """子进程环境：PYTHONPATH 同时含 captchas/ 与仓库根（包内绝对 import 需要）。"""
    env = dict(os.environ)
    parts = [str(_CAPTCHAS_ROOT), str(_REPO_ROOT)]
    old = env.get("PYTHONPATH")
    if old:
        parts.append(old)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _t0_of(body: dict) -> float:
    return body.get("_t0") or time.monotonic()


def mark_start(body: dict) -> None:
    """在请求入口打时间戳，供 _normalize 计 elapsed。"""
    body["_t0"] = time.monotonic()
