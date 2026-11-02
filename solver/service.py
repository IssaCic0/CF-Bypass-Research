"""CF 过盾求解服务 —— 本仓库的最终方案实现（路线 B：Camoufox 求解 + HTTP 回放）。

为什么是这个方案：
  · 纯算/纯 JS 引擎路线已实测拿不到 cf_clearance（见 docs/ 报告 §14-16）
  · Camoufox 在 C++ 层篡改指纹，是唯一实测 2/2 通过的路线
  · 求解放进独立进程：崩溃隔离、可单独扩容、Go 网关只做 HTTP 客户端

服务契约（以 docs/API.md 为准，本 docstring 与之一致）：
  POST /solve   Authorization: Bearer <token>
                {"url": "https://target/", "timeout": 45, "force": false}
                → 200 {"ok":true, "solved":true, "cf_clearance":"...", "user_agent":"...",
                       "cookies":{...}, "cookie_list":[...], "expires_in":1500,
                       "expires_at":1761..., "elapsed":6.2, "cached":false,
                       "egress_id":"default"}
                → 200 {"ok":false, "solved":false, "error_code":"unsolved", "error":"...",
                       "stage":"wait", "hint":"..."}      # 求解失败 ≠ 请求失败
                → 4xx/5xx {"ok":false, "error_code":"...", "error":"...", "stage":"...", "hint":"..."}
  GET  /health  （公开，不需要 token）→ 200 就绪 / 503 未就绪（浏览器死了或正在恢复）
  GET  /stats   兼容旧客户端的统计别名（同时保留 /health 的字段）
  GET  /metrics Prometheus 文本（需 token）

设计要点（每条都对应一个曾被踩过的坑，见 docs/ADR 与 plans/）：
  · 浏览器实例**复用**（每次求解只开一个隔离 context），避免每次付 3.4s 启动
  · 同一「域名 + 出口标识」结果**缓存**；cf_clearance 天生绑 IP+UA，键里必须带出口标识，
    否则接了代理池会把 A 出口的凭证发给 B 出口（静默 403，最难排查）
  · 求解串行化（Camoufox sync API 绑定线程），准入靠**有界队列 + inflight 信号量**，
    满了立刻 429/503，绝不让请求在无界队列里等 300s 再超时
  · worker 线程**可自愈**：浏览器启动失败/中途断开 → 指数退避重拉，期间 /health 返 503
  · 全程不落盘、日志里不打 cookie 与 token 值（只打长度与指纹）

用法：
  pip install camoufox && python -m camoufox fetch
  python solver/service.py --port 8878          # 首次启动自动生成 token 写入 ./.env
  curl -H "Authorization: Bearer $(grep CF_SOLVE_TOKEN .env | cut -d= -f2)" \\
       -X POST localhost:8878/solve -d '{"url":"https://steamdb.info/"}'
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import ipaddress
import json
import os
import queue
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit

try:  # 作为包导入（tests / 未来 solver 包）
    from .probe import (  # 探测能力已拆到独立模块（v4.4，只搬不改）
        DEFAULT_PROBE_UA,
        PROBE_MAX_REDIRECTS,
        _PROBE_UA_BY_OS,
        probe_protection,
    )
    from .auth import (  # token 装载同理
        TOKEN_ENV,
        TOKEN_LINE_PREFIX,
        _read_token_file,
        load_or_create_token,
        token_fingerprint,
    )
    from .errors import ErrorCode, SolveError
    from .detect import summarize as detect_summarize
    from .cache import build_store
    from .proxypool import ProxyPool, parse_proxy_url
    from .backends import (
        MIN_CLEARANCE_REMAINING,
        BackendRouter,
        CamoufoxBackend,
        FlareSolverrBackend,
        derive_ttl,
    )
    from .prewarm import Prewarmer, parse_targets
    from .metrics import render_metrics_text  # v5.0 T-56：Prometheus 渲染已下沉（只搬不改）
    from .engines import Engine, build_factory as build_engine_factory, preflight as engine_preflight
    from .cfsolve_backend import CfsolveBackend
    from .pydoll_backend import PydollBackend, find_chrome
    from .route import (
        ACTION_REJECT as ROUTE_REJECT,
        ACTION_SKIP as ROUTE_SKIP,
        ROUTE_MODES,
        classify_failure as route_classify_failure,
        decide as route_decide,
        skip_backends as route_skip_backends,
    )
    from .challenges import (
        CACHEABLE_WANTS,
        COOKIE_FIELD,
        READ_TURNSTILE_TOKEN,
        TURNSTILE_SELECTOR,
        ENSURE_RECAPTCHA_JS,
        START_RECAPTCHA,
        READ_RECAPTCHA,
        NOMINAL_TTL,
        RESULT_FIELD,
        TOKEN_WANTS,
        Want,
        build_turnstile_stub,
    )
except ImportError:  # 直接以脚本运行：python solver/service.py
    from probe import (  # type: ignore[no-redef]
        DEFAULT_PROBE_UA,
        PROBE_MAX_REDIRECTS,
        _PROBE_UA_BY_OS,
        probe_protection,
    )
    from auth import (  # type: ignore[no-redef]
        TOKEN_ENV,
        TOKEN_LINE_PREFIX,
        _read_token_file,
        load_or_create_token,
        token_fingerprint,
    )
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]
    from detect import summarize as detect_summarize  # type: ignore[no-redef]
    from cache import build_store  # type: ignore[no-redef]
    from proxypool import ProxyPool, parse_proxy_url  # type: ignore[no-redef]
    from backends import (  # type: ignore[no-redef]
        MIN_CLEARANCE_REMAINING,
        BackendRouter,
        CamoufoxBackend,
        FlareSolverrBackend,
        derive_ttl,
    )
    from prewarm import Prewarmer, parse_targets  # type: ignore[no-redef]
    from metrics import render_metrics_text  # type: ignore[no-redef]
    from engines import Engine, build_factory as build_engine_factory, preflight as engine_preflight  # type: ignore[no-redef]
    from cfsolve_backend import CfsolveBackend  # type: ignore[no-redef]
    from pydoll_backend import PydollBackend, find_chrome  # type: ignore[no-redef]
    from route import (  # type: ignore[no-redef]
        ACTION_REJECT as ROUTE_REJECT,
        ACTION_SKIP as ROUTE_SKIP,
        ROUTE_MODES,
        classify_failure as route_classify_failure,
        decide as route_decide,
        skip_backends as route_skip_backends,
    )
    from challenges import (  # type: ignore[no-redef]
        CACHEABLE_WANTS,
        COOKIE_FIELD,
        READ_TURNSTILE_TOKEN,
        TURNSTILE_SELECTOR,
        ENSURE_RECAPTCHA_JS,
        START_RECAPTCHA,
        READ_RECAPTCHA,
        NOMINAL_TTL,
        RESULT_FIELD,
        TOKEN_WANTS,
        Want,
        build_turnstile_stub,
    )

# ── 常量 ───────────────────────────────────────────
DEFAULT_TTL = 1500          # cf_clearance 通常 30 分钟量级；cookie 带 expires 时以 cookie 为准
MIN_TTL = 60
MAX_URL_LEN = 2048
MAX_BODY_BYTES = 64 * 1024
#: 超大 body 的错误消息。`_read_body_raw` 返回它时，调用方必须转成 413 payload-too-large
#: （而不是和其它 body_error 一样落进 400 invalid-request）。
_BODY_TOO_LARGE_MSG = f"__payload_too_large__:{MAX_BODY_BYTES}"
DEFAULT_TIMEOUT = 45.0
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 120.0         # 上限：避免 timeout=1e9 把 worker 永久占住
SUBMIT_SLACK = 5.0          # worker 等待上限 = 预算 + 该余量
POLL_INTERVAL = 0.15        # 等 cookie 的轮询间隔（原 1.0s 平均白等 ~500ms）
GOTO_BUDGET_RATIO = 0.6     # 导航最多占预算的 60%
GOTO_BUDGET_CAP = 25.0
DEFAULT_MAX_QUEUE = 16
DEFAULT_MAX_INFLIGHT = 8
#: `/detect` 的并发上限（与求解 inflight **分开**计：探测不许挤占浏览器的额度）
DEFAULT_MAX_DETECT = 4
#: `/detect` 跟随重定向的跳数上限
DEFAULT_WARM_RETRY_MAX = 30.0
SHUTDOWN_GRACE = 10.0


#: DNS 正缓存条目上限（60s 短命缓存，超了直接清空——比引入 LRU 便宜且够用）
DNS_CACHE_MAX = 2048

#: 延迟直方图分桶（秒），用于 /metrics
LATENCY_BUCKETS = (1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)

#: `/detect` 结果的按域缓存 TTL（秒）。默认 5 分钟。
#: 为什么需要：`route=auto` 每次求解都会先探测一次（约 +0.5s 出站）。
#: 同一批任务往往反复打同一批域，缓存能把这 0.5s 摊掉；但探测结论会随 CF 改版变化，
#: 所以 TTL 必须短且可关（`--detect-cache-ttl 0` = 关闭）。
DETECT_CACHE_TTL = 300.0
#: 探测缓存的条目上限：域名是调用方可控输入，必须封顶。
DETECT_CACHE_MAX = 256

#: 审计日志（`--audit-log`）允许落盘的字段**白名单**。
#: 用白名单而不是黑名单：黑名单一旦漏了某个新字段，凭证就可能被写进审计文件。
#: 这里刻意**不含** clearance / token / cookies / cookie_list —— 审计只回答
#: "谁在什么时候打了哪个站、结果如何"，不回答"凭证是什么"。
AUDIT_FIELDS = (
    "event", "req_id", "host", "ok", "cached", "elapsed", "clearance_len", "ms",
    "error_code", "stage", "backend", "route", "reason", "peer", "ts",
)

#: `/metrics` 里按域指标只保留前 N 个域。
#: 域名是**调用方可控输入**，直接当标签用会让 Prometheus 的基数失控（几万个 series），
#: 这是"加了指标反而把监控系统打挂"的典型方式。
HOST_METRICS_TOP_N = 20

#: CGNAT 段（Python 的 `is_private` 不覆盖它，但它在实践中就是内网）
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _now() -> float:
    return time.time()


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


def _clamp_timeout(value) -> float:
    """把外部传入的 timeout 收敛到 [MIN_TIMEOUT, MAX_TIMEOUT]。"""
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise SolveError(ErrorCode.INVALID_REQUEST, "timeout 必须是数字", stage="validate")
    if t != t or t in (float("inf"), float("-inf")):  # NaN / inf
        raise SolveError(ErrorCode.INVALID_REQUEST, "timeout 必须是有限数字", stage="validate")
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, t))


def _default_browser_factory(*, headless: bool, os_persona: str = ""):
    """默认浏览器工厂：Camoufox。测试里注入替身即可离线跑。

    `os_persona`（如 `windows` / `macos` / `linux`）用于**固定**指纹 persona。
    为什么需要它：实测 Camoufox 0.5.8 **每次启动随机挑 OS**（同一个脚本三次分别得到
    `X11; Linux x86_64` / `Macintosh; Intel Mac OS X 10.15` / `Windows NT 10.0` 三种 UA）。
    随机 persona 会让 UA/时区/WebGL 与宿主平台、出口 IP 的地理互相矛盾——
    这正是 CF 判定"环境真实性"的输入之一。默认仍保持随机（不改变已实测通过的配置），
    生产环境建议显式固定，并用 A/B 实测确认收益（见 plans/ 的 T-14）。
    """
    try:
        from camoufox.sync_api import Camoufox
    except ImportError as e:
        raise SolveError(
            ErrorCode.NOT_CONFIGURED,
            f"未安装 camoufox：{e}",
            stage="admit",
            hint="pip install camoufox && python -m camoufox fetch",
        ) from e
    kwargs = {"headless": headless, "geoip": False}
    if os_persona:
        kwargs["os"] = [os_persona]
    return Camoufox(**kwargs)


def _same_document(candidate: str, target: str) -> bool:
    """判断两个 URL 是否指向"同一个文档"（忽略 fragment 与末尾斜杠差异）。"""
    def norm(u: str) -> str:
        base = u.split("#", 1)[0]
        return base.rstrip("/")
    return bool(candidate) and bool(target) and norm(candidate) == norm(target)


def _is_connected(browser) -> bool:
    """浏览器是否还活着。Playwright 的 Browser 有 is_connected()；替身没有就当作活着。"""
    checker = getattr(browser, "is_connected", None)
    if checker is None:
        return True
    try:
        return bool(checker())
    except Exception:
        return False


# ── token 装载 ─────────────────────────────────────
# ── 求解器 ─────────────────────────────────────────
class Solver:
    """单 worker 线程持有 Camoufox，串行求解 + 按「域名+出口标识」缓存。

    为什么必须单线程：Playwright/Camoufox 的 **sync API 绑定创建它的线程**，
    在 ThreadingHTTPServer 的其他线程里直接调用会报
    "cannot switch to a different thread"。所以浏览器只在 worker 线程里创建与使用，
    HTTP 请求通过**有界队列**派发过去；队列满即拒绝，不做无界缓冲。

    为什么 worker 要能自愈：浏览器可能启动即失败（camoufox 二进制没 fetch），
    也可能中途断开。原实现把 `Camoufox(...)` 放在 try 之外，线程一死服务就永久不可用，
    而 /health 还在报 ok。现在启动失败会记录原因并指数退避重拉，/health 同步反映。
    """

    def __init__(
        self,
        headless: bool = True,
        ttl: int = DEFAULT_TTL,
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        max_inflight: int = DEFAULT_MAX_INFLIGHT,
        max_detect: int = DEFAULT_MAX_DETECT,
        egress_id: str = "default",
        browser_factory=None,
        allow_private_hosts: bool = False,
        warm_retry_max: float = DEFAULT_WARM_RETRY_MAX,
        store=None,
        cache_dir: str | None = None,
        token: str = "",
        cache_max_files: int = 4096,
        proxy_pool: ProxyPool | None = None,
        browser_os: str = "",
        detect_cache_ttl: float = DETECT_CACHE_TTL,
    ) -> None:
        self.ttl = max(MIN_TTL, int(ttl))
        self.egress_id = egress_id or "default"
        self._headless = headless
        if browser_factory is None:
            persona = browser_os
            browser_factory = lambda *, headless: _default_browser_factory(  # noqa: E731
                headless=headless, os_persona=persona
            )
        self._browser_factory = browser_factory
        self.browser_os = browser_os
        self._allow_private_hosts = allow_private_hosts
        self._warm_retry_max = warm_retry_max
        self._pool = proxy_pool
        self.prewarmer = None            # 由 main 注入（配置了 --prewarm 才启动）
        #: 实际生效的后端链（由 main 在装配路由器后回填），只用于 /health 展示
        self.backend_chain: list[str] = ["camoufox"]

        self._started_at = _now()
        if store is not None:
            self._store = store
            self.cache_backend = "injected"
        else:
            self._store, self.cache_backend = build_store(cache_dir, token, max_files=cache_max_files)
        self._last_purge = _now()
        self._lock = threading.Lock()
        self._tasks: queue.Queue = queue.Queue(maxsize=max(1, max_queue))
        self._inflight = threading.BoundedSemaphore(max(1, max_inflight))
        # 探测走独立闸门：它不发浏览器，但会同步占用 handler 线程去做一次出站请求。
        # 与求解共用同一个信号量会让"探测"吃掉"求解"的额度（反之亦然）。
        self.detect_inflight = threading.BoundedSemaphore(max(1, max_detect))
        self._stopping = threading.Event()

        # 生命周期 / 健康
        self._browser_alive = False
        self._browser_ua = ""
        self._ever_alive = False
        self._restarts = 0
        self._last_error = ""
        self._last_error_at = 0.0
        self._last_success_at = 0.0

        # 计数
        self._solves = 0
        self._hits = 0
        self._failures = 0
        self._timeouts = 0
        self._rejected_busy = 0
        # 按后端的成功/失败计数（`cfsolve_backend_total{backend,result}`）。
        # 为什么单列：`solves_total` 原来的 HELP 写"含失败"但实现只数成功，
        # 而 router 路径连成功都不数 —— 两处口径都对不上，运维没法定位"哪个后端在退步"。
        self._backend_ok: dict[str, int] = {}
        self._backend_err: dict[str, int] = {}
        self._elapsed_sum = 0.0
        self._hist = [0] * len(LATENCY_BUCKETS)

        self._dns_cache: dict[str, tuple[float, str]] = {}
        # 探测结果按域短 TTL 缓存（见 DETECT_CACHE_TTL 的说明）
        self._detect_cache: dict[str, tuple[float, dict]] = {}
        # 允许 0 = 关闭（运维有时要每次都看实时探测结论）
        self.detect_cache_ttl = max(0.0, float(detect_cache_ttl))
        self.detect_cache_hits = 0

        self._thread = threading.Thread(target=self._worker, name="cfsolve-worker", daemon=True)
        self._thread.start()

    # -- worker：浏览器只活在这个线程里 ----------------
    def _worker(self) -> None:
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                cm = self._browser_factory(headless=self._headless)
                browser = cm.__enter__()
            except Exception as e:  # noqa: BLE001 —— 启动失败不能让线程静默退出
                self._mark_browser_dead("launch_failed", e)
                self._fail_all_queued(
                    SolveError(
                        ErrorCode.BROWSER_UNAVAILABLE,
                        f"浏览器启动失败：{type(e).__name__}: {str(e)[:160]}",
                        stage="admit",
                        hint="检查 camoufox 是否已安装并执行 python -m camoufox fetch；/health 会在恢复后自动转为 200",
                    )
                )
                if self._stopping.wait(backoff):
                    return
                backoff = min(backoff * 2, self._warm_retry_max)
                continue

            backoff = 1.0
            if not _is_connected(browser):
                # 启动"成功"但立刻不可用（例如替身/异常环境）：当作启动失败，继续退避重拉
                self._mark_browser_dead("launch_unusable", None)
                self._fail_all_queued(
                    SolveError(ErrorCode.BROWSER_UNAVAILABLE, "浏览器启动后不可用，正在重试", stage="admit")
                )
                if self._stopping.wait(backoff):
                    return
                backoff = min(backoff * 2, self._warm_retry_max)
                continue
            # 先读权威 UA，再宣布 alive：否则浏览器半死时 /health 会先说 ready 再超时（health 说谎）
            ua = self._read_browser_ua(browser)
            if not ua:
                self._note_error("ua_unreadable", "读不到 navigator.userAgent（不影响求解，但缓存共享无从判断）")
            with self._lock:
                self._browser_alive = True
                self._ever_alive = True
                self._last_error = "" if ua else self._last_error
                self._browser_ua = ua
            try:
                self._serve(cm, browser)
            finally:
                try:
                    cm.__exit__(None, None, None)
                except Exception:
                    pass
                with self._lock:
                    self._browser_alive = False
                    self._browser_ua = ""

    def _read_browser_ua(self, browser) -> str:
        """用一个临时 context 读浏览器真实 UA（约几十毫秒），失败返回空串。"""
        ctx = None
        try:
            ctx = browser.new_context()
            page = ctx.new_page()
            return page.evaluate("navigator.userAgent") or ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            if ctx is not None:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass

    def _serve(self, cm, browser) -> None:
        """不断从队列取任务执行；浏览器断开或收到退出信号则返回（交由外层重启）。"""
        while not self._stopping.is_set():
            try:
                task = self._tasks.get(timeout=0.5)
            except queue.Empty:
                if not _is_connected(browser):
                    self._mark_browser_dead("disconnected", None)
                    self._fail_all_queued(
                        SolveError(ErrorCode.BROWSER_UNAVAILABLE, "浏览器已断开，正在重启", stage="admit")
                    )
                    return
                self._maybe_purge()
                continue

            if task is None:      # 退出信号
                return

            fn, box = task
            if box["cancelled"].is_set():     # 客户端已放弃，别烧浏览器
                box["error"] = SolveError(ErrorCode.TIMEOUT, "请求已超时，任务被丢弃", stage="admit")
                box["done"].set()
                continue

            try:
                box["result"] = fn(browser)
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            finally:
                box["done"].set()

            if not _is_connected(browser):
                self._mark_browser_dead("disconnected", None)
                return

    def _note_error(self, reason: str, detail: str) -> None:
        """记录一条不致命但需要被看见的问题（会出现在 /health.last_error 里）。"""
        with self._lock:
            self._last_error = f"{reason}: {detail}"
            self._last_error_at = _now()

    def _mark_browser_dead(self, reason: str, err: Exception | None) -> None:
        with self._lock:
            was_alive = self._browser_alive
            self._browser_alive = False
            self._browser_ua = ""
            if was_alive or reason == "launch_failed":
                self._restarts += 1
            self._last_error = f"{reason}: {type(err).__name__}: {str(err)[:160]}" if err else reason
            self._last_error_at = _now()

    def _fail_all_queued(self, err: SolveError) -> None:
        """浏览器不可用时，队列里的任务必须立刻失败，而不是等满 300s。"""
        while True:
            try:
                task = self._tasks.get_nowait()
            except queue.Empty:
                return
            if task is None:
                self._tasks.put(None)
                return
            _, box = task
            box["error"] = err
            box["done"].set()

    def _submit(self, fn, budget: float):
        """把浏览器任务派发到 worker 线程并等待结果；队列满则立刻拒绝。"""
        box = {
            "done": threading.Event(),
            "cancelled": threading.Event(),
            "result": None,
            "error": None,
        }
        try:
            self._tasks.put_nowait((fn, box))
        except queue.Full:
            with self._lock:
                self._rejected_busy += 1
            raise SolveError(
                ErrorCode.BUSY,
                "求解队列已满",
                stage="admit",
                hint="降低并发、提高 --max-queue，或多起几个 solver 进程（每进程一个 Camoufox）",
            )

        wait_for = budget + SUBMIT_SLACK
        if not box["done"].wait(timeout=wait_for):
            box["cancelled"].set()        # 让 worker 跳过该任务，别烧浏览器
            with self._lock:
                self._timeouts += 1
            raise SolveError(
                ErrorCode.TIMEOUT,
                f"{wait_for:.0f}s 内未得到求解结果",
                stage="wait",
                hint="目标站可能更慢：提高 --max-queue 并降低并发，或调大请求里的 timeout（上限 120s）",
            )
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    # -- 单个请求的防护 ---------------------------------
    def _classify_host(self, host: str) -> str:
        """把主机分成三态：`blocked` / `unresolvable` / `allowed`。

        为什么必须区分后两者（这是一个真实回归的教训）：
        真实页面大量引用在**本网络不可解析**的域名（广告/统计/字体 CDN，NXDOMAIN 或被
        sinkhole 到 `0.0.0.0`）。把它们当作"越权目标"会让整次求解被判 400 —— 一个凭证都拿不到。
        而"解析不出来"对 SSRF 毫无意义：它连不到内网任何东西。
        """
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return "unresolvable"
        verdict = "allowed"
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            # 0.0.0.0 / :: 必须先于 is_private 判断（Python 里它也满足 is_private）。
            # 归为 blocked 而不是放行：在 Linux 上 `connect(0.0.0.0)` 等价于连 **localhost**，
            # 放行就等于给攻击者一个"域名解析到 0.0.0.0 → 打本机端口"的 SSRF 通道。
            # 代价：用 pihole 之类把广告域名 sinkhole 到 0.0.0.0 的环境会看到拦截，
            # 这类部署请让求解机的 DNS 返回 NXDOMAIN，或显式 --allow-private-hosts。
            if ip.is_unspecified:
                return "blocked"
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                return "blocked"
            if ip.version == 4 and ip in _CGNAT:      # 100.64.0.0/10：运营商级 NAT，常被当作内网
                return "blocked"
        return verdict

    def _check_target(self, host: str) -> None:
        """入口严格校验：调用方给的 URL 一旦解析到受限地址或根本解析不了，直接 400。

        求解服务会用自己的浏览器去访问调用方给的 URL，是天然的 SSRF 跳板。
        本地联调/测试可显式 --allow-private-hosts。
        """
        if self._allow_private_hosts:
            return
        cached = self._dns_cache.get(host)
        if cached and cached[0] > _now():
            return
        verdict = self._classify_host(host)
        if verdict == "blocked":
            raise SolveError(
                ErrorCode.INVALID_REQUEST,
                f"目标解析到受限地址（{host}），已拒绝",
                stage="validate",
                hint="求解服务不允许访问私网/回环地址；确需联调请加 --allow-private-hosts",
            )
        if verdict == "unresolvable":
            # 实测教训：真实站点的 DNS 会**瞬时**失败（g2.com 连续两次 invalid-request 后第三次成功）。
            # 把它报成 400 "参数错误" 会让调用方以为 URL 写错了、直接放弃且不重试。
            # 语义上这属于"到不了"，是传输层问题 → 502 + 可重试。
            raise SolveError(
                ErrorCode.TRANSPORT, f"域名解析失败：{host}", stage="validate",
                hint="DNS 解析失败可能是瞬时的，可重试；持续失败请检查求解机的 DNS 与网络",
            )
        if len(self._dns_cache) >= DNS_CACHE_MAX:
            # 条目太多（长期运行 + 抓到大量域）时清空重来：正缓存只有 60s 寿命，清空代价可忽略。
            # 不这么做就是一个只增不减的 dict（原实现如此）。
            self._dns_cache.clear()
        self._dns_cache[host] = (_now() + 60.0, host)

    def probe_user_agent(self) -> str:
        """探测用 UA：优先用浏览器**真实** UA（与求解同身份），浏览器还没起来时按 persona 推。"""
        with self._lock:
            ua = self._browser_ua
        if ua:
            return ua
        return _PROBE_UA_BY_OS.get(self.browser_os, DEFAULT_PROBE_UA)

    def guard_hop(self, url: str) -> None:
        """重定向跳转的 SSRF 守卫（守卫每一跳，不只是入口 URL）。

        与 `_check_target` 的差别（**故意不同**，两边语义各有出处）：
          · 入口 URL 用**严格**判定：解析不了 = 调用方写错 → 报错；
          · 重定向目标用**三态**：`blocked` 才拒绝，`unresolvable` 放行
            （目标站在重定向里带一条解析不了的统计域，不该让整次探测失败）。
        这与 `/solve` 的 route 守卫是同一套语义（见 `_install_request_guard`）。

        非 http(s) 的跳转（`file:` / `gopher:` / `ftp:` …）一律拒绝：opener 里装着其它
        协议处理器，放行等于把协议面交出去。
        """
        if self._allow_private_hosts:
            return
        try:
            parts = urlsplit(url)
        except Exception as e:  # noqa: BLE001
            raise SolveError(
                ErrorCode.INVALID_REQUEST, f"重定向目标无法解析：{str(e)[:100]}", stage="validate"
            ) from e
        if (parts.scheme or "").lower() not in ("http", "https"):
            raise SolveError(
                ErrorCode.INVALID_REQUEST,
                f"重定向到非 http(s) 协议（{parts.scheme or '空'}），已拒绝",
                stage="validate",
                hint="探测只跟随 http/https 跳转",
            )
        host = (parts.hostname or "").lower()
        if not host:
            raise SolveError(
                ErrorCode.INVALID_REQUEST, "重定向目标缺少主机名，已拒绝", stage="validate"
            )
        if self._classify_host(host) == "blocked":
            raise SolveError(
                ErrorCode.INVALID_REQUEST,
                f"重定向目标解析到受限地址（{host}），已拒绝",
                stage="validate",
                hint="求解服务只允许探测公网目标；确需联调请加 --allow-private-hosts",
            )

    def _entry_for(self, host: str):
        """该域粘住的代理出口；没有代理池时返回 None。"""
        return self._pool.for_host(host) if self._pool is not None else None

    # -- 供 BackendRouter 使用的治理接口 ----------------
    #
    # 为什么需要这一组：治理（缓存/准入/计数/按域统计）原来只写在 `solve()` 里，
    # 而 `BackendRouter` 的其它后端（cfsolve / pydoll / flaresolverr）根本不走 `solve()`，
    # 于是它们既不进缓存、也不受 inflight 约束、还不进任何指标 —— 一个"热路径命中率
    # 归零 + 能被打穿 + 指标说一切正常"的组合。治理必须绑在**入口/路由**，不能绑在某个实现上。

    # -- 跨进程去重（同一域同时被多个 worker 抢到）-------
    def claim_solve(self, url: str, budget: float) -> bool:
        """URL 级的跨进程抢占入口（供 Solver.solve 与 BackendRouter 共用）。"""
        host = _host_of(url)
        if not host:
            return True
        entry = self._entry_for(host) if self._pool is not None else None
        return self._claim(self.cache_key(host, entry), budget)

    def wait_for_peer(self, url: str, budget: float) -> dict | None:
        """URL 级的"等对端填缓存"入口（同一条链路共用，避免两份实现漂移）。"""
        host = _host_of(url)
        if not host:
            return None
        entry = self._entry_for(host) if self._pool is not None else None
        return self._wait_for_peer(self.cache_key(host, entry), budget)

    def release_claim_for(self, url: str) -> None:
        host = _host_of(url)
        if not host:
            return
        entry = self._entry_for(host) if self._pool is not None else None
        self._release_claim(self.cache_key(host, entry))

    def _claim(self, key: str, budget: float) -> bool:
        """抢占"我来解这个域"。没有抢占能力的存储（内存/文件）直接视为抢到。

        内存存储本来就只服务单进程；文件存储在 `solver/cache.py` 里也明说不做跨进程互斥。
        所以只有 SQLite（多进程共享）才需要/才有这一步。
        """
        fn = getattr(self._store, "try_claim", None)
        if not callable(fn):
            return True
        try:
            return bool(fn(key, ttl=max(5.0, min(120.0, float(budget)))))
        except Exception:  # noqa: BLE001 —— 抢占出错就退化为不抢占，绝不阻断求解
            return True

    def _release_claim(self, key: str) -> None:
        fn = getattr(self._store, "release_claim", None)
        if callable(fn):
            try:
                fn(key)
            except Exception:  # noqa: BLE001
                pass

    def _wait_for_peer(self, key: str, budget: float) -> dict | None:
        """没抢到时等对端把缓存填好（最多 ~10s 或预算的一半）。

        返回 None 表示"对端没能在预期时间内填好"——此时我们仍然自己去解：
        宁可多解一次，也不能让请求永久挂住（对端可能已经崩了，claim 在 TTL 后自然过期）。
        """
        deadline = time.monotonic() + max(1.0, min(10.0, float(budget) / 2))
        while time.monotonic() < deadline:
            try:
                cached = self._store.get(key)
            except Exception:  # noqa: BLE001
                cached = None
            if cached and float(cached.get("expires_at", 0) or 0) > _now():
                with self._lock:
                    self._hits += 1
                hit = dict(cached)
                hit.update({"ok": True, "solved": True, "cached": True, "elapsed": 0.0})
                return hit
            time.sleep(0.1)
        return None

    def cache_lookup(self, url: str) -> dict | None:
        """按「域名+出口」查缓存，命中则返回可直接对外返回的 dict（含 cached=True）。"""
        host = _host_of(url)
        if not host:
            return None
        entry = self._entry_for(host) if self._pool is not None else None
        if self._pool is not None and entry is None:
            return None                      # 所有出口都在冷却：当作未命中，交给上层报 busy
        try:
            cached = self._store.get(self.cache_key(host, entry))
        except Exception:  # noqa: BLE001 —— 缓存坏了不该打断求解
            return None
        if not cached or float(cached.get("expires_at", 0) or 0) <= _now():
            return None
        with self._lock:
            self._hits += 1
        hit = dict(cached)
        hit.update({"ok": True, "solved": True, "cached": True, "elapsed": 0.0})
        return hit

    def remember(self, url: str, result: dict) -> None:
        """把后端产出的凭证写进缓存（只在 TTL 有效时写；TTL=0 写进去等于每次重解）。"""
        host = _host_of(url)
        if not host or not isinstance(result, dict):
            return
        if float(result.get("expires_at", 0) or 0) <= _now():
            return
        entry = self._entry_for(host) if self._pool is not None else None
        try:
            self._store.set(self.cache_key(host, entry), result, host=host,
                            backend=str(result.get("backend") or ""))
        except Exception:  # noqa: BLE001
            pass

    def begin_backend_call(self) -> bool:
        """路由侧后端调用的准入门票；拿不到就立刻 429，不做无界排队。"""
        return self._inflight.acquire(blocking=False)

    def end_backend_call(self, ok: bool, elapsed: float, host: str = "", backend: str = "") -> None:
        """归还准入门票 + 记账（次数、耗时分布、按后端、按域）。"""
        try:
            self._inflight.release()
        except ValueError:               # 释放次数多于获取（调用方写错）：不放大成 500
            pass
        self._account(ok, elapsed, host=host, backend=backend)

    def _account(self, ok: bool, elapsed: float, host: str = "", backend: str = "") -> None:
        """统一记账入口：**所有路径都必须走这里**，否则指标就会"只覆盖一半路径"。

        v4.1 之前只有 `Solver.solve`（camoufox）在记账，router 里的其它后端一条都没记；
        修完 router 才发现主路径（纯 camoufox 部署）反而没有 backend/host 维度——
        同一个坑踩了两次，所以这里做成**唯一入口**，且由主路径与路由路径共用。
        """
        with self._lock:
            self._solves += 1            # 统一口径：真实求解次数（含失败）
            self._elapsed_sum += elapsed
            if not ok:
                self._failures += 1
                if backend:
                    self._backend_err[backend] = self._backend_err.get(backend, 0) + 1
            elif backend:
                self._backend_ok[backend] = self._backend_ok.get(backend, 0) + 1
            for i, edge in enumerate(LATENCY_BUCKETS):
                if elapsed <= edge:
                    self._hist[i] += 1
                    break
        # 按域统计（只放计数，不放凭证）；SqliteStore 才有，其它实现静默跳过
        if host:
            record = getattr(self._store, "record_solve", None)
            if callable(record):
                try:
                    record(host, ok)
                except Exception:  # noqa: BLE001
                    pass

    def detect_cache_get(self, url: str) -> dict | None:
        """取探测缓存（未命中/已过期返回 None）。"""
        if self.detect_cache_ttl <= 0:
            return None
        host = _host_of(url)
        if not host:
            return None
        with self._lock:
            entry = self._detect_cache.get(host)
            if entry is None:
                return None
            expire_at, value = entry
            if expire_at <= _now():
                self._detect_cache.pop(host, None)
                return None
            self.detect_cache_hits += 1
        cached = dict(value)
        cached["cached"] = True
        return cached

    def detect_cache_put(self, url: str, result):  # noqa: ANN001
        """写探测缓存（超过上限按**最早过期**淘汰，与缓存层的语义保持一致）。"""
        if self.detect_cache_ttl <= 0 or not isinstance(result, dict):
            return
        host = _host_of(url)
        if not host:
            return
        with self._lock:
            if len(self._detect_cache) >= DETECT_CACHE_MAX and host not in self._detect_cache:
                oldest = min(self._detect_cache.items(), key=lambda kv: kv[1][0])[0]
                self._detect_cache.pop(oldest, None)
            self._detect_cache[host] = (_now() + self.detect_cache_ttl, dict(result))

    def cache_remaining(self, url: str) -> float:
        """这条凭证还能用多久（秒）；没有缓存或出口都在冷却时返回 -1。

        保活线程用它判断"要不要现在续期"，避免做无意义求解。
        """
        host = _host_of(url)
        if not host:
            return -1.0
        entry = self._entry_for(host)
        if self._pool is not None and entry is None:
            return -1.0
        cached = self._store.get(self.cache_key(host, entry))
        if not cached:
            return -1.0
        return max(0.0, float(cached.get("expires_at", 0)) - _now())

    # -- 缓存键 ---------------------------------------
    def cache_key(self, host: str, entry=None) -> str:
        """缓存键 = 域名 + 出口标识。

        **UA 不进键**（这是经过一轮审查后改的，理由要说清）：
          · 缓存里存的是**成对**的「cookie + 当时的 UA」，回放时客户端用的就是这对值
            （`DoWith` 强制 req 的 UA 等于 `cl.UserAgent`）。所以旧 UA 的凭证配旧 UA 依然自洽，
            不需要也不应该因为"当前浏览器 UA 变了"就丢弃它。
          · 反过来，把 UA 放进键会让「跨进程共享」这个主要收益直接归零：
            各进程的 Camoufox 指纹可能不同，键一变就谁也读不到谁的条目，退化成全部重复求解。
          · UA 若真变了、且站点也校验，客户端回放会拿到 403 —— 由既有自愈逻辑重解，不会静默。
        UA 只用于观测（`/health.ua_fingerprint`），让运维一眼看出两个进程能否共享缓存。
        """
        egress = entry.egress_id if entry is not None else self.egress_id
        raw = f"{host}|{egress}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:32]

    # -- 单次求解（在 worker 线程里执行）--------------
    def _do_solve(
        self,
        browser,
        url: str,
        budget: float,
        proxy: dict | None = None,
        *,
        want: "Want" = Want.CLEARANCE,
        sitekey: str = "",
        action: str = "",
        cdata: str = "",
    ) -> dict:
        t_end = time.monotonic() + budget
        ctx = browser.new_context(proxy=proxy)
        # Turnstile 用"在目标 origin 下渲染官方 widget"的方式拿 token：
        # 拦截目标 URL 直接返回存根页（token 绑 origin，换个域拿到也没用）。
        stub_html = build_turnstile_stub(sitekey, action, cdata) if want is Want.TURNSTILE else ""
        # 每条真实发出的请求都要过守卫：只校验首个 URL 是可以被 302 / 子资源绕过的
        violation: dict = {}
        self._install_request_guard(ctx, violation, stub_url=url if stub_html else "", stub_html=stub_html)
        goto_error = None
        try:
            page = ctx.new_page()
            goto_budget = max(1.0, min(budget * GOTO_BUDGET_RATIO, GOTO_BUDGET_CAP))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(goto_budget * 1000))
            except Exception as e:  # noqa: BLE001
                # 导航超时/被中断时 cookie 仍可能已下发，继续轮询而不是直接失败；
                # 但如果最终一个 cookie 都没有，这个异常就是"网络层失败"的证据。
                goto_error = f"{type(e).__name__}: {str(e)[:120]}"

            cookies: dict[str, str] = {}
            cookie_list: list[dict] = []
            token = ""
            token_field = ""

            if want in TOKEN_WANTS:
                token, token_field = self._collect_token(
                    page, want, sitekey=sitekey, action=action, deadline=t_end
                )
            else:
                cookie_name = COOKIE_FIELD[want]
                while True:
                    if violation.get("top_level"):
                        break
                    cookies, cookie_list = self._read_cookies(ctx)
                    if cookie_name in cookies:
                        break
                    if time.monotonic() >= t_end:
                        break
                    time.sleep(POLL_INTERVAL)
                token = cookies.get(cookie_name, "")

            if violation and violation.get("top_level"):
                raise SolveError(
                    ErrorCode.INVALID_REQUEST,
                    f"页面（或其一跳重定向）指向受限地址（{violation.get('host')}），已拦截",
                    stage="validate",
                    hint="求解服务只允许访问公网目标",
                )
            if violation:
                # 子资源被拦：不影响本次求解，但必须留痕（可能说明 DNS 环境在 sinkhole）
                self._note_error(
                    "subresource_blocked",
                    f"已拦截子资源请求 {violation.get('host')}（不影响本次求解）",
                )

            try:
                ua = page.evaluate("navigator.userAgent")
            except Exception:
                ua = ""
            return {
                "cookies": cookies,
                "cookie_list": cookie_list,
                "ua": ua,
                "token": token,
                "token_field": token_field,
                "goto_error": goto_error,
                "budget_left": max(0.0, t_end - time.monotonic()),
            }
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    def _collect_token(self, page, want, *, sitekey: str, action: str, deadline: float) -> tuple[str, str]:
        """一次性 token 的收集器（Turnstile / reCAPTCHA v3）。

        两者机制不同但同构：
          · Turnstile：widget 自己算完会把 token 写进隐藏 input，轮询即可；
          · reCAPTCHA v3：需要主动调 `grecaptcha.execute`，拿它的返回值（也兼容 DOM 回填）。
        都不缓存 —— 一次性 token 复用必然失败。
        """
        if not sitekey:
            raise SolveError(
                ErrorCode.INVALID_REQUEST, f"{want.value} 需要 sitekey", stage="validate",
                hint="从页面里的 data-sitekey / render= 参数取值",
            )

        if want is Want.TURNSTILE:
            widget_error = ""
            while time.monotonic() < deadline:
                try:
                    value = page.evaluate(READ_TURNSTILE_TOKEN) or ""
                except Exception:  # noqa: BLE001 —— 页面/全局还没就绪是正常过程
                    value = ""
                if value.startswith("ERR:"):
                    widget_error = value[4:]
                elif value:
                    return value, "cf-turnstile-response"
                time.sleep(POLL_INTERVAL)
            if widget_error:
                raise SolveError(
                    ErrorCode.UNSOLVED,
                    f"Turnstile widget 渲染失败：{widget_error[:160]}",
                    stage="wait",
                    hint="确认 sitekey 与目标域匹配；测试可用官方测试 sitekey 1x00000000000000000000AA",
                )
            return "", "cf-turnstile-response"

        # reCAPTCHA v3：确保脚本 → 等 grecaptcha 就绪 → **点火** execute → 轮询全局变量。
        try:
            page.evaluate(ENSURE_RECAPTCHA_JS, sitekey)
        except Exception:  # noqa: BLE001
            pass
        waiter = getattr(page, "wait_for_function", None)
        if waiter is not None:
            try:
                waiter(
                    "() => typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function'",
                    timeout=min(15000, max(1000, int((deadline - time.monotonic()) * 1000 * 0.4))),
                )
            except Exception:  # noqa: BLE001 —— 超时也继续，下面还会重试点火
                pass
        fired = False
        widget_error = ""
        while time.monotonic() < deadline:
            if not fired:
                try:
                    page.evaluate(START_RECAPTCHA, {"key": sitekey, "action": action})
                    fired = True
                except Exception:  # noqa: BLE001
                    pass
            try:
                value = page.evaluate(READ_RECAPTCHA) or ""
            except Exception:  # noqa: BLE001
                value = ""
            if value.startswith("ERR:"):
                widget_error = value[4:]
            elif value:
                return value, "g-recaptcha-response"
            time.sleep(POLL_INTERVAL)
        if widget_error:
            raise SolveError(
                ErrorCode.UNSOLVED, f"reCAPTCHA 执行失败：{widget_error[:160]}", stage="wait",
                hint="确认 sitekey 与页面匹配；v3 需要站点已启用该 action",
            )
        return "", "g-recaptcha-response"

    def _install_request_guard(self, ctx, violation: dict, *, stub_url: str = "", stub_html: str = "") -> None:
        """在 context 上挂请求拦截：每个真实请求的 host 都复核 SSRF 守卫。

        为什么必须这么做：`_check_target` 只看调用方给的那一个 URL，
        而浏览器会跟随 302、加载子资源、被 JS 跳转——攻击者只需一次重定向就能把
        内部地址塞进来（这正是原实现"只校验首跳 + 回显页面标题"的老洞）。

        三态 + 两层语义：
          · `blocked` + **顶层导航**（含重定向）→ abort，并把整次求解判 `invalid-request`（这是真攻击）；
          · `blocked` + **子资源/iframe** → 只 abort 该请求 + 留痕，**继续求解**。
            目标域的凭证与被拦的子资源无关；反过来把子资源也升级成整次失败，
            会让"把广告域名 sinkhole 到 0.0.0.0/私网"的环境（pihole/AdGuard/企业 DNS）
            对任何页面都返回 400 —— 用安全换来了不可用，没有意义；
          · `unresolvable`（NXDOMAIN）→ 放行，让浏览器自然失败。
        解析结果只做**本次求解作用域内的正缓存**（负结果不缓存，否则会放大 DNS rebinding）。

        安装失败时**fail closed**：安全控制不能静默失效。
        """
        # 注意：即使 --allow-private-hosts 关掉了 SSRF 校验，**存根页拦截也必须装上**，
        # 否则 Turnstile 永远拿不到 token（token 绑 origin，必须由本地页面在目标 origin 下渲染）。
        guard_enabled = not self._allow_private_hosts
        allow_cache: dict[str, bool] = {}

        def check(url: str, *, top_level: bool) -> bool:
            """返回 True=允许，False=拦截。"""
            if not guard_enabled:
                return True
            try:
                host = urlsplit(url).hostname or ""
            except Exception:  # noqa: BLE001
                return True
            if not host:
                return True
            if allow_cache.get(host):
                return True
            verdict = self._classify_host(host)
            if verdict == "blocked":
                violation["host"] = host
                if top_level:
                    violation["top_level"] = True
                return False
            if verdict == "allowed":
                allow_cache[host] = True
            return True  # unresolvable 也放行

        def is_top_level(req) -> bool:
            marker = getattr(req, "is_navigation_request", None)
            try:
                if marker is None or not marker():
                    return False
            except Exception:  # noqa: BLE001
                return False
            frame = getattr(req, "frame", None)
            if frame is None:
                return True
            try:
                return getattr(frame, "parent_frame", None) is None
            except Exception:  # noqa: BLE001
                return False

        def handler(route):
            req = route.request
            target = req.url or ""
            if stub_html and _same_document(target, stub_url):
                # 目标文档直接返回本地存根页：Turnstile token 绑 origin，
                # 只有在目标 origins 下渲染出来的 token 才可用。
                try:
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=stub_html)
                    return
                except Exception:  # noqa: BLE001
                    pass
            if not check(target, top_level=is_top_level(req)):
                try:
                    route.abort()
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                route.continue_()
            except Exception:  # noqa: BLE001
                pass

        try:
            ctx.route("**/*", handler)
        except Exception as e:  # noqa: BLE001
            if guard_enabled or stub_html:
                raise SolveError(
                    ErrorCode.BROWSER_UNAVAILABLE,
                    f"请求拦截安装失败（{type(e).__name__}: {str(e)[:100]}），已拒绝求解",
                    stage="admit",
                    hint="安全控制不允许静默失效：升级 playwright，或明确用 --allow-private-hosts 关闭防护",
                ) from e
            return

        if not guard_enabled:
            return

        # WebSocket 握手不在 route("**/*") 的覆盖范围内（Playwright 有独立 API）。
        route_ws = getattr(ctx, "route_web_socket", None)
        if route_ws is None:
            # 旧版 Playwright 没有这个 API —— 这是最现实的场景，必须留痕而不是静默
            self._note_error("ws_guard_unavailable", "当前 playwright 不支持 route_web_socket，ws 目标未被拦截")
            return

        def ws_handler(ws):
            if check(getattr(ws, "url", "") or "", top_level=False):
                connect = getattr(ws, "connect_to_server", None)
                if connect is not None:
                    try:
                        connect()
                    except Exception:  # noqa: BLE001
                        pass
                return
            for closer in ("close", "abort"):
                fn = getattr(ws, closer, None)
                if fn is not None:
                    try:
                        fn()
                    except Exception:  # noqa: BLE001
                        pass
                    break

        try:
            route_ws("**/*", ws_handler)
        except Exception as e:  # noqa: BLE001
            self._note_error("ws_guard_unavailable", f"ws 拦截安装失败：{type(e).__name__}")

    @staticmethod
    def _proxy_dict(entry) -> dict | None:
        """代理池条目 → Playwright 的 proxy 参数（含凭据，仅内存中使用，绝不进日志）。"""
        if entry is None:
            return None
        return parse_proxy_url(entry.raw_url)

    @staticmethod
    def _read_cookies(ctx) -> tuple[dict[str, str], list[dict]]:
        raw = ctx.cookies()
        cookies: dict[str, str] = {}
        cookie_list: list[dict] = []
        for c in raw:
            name = c.get("name", "")
            cookies[name] = c.get("value", "")
            cookie_list.append(
                {
                    "name": name,
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expires", -1),
                    "http_only": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "same_site": c.get("sameSite", ""),
                }
            )
        return cookies, cookie_list

    # -- 对外入口 -----------------------------------
    def solve(
        self,
        url: str,
        timeout: float = DEFAULT_TIMEOUT,
        force: bool = False,
        *,
        want: str = "clearance",
        sitekey: str = "",
        action: str = "",
        cdata: str = "",
    ) -> dict:
        try:
            want_enum = Want.parse(want)
        except ValueError as e:
            raise SolveError(ErrorCode.INVALID_REQUEST, str(e), stage="validate") from e
        if self._stopping.is_set():
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE, "服务正在关闭，不再受理新请求", stage="admit",
                hint="这是正常退出流程；请把请求打到其它实例",
            )
        if not isinstance(url, str) or len(url) > MAX_URL_LEN:
            raise SolveError(ErrorCode.INVALID_REQUEST, "url 缺失或过长", stage="validate")
        if not url.startswith(("http://", "https://")):
            raise SolveError(
                ErrorCode.INVALID_REQUEST, "url 必须是 http(s)", stage="validate",
                hint="需要带 scheme 的绝对地址，例如 https://example.com/",
            )
        if want_enum in TOKEN_WANTS and not sitekey:
            raise SolveError(
                ErrorCode.INVALID_REQUEST, f"want={want_enum.value} 需要 sitekey", stage="validate",
                hint="从页面源码里取 data-sitekey，或 reCAPTCHA 的 api.js?render=<sitekey>",
            )
        host = _host_of(url)
        if not host:
            raise SolveError(ErrorCode.INVALID_REQUEST, f"无法解析域名: {url}", stage="validate")
        budget = _clamp_timeout(timeout)
        self._check_target(host)

        # 先定出口（粘性），缓存键随出口变化
        proxy_entry = self._entry_for(host)
        if self._pool is not None and proxy_entry is None:
            raise SolveError(
                ErrorCode.BUSY,
                "所有代理出口都在冷却中",
                stage="admit",
                hint="等冷却结束，或加更多出口；/health 的 proxies 字段能看到各自冷却剩余时间",
            )

        key = self.cache_key(host, proxy_entry)
        # 一次性 token 绝不进缓存（复用必然失败）；cookie 类才按域复用
        cacheable = want_enum in CACHEABLE_WANTS
        if not force and cacheable:
            cached = self._store.get(key)
            if cached and cached.get("expires_at", 0) > _now():
                with self._lock:
                    self._hits += 1
                hit = dict(cached)
                hit.update({"ok": True, "solved": True, "cached": True, "elapsed": 0.0})
                return hit

        # 跨进程抢占：多个 worker **同时**收到同一个新域时，只让一个真的去解，
        # 其余去等缓存（实测不加这一步时 4 进程 burst 会各解一次 = 12/12）。
        claimed = False
        if cacheable and not force:
            claimed = self._claim(key, budget)
            if not claimed:
                peer = self._wait_for_peer(key, budget)
                if peer is not None:
                    return peer

        # 先把代理字典算出来**再**拿信号量：解析失败时不能留下未释放的额度。
        # 同时保证错误消息里不出现凭据（parse_proxy_url 的原始报错会带 user:pass）。
        try:
            proxy = self._proxy_dict(proxy_entry)
        except ValueError:
            if claimed:
                self._release_claim(key)      # 别把抢占带进异常（对端会白等 TTL）
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE,
                "代理配置无法解析（凭据已隐去）",
                stage="admit",
                hint="检查 --proxy 的格式：scheme://user:pass@host:port",
            )

        # 准入：inflight 上限，满了立刻拒绝（不等）
        if not self._inflight.acquire(blocking=False):
            if claimed:
                self._release_claim(key)      # 准入被拒也要放行，否则对端要白等
            with self._lock:
                self._rejected_busy += 1
            raise SolveError(
                ErrorCode.BUSY,
                "并发求解数已达上限",
                stage="admit",
                hint="降低并发或提高 --max-inflight；注意单进程只串行求解，调大只增加排队深度",
            )

        t0 = time.monotonic()
        try:
            r = self._submit(
                lambda b: self._do_solve(
                    b, url, budget, proxy,
                    want=want_enum, sitekey=sitekey, action=action, cdata=cdata,
                ),
                budget,
            )
        except SolveError as e:
            # 失败也要放掉抢占：否则别的进程会白等一个"永远不会来"的缓存（最多等满 claim TTL）
            if claimed:
                self._release_claim(key)
            # 失败也要记账：HELP 声明"含失败"，而旧实现只在成功路径 +1（指标说谎）。
            # 参数错误/队列满这类**没真正发起求解**的终止性错误不该算一次求解。
            if e.code not in (ErrorCode.INVALID_REQUEST, ErrorCode.BUSY, ErrorCode.UNAUTHORIZED):
                self._account(False, round(time.monotonic() - t0, 2), host=host, backend="camoufox")
            # 只有"可能是出口的锅"的失败才记在代理头上；
            # 参数错误/队列满/自身在关闭，与出口无关，记进去会误伤（把好代理冷却掉）。
            if (
                self._pool is not None
                and proxy_entry is not None
                and e.code in (ErrorCode.UNSOLVED, ErrorCode.BROWSER_UNAVAILABLE, ErrorCode.TIMEOUT, ErrorCode.TRANSPORT)
            ):
                self._pool.record_failure(proxy_entry, e.code.value)
            raise
        finally:
            self._inflight.release()

        elapsed = round(time.monotonic() - t0, 2)
        # 注意：这里**不能**先记一次成功。下面 `if not token` / `ttl<=0` 还会各记一次失败，
        # 早先那版把成功记账写成无条件执行 → 一次失败会被记成「1 次成功 + 1 次失败」，
        # 指标上表现为"失败请求还有 50% 成功率"（独立审查 Q4 抓到的 P1）。
        cookies = r["cookies"]
        ua = r.get("ua") or self._browser_ua
        token = r.get("token") or ""
        token_field = r.get("token_field") or ""

        if not token:
            if claimed:
                self._release_claim(key)
            self._account(False, elapsed, host=host, backend="camoufox")
            if self._pool is not None and proxy_entry is not None:
                self._pool.record_failure(proxy_entry, "unsolved")
            # 一个 cookie 都没拿到 + 导航报错 ⇒ 网络/传输层失败，不是"被盾拒"
            if not cookies and r.get("goto_error"):
                raise SolveError(
                    ErrorCode.TRANSPORT,
                    f"导航失败：{r['goto_error']}",
                    stage="navigate",
                    hint="检查 DNS/出口连通性；若开了代理，确认代理可用且没被目标封",
                )
            wanted = COOKIE_FIELD.get(want_enum, want_enum.value)
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"{budget:.0f}s 内未取得 {wanted}",
                stage="wait",
                hint=(
                    "该域可能已升级到更严的挑战；也可能是出口 IP 被提分。"
                    "先把 want 说清楚（clearance / awswaf / turnstile / recaptcha_v3），"
                    "再换站点验证服务本身可用。"
                ),
            )

        if want_enum in CACHEABLE_WANTS:
            ttl = self._ttl_from_cookie_list(r["cookie_list"], cookie_name=COOKIE_FIELD[want_enum])
            if ttl <= 0:
                if claimed:
                    self._release_claim(key)
                self._account(False, elapsed, host=host, backend="camoufox")
                raise SolveError(
                    ErrorCode.UNSOLVED,
                    f"拿到的 {COOKIE_FIELD[want_enum]} 已过期或有效期过短，不返回"
                    "（否则客户端只会拿到静默 403）",
                    stage="wait",
                    hint="重试一次；若持续出现，检查本机时钟是否准确、出口 IP 是否已被提分",
                )
        else:
            # 一次性 token：给个名义寿命提示调用方尽快用，但不缓存
            ttl = NOMINAL_TTL.get(want_enum, 120)

        self._account(True, elapsed, host=host, backend="camoufox")   # 到这里才算真的解出来了
        expires_at = _now() + ttl
        result = {
            "ok": True,
            "solved": True,
            "want": want_enum.value,
            RESULT_FIELD[want_enum]: token,
            "user_agent": ua,
            "cookies": cookies,                 # deprecated：保留兼容老客户端
            "cookie_list": r["cookie_list"],    # 结构化：含 domain/path/expires
            "expires_in": ttl,
            "expires_at": expires_at,
            "elapsed": elapsed,
            "cached": False,
            "cacheable": want_enum in CACHEABLE_WANTS,
            "egress_id": proxy_entry.egress_id if proxy_entry is not None else self.egress_id,
        }
        # 标出产出这条凭证的后端：router 路径由 router 填，直连 Solver 的路径在这里补，
        # 否则同一条链路里"有时有 backend 字段、有时没有"，调用方没法统一处理。
        result.setdefault("backend", "camoufox")
        if want_enum in TOKEN_WANTS:
            result["token_field"] = token_field     # 告诉调用方塞进哪个表单字段
        else:
            self._store.set(key, result)            # 只有 cookie 类落盘（IO 不持锁）
            if claimed:
                self._release_claim(key)            # 缓存已就绪，放行其它进程
        if self._pool is not None and proxy_entry is not None:
            self._pool.record_success(proxy_entry, elapsed)
        with self._lock:
            self._last_success_at = _now()
        return result

    def _maybe_purge(self) -> None:
        """空闲时清理过期缓存（最多每分钟一次）。多进程共享目录时各自会清。"""
        if _now() - self._last_purge < 60.0:
            return
        self._last_purge = _now()
        try:
            self._store.purge_expired()
        except Exception:  # noqa: BLE001 —— 清理失败不能影响服务
            pass

    def _store_len(self) -> int:
        try:
            return len(self._store)  # type: ignore[arg-type]
        except TypeError:
            return sum(1 for _ in self._store.items())

    def _ttl_from_cookie_list(self, cookie_list: list[dict], cookie_name: str = "cf_clearance") -> int:
        """返回这条 clearance 还能用多久（秒）；**已过期或过短时返回 0**（调用方据此拒发）。

        语义（与 `--ttl` 帮助文本一致）：`--ttl` 是**上限**，真实值取 cookie 自带的 `expires`。
        不用 `max(MIN_TTL, ...)` 兜底——那会把只剩 5 秒的死凭证当成还能用 60 秒。
        实现统一在 `backends.derive_ttl`（三个非 Camoufox 后端共用同一份，禁止各自再算一遍）。
        """
        return derive_ttl(cookie_list, cookie_name, default=self.ttl, max_ttl=self.ttl)

    # -- 健康与指标 -----------------------------------
    def health(self, *, include_internal: bool = True) -> tuple[int, dict]:
        """健康视图。`include_internal=False` 时只返回"服务是否在服务"的最小集。

        最小集是给**免鉴权**的容器探活用的；完整集含运维信息（last_error 可能带主机名、
        代理地址、缓存域数、UA 指纹），只给带 token 的调用方。
        """
        with self._lock:
            alive = self._browser_alive and self._thread.is_alive()
            if alive:
                state = "ready"
            elif not self._ever_alive:
                state = "starting"
            else:
                state = "degraded"
            minimal = {
                "ok": alive,
                "state": state,
                "browser_alive": self._browser_alive,
                "worker_alive": self._thread.is_alive(),
            }
            if not include_internal:
                return (200 if alive else 503), minimal
            body = {
                **minimal,
                "browser": "camoufox",
                # 真正会被调用的后端链。注意：上面的三态只反映**本地 Camoufox worker**，
                # 不反映链上其它后端（cfsolve 二进制缺失、pydoll 的 Chrome 被挪走）的可用性。
                "backends": list(self.backend_chain),
                "egress_id": self.egress_id,
                "cache_backend": self.cache_backend,
                # UA 指纹（不是 UA 本体）：它是缓存键的第三维。
                # 多进程共享缓存时，两个进程这里不同 ⇒ 它们不共享缓存（正确行为，不是故障）。
                "browser_os": self.browser_os or "random",
                "ua_fingerprint": (
                    hashlib.sha256(self._browser_ua.encode("utf-8")).hexdigest()[:12]
                    if self._browser_ua
                    else ""
                ),
                "cached_domains": self._store_len(),
                "queue_depth": self._tasks.qsize(),
                "solves": self._solves,
                "cache_hits": self._hits,
                "failures": self._failures,
                "timeouts": self._timeouts,
                "rejected_busy": self._rejected_busy,
                "browser_restarts": self._restarts,
                "last_success_at": self._last_success_at,
                "last_error": self._last_error,
                "last_error_at": self._last_error_at,
                "uptime_sec": round(_now() - self._started_at, 1),
            }
            avg = round(self._elapsed_sum / self._solves, 2) if self._solves else 0.0
            body["avg_elapsed_sec"] = avg
            if self._pool is not None:
                body["proxies"] = self._pool.snapshot()   # 已脱敏，不含凭据
            if self.prewarmer is not None:
                body["prewarm"] = self.prewarmer.stats.as_dict()
        return (200 if alive else 503), body

    def stats(self) -> dict:
        return self.health()[1]

    def metrics_text(self) -> str:
        """Prometheus 文本格式（渲染逻辑在 metrics.py，原样再导出）。"""
        with self._lock:
            return render_metrics_text(
                solves=self._solves,
                hits=self._hits,
                failures=self._failures,
                timeouts=self._timeouts,
                rejected_busy=self._rejected_busy,
                restarts=self._restarts,
                browser_alive=self._browser_alive,
                queue_depth=self._tasks.qsize(),
                elapsed_sum=self._elapsed_sum,
                hist=self._hist,
                backend_ok=dict(self._backend_ok),
                backend_err=dict(self._backend_err),
                host_stats_fn=getattr(self._store, "host_stats", None),
            )

    # -- 退出 -----------------------------------------
    def close(self) -> None:
        self._stopping.set()
        try:
            self._tasks.put_nowait(None)
        except queue.Full:
            pass
        self._fail_all_queued(SolveError(ErrorCode.INTERNAL, "服务正在关闭", stage="admit"))
        self._thread.join(timeout=SHUTDOWN_GRACE)


# ── HTTP 服务 ──────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    solver: Solver            # 由 main 注入
    token: str = ""           # 由 main 注入
    router = None             # 可选：配了兜底后端时使用（BackendRouter）
    server_version = "cfsolve/2.0"
    protocol_version = "HTTP/1.1"     # 支持 keep-alive；默认 HTTP/1.0 会每响应断连
    #: 在途请求计数（优雅退出要靠它"等请求做完再走"）。
    #: 为什么需要：`daemon_threads=True` 只保证进程能退出，**不保证**在途请求能拿到响应；
    #: 实测（Windows）收到终止信号时，正在求解的调用方拿到的是 `WinError 10054 连接被强制关闭`。
    inflight_lock = threading.Lock()
    inflight_requests = 0
    #: 是否允许非回环地址触发 `/shutdown`（默认禁止：这是一个能停服的高危端点）
    allow_remote_shutdown = False
    #: 审计日志路径（空=关闭）。由 `--audit-log` 注入。
    audit_path = ""
    audit_lock = threading.Lock()
    audit_fp = None

    @staticmethod
    def shutdown_allowed(peer: str) -> bool:
        """谁能触发 `/shutdown`。

        **实测教训（只有容器能暴露）**：容器 + 端口映射时，客户端地址是 docker 网关
        （如 `172.17.0.1`），不是 `127.0.0.1` —— 只放行回环会让"滚动重启"在容器里直接失败。
        所以默认只放行回环，但提供 `--allow-remote-shutdown` 给容器/编排场景；
        无论哪种，**Bearer token 都是第一道门**。
        """
        if Handler.allow_remote_shutdown:
            return True
        return (peer or "") in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

    @classmethod
    def enter_request(cls) -> None:
        with cls.inflight_lock:
            cls.inflight_requests += 1

    @classmethod
    def exit_request(cls) -> None:
        with cls.inflight_lock:
            cls.inflight_requests -= 1

    #: 空闲连接超时。开了 keep-alive 却不设超时，一条空闲连接会一直占着 handler 线程
    #: （`daemon_threads` 只影响退出，不影响运行期），长期跑会被慢客户端耗尽线程。
    timeout = 30

    # -- 日志：JSON lines，不打 cookie / token --
    def log_message(self, fmt, *args):  # noqa: N802
        pass    # 统一走 _log()

    def _log(self, **fields) -> None:
        fields.setdefault("ts", round(_now(), 3))
        print(json.dumps(fields, ensure_ascii=False), flush=True)
        self._audit_write(fields)

    def _audit_write(self, fields: dict) -> None:
        """把事件写进审计文件（`--audit-log`）。**只写白名单字段**，且失败绝不打断服务。

        为什么单列一个方法：审计是"事后追责/取证"，它是**旁路**，
        写盘失败（磁盘满 / 文件被占用）不应该让求解失败——但必须留痕到 stdout。
        """
        if not Handler.audit_path:
            return
        record = {k: fields[k] for k in AUDIT_FIELDS if k in fields}
        line = json.dumps(record, ensure_ascii=False)
        try:
            with Handler.audit_lock:
                if Handler.audit_fp is None:
                    Handler.audit_fp = open(Handler.audit_path, "a", encoding="utf-8")
                Handler.audit_fp.write(line + "\n")
                Handler.audit_fp.flush()
        except Exception as e:  # noqa: BLE001 —— 审计写失败只降级，不打断请求
            print(json.dumps({"event": "audit-write-failed", "error": f"{type(e).__name__}: {str(e)[:100]}"},
                             ensure_ascii=False), flush=True)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """GET /health 公开（供容器健康检查），其余一律要求 Bearer token。"""
        expected = self.__class__.token
        if not expected:
            return True
        header = self.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return secrets.compare_digest(header[7:].strip(), expected)

    def _deny(self) -> None:
        self._send(
            401,
            {
                "ok": False,
                "error_code": ErrorCode.UNAUTHORIZED.value,
                "error": "缺少或错误的 Bearer token",
                "stage": "admit",
                "hint": f"用 Authorization: Bearer <token>；token 在 .env 的 {TOKEN_LINE_PREFIX}<...> 或环境变量 {TOKEN_ENV}",
            },
        )

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            # 免鉴权 → 只给"服务是否在服务"的最小视图；带 token 才给完整运维视图。
            # 理由：完整视图里有 last_error（可能含被拦截子资源的主机名）、代理地址与评分、
            # 缓存域数、UA 指纹、persona。容器探活只需要状态码 + state。
            code, body = self.solver.health(include_internal=self._authorized())
            self._send(code, body)
            return
        if not self._authorized():
            self._deny()
            return
        if path == "/detect":
            self._handle_detect_from_query()
        elif path == "/stats":
            self._send(200, self.solver.stats())
        elif path == "/metrics":
            text = self.solver.metrics_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)
        else:
            self._send(404, {"ok": False, "error_code": "not-found", "error": "not found", "stage": "admit"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        # 先把 body 读完：否则提前返回 401/404 时残留的 body 会让 keep-alive 连接被对端重置
        raw, body_error = self._read_body_raw()
        if not self._authorized():
            self._deny()
            return
        if path == "/shutdown":
            self._handle_shutdown()
            return
        if path == "/detect":
            self._handle_detect(raw, body_error)
            return
        if path != "/solve":
            self._send(404, {"ok": False, "error_code": "not-found", "error": "not found", "stage": "admit"})
            return

        req_id = secrets.token_hex(4)
        started = time.monotonic()
        # 路由决策在 try 之外初始化：失败时也要把它带回给调用方
        # （实测：route=auto 求解失败时响应里完全没有 routed_by，排查时不知道"当时怎么判的"）。
        routed_by = None
        # `url` 必须在 try **之前**就绑定：空 body / 坏 JSON / 超大 body 三条早退路径
        # 都在 `url = req.get("url")` 之前就抛 SolveError，而下面的 except 会无条件读它。
        # 实测后果（v4.7 抓到）：`UnboundLocalError` 把 `_log` 那行打穿 ——
        # 响应仍是 400（只看状态码的用例全绿），但**审计里一条都没有**，
        # 且 socketserver 会替每一个畸形请求打一整段 traceback（日志洪水）。
        # 畸形输入恰恰是最需要审计的那一类，所以这里不是"兜个 None"而是修根。
        url = ""
        try:
            if body_error:
                if body_error.startswith("__payload_too_large__"):
                    # v5.0 T-51：超大 body → 413（可重试），区别于 400（不该重试）
                    raise SolveError(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"请求体超过 {MAX_BODY_BYTES} 字节",
                        stage="validate",
                        hint=f"减小请求体到 {MAX_BODY_BYTES} 字节以内后重试；如需提高上限，启动时调 --max-body-bytes",
                    )
                raise SolveError(ErrorCode.INVALID_REQUEST, body_error, stage="validate")
            req = self._parse_json(raw)
            url = req.get("url")
            if not isinstance(url, str) or not url:
                raise SolveError(ErrorCode.INVALID_REQUEST, "缺少 url 字段", stage="validate")
            if len(url) > MAX_URL_LEN:
                raise SolveError(ErrorCode.INVALID_REQUEST, f"url 超过 {MAX_URL_LEN} 字符", stage="validate")
            if not url.startswith(("http://", "https://")):
                raise SolveError(
                    ErrorCode.INVALID_REQUEST, "url 必须是 http(s)", stage="validate",
                    hint="需要带 scheme 的绝对地址，例如 https://example.com/",
                )
            timeout = req.get("timeout", DEFAULT_TIMEOUT)
            force_raw = req.get("force", False)
            if not isinstance(force_raw, (bool, int)):
                raise SolveError(ErrorCode.INVALID_REQUEST, "force 必须是布尔值", stage="validate")

            # 要哪种产物：clearance（默认）/ awswaf / turnstile / recaptcha_v3
            want_raw = req.get("want", "")
            if not isinstance(want_raw, str):
                raise SolveError(ErrorCode.INVALID_REQUEST, "want 必须是字符串", stage="validate")
            try:
                want_enum = Want.parse(want_raw)
            except ValueError as e:
                raise SolveError(ErrorCode.INVALID_REQUEST, str(e), stage="validate") from e

            sitekey = req.get("sitekey", "") or ""
            action = req.get("action", "") or ""
            cdata = req.get("cdata", "") or ""
            for name, value in (("sitekey", sitekey), ("action", action), ("cdata", cdata)):
                if not isinstance(value, str) or len(value) > 256:
                    raise SolveError(ErrorCode.INVALID_REQUEST, f"{name} 必须是不超过 256 字符的字符串",
                                     stage="validate")
            if want_enum in TOKEN_WANTS and not sitekey:
                raise SolveError(
                    ErrorCode.INVALID_REQUEST, f"want={want_enum.value} 需要 sitekey", stage="validate",
                    hint="从页面源码里取 data-sitekey，或 reCAPTCHA 的 api.js?render=<sitekey>",
                )
            # ── 入口 SSRF 守卫：**与后端无关**（v4.1）──────────────
            # 守卫原来只写在 Solver.solve() 里，而配了 cfsolve/pydoll/flaresolverr 时
            # handler 走 router 分支、根本不进 Solver.solve —— 于是整条守卫被绕过，
            # 已实测复现（假 cfsolve 二进制收到了 --page http://127.0.0.1:.../health）。
            # 正确做法：守卫在**选后端之前**执行一次，任何后端组合都拦得住。
            entry_host = _host_of(url)
            if not entry_host:
                raise SolveError(ErrorCode.INVALID_REQUEST, f"无法解析域名: {url}", stage="validate")
            self.solver._check_target(entry_host)

            # ── route：让探测结论驱动决策（v4.0 T-32）──────────────
            # 不传 route 等价于 fixed，行为与 v3 完全一致（向后兼容的第一优先级）。
            route_mode = req.get("route", "fixed")
            if not isinstance(route_mode, str):
                raise SolveError(ErrorCode.INVALID_REQUEST, "route 必须是字符串", stage="validate")
            route_mode = route_mode.strip().lower() or "fixed"
            if route_mode not in ROUTE_MODES:
                raise SolveError(
                    ErrorCode.INVALID_REQUEST, f"未知 route={route_mode!r}", stage="validate",
                    hint=f"可选 {sorted(ROUTE_MODES)}；不传等价于 fixed（v3 行为）",
                )
            if route_mode != "fixed":
                detect_result = self._probe_for_route(url, timeout=timeout) if route_mode == "auto" else None
                decision = route_decide(detect_result, route_mode)
                routed_by = decision.as_dict()
                if isinstance(detect_result, dict):
                    routed_by["detect"] = {
                        "protected": bool(detect_result.get("protected")),
                        "providers": [p.get("name") for p in (detect_result.get("providers") or [])
                                      if isinstance(p, dict)],
                        "challenge_type": detect_result.get("cloudflare_challenge_type"),
                        "route": (detect_result.get("recommendation") or {}).get("route"),
                    }
                if decision.action == ROUTE_SKIP:
                    # 没盾：不求解，直接告诉调用方去请求目标站（省掉 16–20s 与 1.2GB 浏览器）
                    self._send(200, {
                        "ok": True, "solved": False, "want": want_enum.value,
                        "no_challenge": True, "route": decision.mode,
                        "hint": decision.hint, "routed_by": routed_by,
                        "elapsed": round(time.monotonic() - started, 2),
                        "egress_id": self.solver.egress_id,
                    })
                    self._log(event="solve", req_id=req_id, host=_host_of(url), ok=True,
                              cached=False, route=decision.mode, reason=decision.reason,
                              ms=round((time.monotonic() - started) * 1000))
                    return
                if decision.action == ROUTE_REJECT:
                    # 不会就是不会：白等一个完整超时再报 unsolved 是最坏的用户体验
                    raise SolveError(
                        ErrorCode.UNSOLVED,
                        "该站使用的防护本服务不支持（" + decision.reason + "）",
                        stage="detect", hint=decision.hint, extra={"routed_by": routed_by},
                    )

            if self.router is not None:
                # 配了兜底后端：按优先级路由（未配置的后端会被 skip 并记录 attempts）
                result = self.router.solve(
                    url, timeout=_clamp_timeout(timeout), force=bool(force_raw),
                    want=want_enum.value, sitekey=sitekey, action=action, cdata=cdata,
                    # route=auto 的 prefer 现在**真的**影响选路（此前只是提示字段）
                    skip=route_skip_backends((routed_by or {}).get("prefer", "")) if routed_by else None,
                )
            else:
                result = self.solver.solve(
                    url, timeout=timeout, force=bool(force_raw),
                    want=want_enum.value, sitekey=sitekey, action=action, cdata=cdata,
                )
            if routed_by is not None:
                result = dict(result)
                result["routed_by"] = routed_by
            self._send(200, result)
            self._log(event="solve", req_id=req_id, host=_host_of(url), ok=True,
                      cached=result.get("cached"), elapsed=result.get("elapsed"),
                      backend=result.get("backend"),
                      route=(routed_by or {}).get("mode") if routed_by else None,
                      clearance_len=len(result.get("cf_clearance", "")),
                      ms=round((time.monotonic() - started) * 1000))
        except SolveError as e:
            if routed_by is not None:
                # 失败也必须回传"我是怎么路由的"：否则线上只看到 unsolved，
                # 却不知道它探测到了什么、选了哪条路。
                e.extra = {**(getattr(e, "extra", None) or {}), "routed_by": routed_by}
            self._send(e.http_status, e.payload())
            self._log(event="solve", req_id=req_id, host=_host_of(url), ok=False,
                      error_code=e.code.value, stage=e.stage,
                      route=(routed_by or {}).get("mode") if routed_by else None,
                      ms=round((time.monotonic() - started) * 1000))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            err = SolveError(ErrorCode.INTERNAL, f"{type(e).__name__}: {str(e)[:160]}",
                             stage="internal", hint="查看服务端 traceback")
            if routed_by is not None:
                err.extra = {**(getattr(err, "extra", None) or {}), "routed_by": routed_by}
            self._send(err.http_status, err.payload())
            self._log(event="solve", req_id=req_id, ok=False, error_code=err.code.value,
                      stage="internal", ms=round((time.monotonic() - started) * 1000))

    # -- 防护识别 ----------------------------------
    def _handle_shutdown(self) -> None:
        """优雅退出端点（供滚动重启用，替代 `taskkill /F`）。

        三道门（都不许省）：
          1. **鉴权**：与其它端点同一套 Bearer（`--no-auth` 时例外，那本来就是联调模式）；
          2. **来源**：默认只允许**回环地址**触发 —— 这是一个能停服的端点，
             公网可达就等于给了个 DoS 开关；
          3. **异步返回**：先 202 再真正退出，调用方不会拿到"连接被重置"。
        """
        peer = (self.client_address or ("", 0))[0]
        if not Handler.shutdown_allowed(peer):
            # 这里**必须自己回响应**：`_handle_shutdown` 是在 do_POST 的 try 之外调用的，
            # 直接 raise 只会打到 socketserver.handle_error（打印 traceback + 断连），
            # 调用方拿到的是"连接被重置"而不是文档承诺的 401。
            err = SolveError(
                ErrorCode.UNAUTHORIZED,
                f"只允许从回环地址触发优雅退出（当前来源 {peer}）",
                stage="admit",
                hint=("容器/端口映射部署请显式加 --allow-remote-shutdown："
                      "此时客户端地址是 docker 网关（如 172.17.0.1），不是 127.0.0.1，"
                      "回环判断不成立。token 仍是第一道门。"),
            )
            self._send(err.http_status, err.payload())
            self._log(event="shutdown-denied", peer=peer)
            return
        self._send(202, {"ok": True, "state": "shutting-down", "peer": peer})
        self._log(event="shutdown", peer=peer)
        srv = getattr(self, "server", None)
        if srv is not None and hasattr(srv, "begin_shutdown"):
            srv.begin_shutdown()

    def _handle_detect_from_query(self) -> None:
        from urllib.parse import parse_qs, urlsplit as _split

        query = parse_qs(_split(self.path).query)
        fresh = (query.get("fresh") or ["0"])[0].strip().lower() in ("1", "true", "yes")
        self._run_detect((query.get("url") or [""])[0], fresh=fresh)

    def _handle_detect(self, raw: bytes, body_error: str) -> None:
        """探测目标站用的是什么防护；**不做求解**，只发一次普通 GET。

        用途：先知道对面是什么（有没有盾、是哪种挑战），再决定走纯协议还是浏览器，
        避免把 16–20s 的求解浪费在"其实没盾/Akamai/DataDome"的站上。
        """
        try:
            if body_error:
                if body_error.startswith("__payload_too_large__"):
                    # v5.0 T-52：/detect 与 /solve 对齐 413 契约
                    raise SolveError(
                        ErrorCode.PAYLOAD_TOO_LARGE,
                        f"请求体超过 {MAX_BODY_BYTES} 字节",
                        stage="validate",
                        hint=f"减小请求体到 {MAX_BODY_BYTES} 字节以内后重试；如需提高上限，启动时调 --max-body-bytes",
                    )
                raise SolveError(ErrorCode.INVALID_REQUEST, body_error, stage="validate")
            payload = Handler._parse_json(raw) if raw else {}
            fresh = bool(payload.get("fresh"))
            self._run_detect(str(payload.get("url") or ""), fresh=fresh)
            return
        except SolveError as e:
            self._send(e.http_status, e.payload())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            err = SolveError(ErrorCode.INTERNAL, f"{type(e).__name__}: {str(e)[:160]}", stage="internal")
            self._send(err.http_status, err.payload())

    def _probe_for_route(self, url: str, timeout) -> dict | None:
        """给 `route=auto` 做一次轻量探测；任何异常/闸门满一律返回 None（退回照常求解）。

        三条纪律（每条都对应一个会咬人的失败模式）：
          · 探测**不许**阻断求解：探测失败只意味着"没有建议"，不是"不能解"；
          · 探测**不许**绕过闸门：`/detect` 有独立信号量，这里复用它，避免 route=auto
            变成"每个 solve 都免费多打一次出站请求"的放大器；
          · 探测**不许**绕过 SSRF 守卫：逐跳 guard 由 probe_protection 负责，这里传同一个守卫。
        """
        # 先查探测缓存：命中就不发出站请求，也不再占探测闸门
        hit = self.solver.detect_cache_get(url)
        if hit is not None:
            return hit

        gate = getattr(self.solver, "detect_inflight", None)
        got_slot = False
        if gate is not None:
            if not gate.acquire(blocking=False):
                return None            # 探测闸门满：宁可不探测，也不排队拖慢求解
            got_slot = True
        try:
            try:
                budget = max(2.0, min(8.0, float(timeout)))
            except (TypeError, ValueError):
                budget = 8.0
            result = probe_protection(url, timeout=budget, guard=self.solver.guard_hop)
            self.solver.detect_cache_put(url, result)
            return result
        except SolveError:
            return None
        except Exception:  # noqa: BLE001 —— 探测永远不该让求解路径 500
            return None
        finally:
            if got_slot:
                gate.release()

    def _run_detect(self, url: str, fresh: bool = False) -> None:
        """两种入口（GET 查询串 / POST JSON）共用的探测实现。

        `fresh=True` 表示跳过 T-34 的按域短 TTL 缓存（运维想立刻看实时结论时用）。

        与 `/solve` 同等的两道治理，缺一不可：
          · **准入**：独立信号量，满了立刻 429。探测是同步出站请求，
            没有背压就等于给调用方一个免费的流量放大器；
          · **SSRF**：入口严格校验 + **每一跳重定向**用 `guard_hop` 复检。
        """
        got_slot = False
        try:
            if not url:
                raise SolveError(ErrorCode.INVALID_REQUEST, "缺少 url", stage="validate")
            if len(url) > MAX_URL_LEN:
                raise SolveError(ErrorCode.INVALID_REQUEST, f"url 超过 {MAX_URL_LEN} 字符", stage="validate")
            if not url.startswith(("http://", "https://")):
                raise SolveError(ErrorCode.INVALID_REQUEST, "url 必须是 http(s)", stage="validate")
            host = _host_of(url)
            if not host:
                raise SolveError(ErrorCode.INVALID_REQUEST, f"无法解析域名: {url}", stage="validate")
            self.solver._check_target(host)          # 入口：严格校验（解析不了=调用方写错）
            if not fresh:
                hit = self.solver.detect_cache_get(url)
                if hit is not None:
                    self._send(200, hit)
                    return
            if not self.solver.detect_inflight.acquire(blocking=False):
                raise SolveError(
                    ErrorCode.BUSY,
                    "探测并发已达上限",
                    stage="admit",
                    hint="降低 /detect 并发，或调大 --max-detect；探测与求解额度分开计",
                )
            got_slot = True
            result = probe_protection(
                url, guard=self.solver.guard_hop, user_agent=self.solver.probe_user_agent()
            )
            self.solver.detect_cache_put(url, result)      # T-34：按域短 TTL 缓存
            self._send(200, result)
        except SolveError as e:
            self._send(e.http_status, e.payload())
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            err = SolveError(ErrorCode.INTERNAL, f"{type(e).__name__}: {str(e)[:160]}", stage="internal")
            self._send(err.http_status, err.payload())
        finally:
            if got_slot:
                self.solver.detect_inflight.release()

    def _read_body_raw(self) -> tuple[bytes, str]:
        """读请求体。返回 (bytes, 错误说明)；错误不抛异常，交由调用方决定状态码。"""
        # 本服务不解析 chunked 请求体（http.server 本身也不解析）。若只报错而不关连接，
        # 未读的 chunk 字节会留在 socket 里，在 HTTP/1.1 keep-alive 上被当成**下一个请求**
        # 的开头 —— 这是请求走私/反序列化的入口，而且本方法在鉴权**之前**就被调用。
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self.close_connection = True
            return b"", "不支持 Transfer-Encoding: chunked 请求体，请改用 Content-Length"
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return b"", "Content-Length 非法"
        if n <= 0:
            return b"", "请求体为空"
        if n > MAX_BODY_BYTES:
            # 超大 body 不读，直接告知；连接由 close_connection 收尾。
            # v5.0 T-51：413 = 可减小请求体后重试（400 = 请求坏了不该重试）。
            # 不在这里抛异常：本方法在 do_POST 的 try 之外被调用，
            # 抛出去会绕过审计与结构化响应；改为标记位，由调用方在 try 内转 413。
            self.close_connection = True
            return b"", _BODY_TOO_LARGE_MSG
        return self.rfile.read(n), ""

    @staticmethod
    def _parse_json(raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise SolveError(ErrorCode.INVALID_REQUEST, f"请求体不是合法 JSON：{e}", stage="validate")
        if not isinstance(payload, dict):
            raise SolveError(ErrorCode.INVALID_REQUEST, "请求体必须是 JSON 对象", stage="validate")
        return payload


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 上 SO_REUSEADDR 的语义与 POSIX 不同：如果目标端口已被另一个
    # （允许复用的）进程占着，我们会"绑定成功"但请求被对端接走——
    # 表现为"服务起来了、却一直返回别人的响应"，极难排查。
    # 所以在 Windows 上不要它：宁可启动时就报 OSError，也不要静默串台。
    allow_reuse_address = os.name != "nt"

    def drain(self, timeout: float) -> int:
        """等在途请求处理完（最多 timeout 秒），返回**剩余未完成数**。

        优雅退出的正确顺序：停止接收新连接（`shutdown()`）→ 等在途请求收尾 → 才 close()。
        少中间这一步，就等于"断开 listener 后立刻把还在算的请求砍掉"。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if Handler.inflight_requests == 0:
                return 0
            time.sleep(0.02)
        return Handler.inflight_requests

    def begin_shutdown(self) -> None:
        """从任意线程触发优雅退出（供 `/shutdown` 端点与信号处理共用）。"""
        threading.Thread(target=self.shutdown, daemon=True).start()


def _track_inflight(method):
    """给 handler 方法套一层在途计数（优雅退出靠它判断"还有没有人没回话"）。"""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        Handler.enter_request()
        try:
            return method(self, *args, **kwargs)
        finally:
            Handler.exit_request()
    return wrapper


Handler.do_GET = _track_inflight(Handler.do_GET)        # type: ignore[method-assign]
Handler.do_POST = _track_inflight(Handler.do_POST)      # type: ignore[method-assign]


# CLI 装配与启动已下沉 bootstrap.py（v5.0 T-56，只搬不改）：
def main(argv: list[str] | None = None) -> int:
    # 延迟导入：bootstrap 依赖本模块的常量与类，顶层 import 会造成循环。
    # main 只在进程启动时调用一次，函数级 import 无热路径开销。
    try:
        from . import bootstrap as _bootstrap
    except ImportError:  # 直接以脚本运行：python solver/service.py
        import bootstrap as _bootstrap  # type: ignore[no-redef]
    return _bootstrap.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
