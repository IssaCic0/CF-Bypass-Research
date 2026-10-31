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
from urllib.parse import urlsplit

try:  # 作为包导入（tests / 未来 solver 包）
    from .errors import ErrorCode, SolveError
    from .cache import build_store
    from .proxypool import ProxyPool, parse_proxy_url
    from .backends import BackendRouter, CamoufoxBackend, FlareSolverrBackend
except ImportError:  # 直接以脚本运行：python solver/service.py
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]
    from cache import build_store  # type: ignore[no-redef]
    from proxypool import ProxyPool, parse_proxy_url  # type: ignore[no-redef]
    from backends import BackendRouter, CamoufoxBackend, FlareSolverrBackend  # type: ignore[no-redef]

# ── 常量 ───────────────────────────────────────────
DEFAULT_TTL = 1500          # cf_clearance 通常 30 分钟量级；cookie 带 expires 时以 cookie 为准
MIN_TTL = 60
#: 剩余寿命低于该值就判定为"等于过期"——发出去只会让客户端吃静默 403
MIN_CLEARANCE_REMAINING = 30
MAX_URL_LEN = 2048
MAX_BODY_BYTES = 64 * 1024
DEFAULT_TIMEOUT = 45.0
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 120.0         # 上限：避免 timeout=1e9 把 worker 永久占住
SUBMIT_SLACK = 5.0          # worker 等待上限 = 预算 + 该余量
POLL_INTERVAL = 0.15        # 等 cookie 的轮询间隔（原 1.0s 平均白等 ~500ms）
GOTO_BUDGET_RATIO = 0.6     # 导航最多占预算的 60%
GOTO_BUDGET_CAP = 25.0
DEFAULT_MAX_QUEUE = 16
DEFAULT_MAX_INFLIGHT = 8
DEFAULT_WARM_RETRY_MAX = 30.0
SHUTDOWN_GRACE = 10.0
TOKEN_ENV = "CF_SOLVE_TOKEN"
TOKEN_LINE_PREFIX = "CF_SOLVE_TOKEN="

#: 延迟直方图分桶（秒），用于 /metrics
LATENCY_BUCKETS = (1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)

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
def load_or_create_token(path: str) -> tuple[str, str]:
    """按优先级取 token：环境变量 → token 文件 → 自动生成并原子写入。

    返回 (token, source)，source ∈ {"env", "file", "generated"}。
    自动生成时用 O_CREAT|O_EXCL 独占创建；若并发下已存在则回读（多进程首启竞态）。
    """
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env, "env"

    if os.path.exists(path):
        token = _read_token_file(path)
        if token:
            return token, "file"

    token = secrets.token_urlsafe(32)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    line = f"{TOKEN_LINE_PREFIX}{token}\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_token_file(path)
        if existing:
            return existing, "file"
        # 文件存在但没有 token 行：追加（保留用户其它 env 内容）
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        os.chmod(path, 0o600)
        return token, "generated"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(line)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return token, "generated"


def _read_token_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith(TOKEN_LINE_PREFIX):
                    return line[len(TOKEN_LINE_PREFIX):].strip()
    except OSError:
        return ""
    return ""


def token_fingerprint(token: str) -> str:
    """日志里只打指纹，不打 token 本体。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


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
        self._elapsed_sum = 0.0
        self._hist = [0] * len(LATENCY_BUCKETS)

        self._dns_cache: dict[str, tuple[float, str]] = {}

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
            raise SolveError(
                ErrorCode.INVALID_REQUEST, f"域名无法解析：{host}", stage="validate",
                hint="检查 URL 拼写，或确认求解机可访问该域名",
            )
        self._dns_cache[host] = (_now() + 60.0, host)

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
    def _do_solve(self, browser, url: str, budget: float, proxy: dict | None = None) -> dict:
        t_end = time.monotonic() + budget
        ctx = browser.new_context(proxy=proxy)
        # 每条真实发出的请求都要过守卫：只校验首个 URL 是可以被 302 / 子资源绕过的
        violation: dict = {}
        self._install_request_guard(ctx, violation)
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
            while True:
                if violation.get("top_level"):
                    break
                cookies, cookie_list = self._read_cookies(ctx)
                if "cf_clearance" in cookies:
                    break
                if time.monotonic() >= t_end:
                    break
                time.sleep(POLL_INTERVAL)

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
                "goto_error": goto_error,
                "budget_left": max(0.0, t_end - time.monotonic()),
            }
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    def _install_request_guard(self, ctx, violation: dict) -> None:
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
        if self._allow_private_hosts:
            return

        allow_cache: dict[str, bool] = {}

        def check(url: str, *, top_level: bool) -> bool:
            """返回 True=允许，False=拦截。"""
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
            if not check(req.url or "", top_level=is_top_level(req)):
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
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE,
                f"SSRF 守卫安装失败（{type(e).__name__}: {str(e)[:100]}），已拒绝求解",
                stage="admit",
                hint="安全控制不允许静默失效：升级 playwright，或明确用 --allow-private-hosts 关闭防护",
            ) from e

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
    def solve(self, url: str, timeout: float = DEFAULT_TIMEOUT, force: bool = False) -> dict:
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
        host = _host_of(url)
        if not host:
            raise SolveError(ErrorCode.INVALID_REQUEST, f"无法解析域名: {url}", stage="validate")
        budget = _clamp_timeout(timeout)
        self._check_target(host)

        # 先定出口（粘性），缓存键随出口变化
        proxy_entry = self._pool.for_host(host) if self._pool is not None else None
        if self._pool is not None and proxy_entry is None:
            raise SolveError(
                ErrorCode.BUSY,
                "所有代理出口都在冷却中",
                stage="admit",
                hint="等冷却结束，或加更多出口；/health 的 proxies 字段能看到各自冷却剩余时间",
            )

        key = self.cache_key(host, proxy_entry)
        if not force:
            cached = self._store.get(key)
            if cached and cached.get("expires_at", 0) > _now():
                with self._lock:
                    self._hits += 1
                hit = dict(cached)
                hit.update({"ok": True, "solved": True, "cached": True, "elapsed": 0.0})
                return hit

        # 先把代理字典算出来**再**拿信号量：解析失败时不能留下未释放的额度。
        # 同时保证错误消息里不出现凭据（parse_proxy_url 的原始报错会带 user:pass）。
        try:
            proxy = self._proxy_dict(proxy_entry)
        except ValueError:
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE,
                "代理配置无法解析（凭据已隐去）",
                stage="admit",
                hint="检查 --proxy 的格式：scheme://user:pass@host:port",
            )

        # 准入：inflight 上限，满了立刻拒绝（不等）
        if not self._inflight.acquire(blocking=False):
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
            r = self._submit(lambda b: self._do_solve(b, url, budget, proxy), budget)
        except SolveError as e:
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
        with self._lock:
            self._solves += 1
            self._elapsed_sum += elapsed
            for i, edge in enumerate(LATENCY_BUCKETS):
                if elapsed <= edge:
                    self._hist[i] += 1
                    break

        cookies = r["cookies"]
        ua = r.get("ua") or self._browser_ua
        if "cf_clearance" not in cookies:
            with self._lock:
                self._failures += 1
            if self._pool is not None and proxy_entry is not None:
                self._pool.record_failure(proxy_entry, "unsolved")
            # 一个 cookie 都没拿到 + 导航报错 ⇒ 是网络/传输层失败，不是"被盾拒"
            if not cookies and r.get("goto_error"):
                raise SolveError(
                    ErrorCode.TRANSPORT,
                    f"导航失败：{r['goto_error']}",
                    stage="navigate",
                    hint="检查 DNS/出口连通性；若开了代理，确认代理可用且没被目标封",
                )
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"{budget:.0f}s 内未取得 cf_clearance",
                stage="wait",
                hint="该域可能已升级到 Turnstile 或更严的 managed challenge；也可能是出口 IP 被提分。"
                     "先换站点验证服务本身可用，再考虑 CAMOUFOX 升级。",
            )

        ttl = self._ttl_from_cookie_list(r["cookie_list"])
        if ttl <= 0:
            with self._lock:
                self._failures += 1
            raise SolveError(
                ErrorCode.UNSOLVED,
                "拿到的 cf_clearance 已过期或有效期过短，不返回（否则客户端只会拿到静默 403）",
                stage="wait",
                hint="重试一次；若持续出现，检查本机时钟是否准确、出口 IP 是否已被提分",
            )
        expires_at = _now() + ttl
        result = {
            "ok": True,
            "solved": True,
            "cf_clearance": cookies["cf_clearance"],
            "user_agent": ua,
            "cookies": cookies,                 # deprecated：保留兼容老客户端
            "cookie_list": r["cookie_list"],    # 结构化：含 domain/path/expires
            "expires_in": ttl,
            "expires_at": expires_at,
            "elapsed": elapsed,
            "cached": False,
            "egress_id": proxy_entry.egress_id if proxy_entry is not None else self.egress_id,
        }
        self._store.set(key, result)        # 落盘 IO 不持锁
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

    def _ttl_from_cookie_list(self, cookie_list: list[dict]) -> int:
        """返回这条 clearance 还能用多久（秒）；**已过期或过短时返回 0**（调用方据此拒发）。

        语义（与 `--ttl` 帮助文本一致）：`--ttl` 是**上限**，真实值取 cookie 自带的 `expires`。
        不用 `max(MIN_TTL, ...)` 兜底——那会把只剩 5 秒的死凭证当成还能用 60 秒。
        """
        for c in cookie_list:
            if c.get("name") != "cf_clearance":
                continue
            exp = c.get("expires", -1)
            if isinstance(exp, (int, float)) and exp > 0:
                remaining = int(exp - _now())
                if remaining < MIN_CLEARANCE_REMAINING:
                    return 0
                return min(remaining, self.ttl)
            break
        # cookie 没有 expires（会话级）→ 退回配置值
        return self.ttl

    # -- 健康与指标 -----------------------------------
    def health(self) -> tuple[int, dict]:
        with self._lock:
            alive = self._browser_alive and self._thread.is_alive()
            if alive:
                state = "ready"
            elif not self._ever_alive:
                state = "starting"
            else:
                state = "degraded"
            body = {
                "ok": alive,
                "browser": "camoufox",
                "state": state,
                "browser_alive": self._browser_alive,
                "worker_alive": self._thread.is_alive(),
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
        return (200 if alive else 503), body

    def stats(self) -> dict:
        return self.health()[1]

    def metrics_text(self) -> str:
        """Prometheus 文本格式。只暴露计数与桶，不含任何域名/凭证。"""
        with self._lock:
            q = self._tasks.qsize()
            alive = 1 if self._browser_alive else 0
            lines = [
                "# HELP cfsolve_solves_total 真实求解次数（含失败）",
                "# TYPE cfsolve_solves_total counter",
                f"cfsolve_solves_total {self._solves}",
                "# HELP cfsolve_cache_hits_total 缓存命中次数",
                "# TYPE cfsolve_cache_hits_total counter",
                f"cfsolve_cache_hits_total {self._hits}",
                "# HELP cfsolve_failures_total 求解失败次数",
                "# TYPE cfsolve_failures_total counter",
                f"cfsolve_failures_total {self._failures}",
                "# HELP cfsolve_timeouts_total 超时次数",
                "# TYPE cfsolve_timeouts_total counter",
                f"cfsolve_timeouts_total {self._timeouts}",
                "# HELP cfsolve_rejected_total 准入拒绝次数",
                "# TYPE cfsolve_rejected_total counter",
                f"cfsolve_rejected_total {self._rejected_busy}",
                "# HELP cfsolve_browser_restarts_total 浏览器重启次数",
                "# TYPE cfsolve_browser_restarts_total counter",
                f"cfsolve_browser_restarts_total {self._restarts}",
                "# HELP cfsolve_browser_alive 浏览器是否存活",
                "# TYPE cfsolve_browser_alive gauge",
                f"cfsolve_browser_alive {alive}",
                "# HELP cfsolve_queue_depth 队列深度",
                "# TYPE cfsolve_queue_depth gauge",
                f"cfsolve_queue_depth {q}",
                "# HELP cfsolve_solve_seconds 求解耗时分布",
                "# TYPE cfsolve_solve_seconds histogram",
            ]
            cumulative = 0
            for edge, count in zip(LATENCY_BUCKETS, self._hist):
                cumulative += count
                lines.append(f'cfsolve_solve_seconds_bucket{{le="{edge:g}"}} {cumulative}')
            lines.append(f'cfsolve_solve_seconds_bucket{{le="+Inf"}} {self._solves}')
            lines.append(f"cfsolve_solve_seconds_sum {round(self._elapsed_sum, 3)}")
            lines.append(f"cfsolve_solve_seconds_count {self._solves}")
        return "\n".join(lines) + "\n"

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
    #: 空闲连接超时。开了 keep-alive 却不设超时，一条空闲连接会一直占着 handler 线程
    #: （`daemon_threads` 只影响退出，不影响运行期），长期跑会被慢客户端耗尽线程。
    timeout = 30

    # -- 日志：JSON lines，不打 cookie / token --
    def log_message(self, fmt, *args):  # noqa: N802
        pass    # 统一走 _log()

    def _log(self, **fields) -> None:
        fields.setdefault("ts", round(_now(), 3))
        print(json.dumps(fields, ensure_ascii=False), flush=True)

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
            code, body = self.solver.health()
            self._send(code, body)
            return
        if not self._authorized():
            self._deny()
            return
        if path == "/stats":
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
        if path != "/solve":
            self._send(404, {"ok": False, "error_code": "not-found", "error": "not found", "stage": "admit"})
            return

        req_id = secrets.token_hex(4)
        started = time.monotonic()
        try:
            if body_error:
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
            if self.router is not None:
                # 配了兜底后端：按优先级路由（未配置的后端会被 skip 并记录 attempts）
                result = self.router.solve(url, timeout=_clamp_timeout(timeout), force=bool(force_raw))
            else:
                result = self.solver.solve(url, timeout=timeout, force=bool(force_raw))
            self._send(200, result)
            self._log(event="solve", req_id=req_id, host=_host_of(url), ok=True,
                      cached=result.get("cached"), elapsed=result.get("elapsed"),
                      clearance_len=len(result.get("cf_clearance", "")),
                      ms=round((time.monotonic() - started) * 1000))
        except SolveError as e:
            self._send(e.http_status, e.payload())
            self._log(event="solve", req_id=req_id, ok=False, error_code=e.code.value,
                      stage=e.stage, ms=round((time.monotonic() - started) * 1000))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            err = SolveError(ErrorCode.INTERNAL, f"{type(e).__name__}: {str(e)[:160]}",
                             stage="internal", hint="查看服务端 traceback")
            self._send(err.http_status, err.payload())
            self._log(event="solve", req_id=req_id, ok=False, error_code=err.code.value,
                      stage="internal", ms=round((time.monotonic() - started) * 1000))

    def _read_body_raw(self) -> tuple[bytes, str]:
        """读请求体。返回 (bytes, 错误说明)；错误不抛异常，交由调用方决定状态码。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return b"", "Content-Length 非法"
        if n <= 0:
            return b"", "请求体为空"
        if n > MAX_BODY_BYTES:
            # 超大 body 不读，直接告知；连接由 close_connection 收尾
            self.close_connection = True
            return b"", f"请求体超过 {MAX_BODY_BYTES} 字节"
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cloudflare cf_clearance 求解服务（Camoufox）")
    ap.add_argument("--port", type=int, default=8878)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL,
                    help="缓存有效期**上限**（秒）。真实值取 cf_clearance 自带的 expires，且不超过本值")
    ap.add_argument("--headed", action="store_true", help="有头模式（调试用）")
    ap.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE, help="有界队列容量，满即 429")
    ap.add_argument("--max-inflight", type=int, default=DEFAULT_MAX_INFLIGHT, help="并发求解上限，满即 429")
    ap.add_argument("--egress-id", default=os.environ.get("CF_EGRESS_ID", "default"),
                    help="本进程的出口标识；换出口必须换 id，否则会复用别处 IP 的凭证")
    ap.add_argument("--token-file", default=".env", help=f"token 文件；首次启动自动生成并写入 {TOKEN_LINE_PREFIX}<token>")
    ap.add_argument("--no-auth", action="store_true", help="关闭鉴权（仅限本机调试；生产禁用）")
    ap.add_argument("--allow-private-hosts", action="store_true", help="允许访问私网/回环地址（仅限本机联调）")
    ap.add_argument("--cache-dir", default=os.environ.get("CF_CACHE_DIR", ""),
                    help="落盘缓存目录（多进程共享）的路径；空=进程内内存缓存")
    ap.add_argument("--cache-max-files", type=int, default=4096, help="落盘缓存的文件数上限（超出按最旧淘汰）")
    ap.add_argument("--proxy", action="append", default=[],
                    help="代理出口，可重复：scheme://user:pass@host:port。按域一致性哈希做粘性")
    ap.add_argument("--proxy-egress-ip", action="append", default=[],
                    help="登记某出口的实测 IP，格式 http://host:port=1.2.3.4（改 IP 会自动失效旧缓存）")
    ap.add_argument("--browser-os", default=os.environ.get("CF_BROWSER_OS", ""),
                    choices=["", "windows", "macos", "linux"],
                    help="固定 Camoufox 的 OS persona（windows/macos/linux）；空=随机（Camoufox 默认，每次启动会变）")
    ap.add_argument("--flaresolverr", default=os.environ.get("FLARESOLVERR_URL", ""),
                    help="可选兜底后端：自托管 FlareSolverr 兼容服务的地址（例如 http://127.0.0.1:8191）")
    args = ap.parse_args(argv)

    token = ""
    source = "disabled"
    if not args.no_auth:
        token, source = load_or_create_token(args.token_file)

    pool = None
    if args.proxy:
        pool = ProxyPool(args.proxy)
        for item in args.proxy_egress_ip:
            server, _, ip = item.partition("=")
            if ip and not pool.set_egress_ip_by_server(server.strip(), ip.strip()):
                print(f"[cfsolve] 警告：--proxy-egress-ip 里的出口不在 --proxy 列表中：{server}", flush=True)

    solver = Solver(
        headless=not args.headed,
        ttl=args.ttl,
        max_queue=args.max_queue,
        max_inflight=args.max_inflight,
        egress_id=args.egress_id,
        allow_private_hosts=args.allow_private_hosts,
        cache_dir=args.cache_dir or None,
        token=token or "memory-only",     # 无 token 时也给个固定值，保证能派生密钥
        cache_max_files=args.cache_max_files,
        proxy_pool=pool,
        browser_os=args.browser_os,
    )
    Handler.solver = solver
    Handler.token = token
    if args.flaresolverr:
        Handler.router = BackendRouter(
            [CamoufoxBackend(solver), FlareSolverrBackend(args.flaresolverr)]
        )
    else:
        Handler.router = None

    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"[cfsolve] 端口 {args.host}:{args.port} 绑定失败：{e}", flush=True)
        print("[cfsolve] 换一个 --port，或用 `netstat -ano | findstr :PORT` 找到并停掉占用进程", flush=True)
        solver.close()
        return 2

    print(
        f"[cfsolve] 监听 http://{args.host}:{args.port} "
        f"(camoufox, ttl={args.ttl}s, egress={args.egress_id}, auth={source}, "
        f"cache={solver.cache_backend}, proxies={0 if pool is None else len(args.proxy)})",
        flush=True,
    )
    if Handler.router is not None:
        print(f"[cfsolve] 后端优先级：{Handler.router.backends}（未配置的会被 skip）", flush=True)
    if token:
        print(f"[cfsolve] token 指纹 {token_fingerprint(token)}（来源 {source}；"
              f"完整 token 见 {args.token_file} 或环境变量 {TOKEN_ENV}）", flush=True)

    def _shutdown(signum, _frame):
        print(f"[cfsolve] 收到信号 {signum}，开始优雅退出", flush=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass    # 非主线程/平台不支持时忽略

    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        srv.server_close()
        solver.close()
    print("[cfsolve] 已退出", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
