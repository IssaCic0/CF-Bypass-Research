"""多后端接缝：后端协议 + 有序路由 + 统一错误码。

这一层的存在理由（而不是"为了架构好看"）：
  · 求解能力未来可能来自不同地方：本地 Camoufox、自托管的 FlareSolverr 兼容服务、
    第三方人机平台。它们的失败语义完全不同（"没解出来" / "余额不足" / "429"），
    如果不归一化，上层就只能看到 `{"error": "..."}` 而无法决定要不要换后端或重试。
  · 有序列表 = 优先级：先试谁、跳过谁（未配置就 skip 而不是报错）、每个尝试过谁，
    都必须能回传出来（`attempts`），否则线上排查只能靠猜。

v4.1 起路由层还负责**治理**（`governance=`）：缓存查询/写入、inflight 准入、
按后端与按域的计数。为什么治理必须放在这里：除 Camoufox 以外的后端
（cfsolve/pydoll/flaresolverr）都**不走 `Solver.solve`**，治理若只写在 `Solver.solve` 里，
这些后端就会"不缓存、不准入、不进指标"——热路径命中率归零、能被打穿、指标还说一切正常
（已实测复现）。

刻意不做的事：
  · 不做权重/熔断/自适应调度 —— 那是当后端数量与流量都上来之后才谈的事；
    现在只有一两个后端时，有序列表 + 失败即换下一个就够了。
  · 不用配置值拼类名/模块路径（参考成熟 captcha 框架的教训）：
    `BackendId` 枚举**就是**白名单，解析只走 `match`。
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit

try:  # 包导入
    from .errors import ErrorCode, SolveError
except ImportError:  # 直接以脚本运行
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]


class BackendId(str, Enum):
    """后端白名单。新增后端 = 加一个枚举值 + 一个分支，绝不接受配置里的任意字符串。"""

    CAMOUFOX = "camoufox"
    CFSOLVE = "cfsolve"
    PYDOLL = "pydoll"
    FLARESOLVERR = "flaresolverr"

    @classmethod
    def parse(cls, raw: str) -> "BackendId":
        for member in cls:
            if member.value == (raw or "").strip().lower():
                return member
        raise ValueError(f"未知后端 {raw!r}；可选：{[m.value for m in cls]}")


# ── cookie 类产物的有效期（单一实现，禁止各处自己算）──────────────────
#
# 为什么必须收敛成一个函数：`expires_at = 0` 会让 Go 侧 `Clearance.expiresAt()`
# 立刻判过期 ⇒ 每次回放都重新求解；服务端 `FileStore.get` 也会按
# `expires_at <= now` 直接删文件。这条不变量曾经在 FlareSolverr 后端修过一次，
# 后来新增的 cfsolve / pydoll 又各漏一次 —— 唯一能防止再漏的办法就是只留一份实现。

#: 剩余寿命低于该值即判定为"等于过期"——发出去只会让客户端吃静默 403
MIN_CLEARANCE_REMAINING = 30

#: cookie 没带 `expires`（会话级）时的名义有效期；cf_clearance 通常是 30 分钟量级
DEFAULT_COOKIE_TTL = 1500


def derive_ttl(
    cookie_list,
    cookie_name: str,
    *,
    default: int = DEFAULT_COOKIE_TTL,
    max_ttl: int | None = None,
    min_remaining: int = MIN_CLEARANCE_REMAINING,
) -> int:
    """从结构化 cookie 列表算这条凭证还能用多久（秒）。

    返回 **0** 表示"已过期或只剩不到 `min_remaining` 秒"——调用方必须据此**拒绝返回**
    这条凭证，而不是发一个注定 403 的东西出去。

    `max_ttl` 用于把结果压到配置上限（例如 `--ttl`）；`default` 用于 cookie 没带 expires
    的会话级情形（此时无法判断真实寿命，只能给名义值并在文档里写清是假设）。
    """
    for c in cookie_list or ():
        if c.get("name") != cookie_name:
            continue
        exp = c.get("expires", -1)
        if isinstance(exp, (int, float)) and exp > 0:
            remaining = int(exp - time.time())
            if remaining < min_remaining:
                return 0
            return min(remaining, max_ttl) if max_ttl else remaining
        break
    return min(default, max_ttl) if max_ttl else default


class SolverBackend(Protocol):
    """一个"能把 URL 换成 clearance"的后端。

    实现纪律（照着做，别绕）：
      · `solve` 成功时返回与 `/solve` 同形状的 dict（至少含 ok/solved/cf_clearance/user_agent）。
      · 失败必须抛 `SolveError` 并带上准确的 `error_code`，**不要**返回 ok:false 让上层猜。
      · `is_configured()` 要在**发请求之前**判断（缺 key / 缺依赖 → 直接 skip）。
    """

    def name(self) -> str: ...
    def is_configured(self) -> bool: ...
    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict: ...


class CamoufoxBackend:
    """把本地 `Solver` 适配成后端。solver 自己已经做了缓存/准入/健康。"""

    def __init__(self, solver) -> None:
        self._solver = solver

    def name(self) -> str:
        return BackendId.CAMOUFOX.value

    def is_configured(self) -> bool:
        return True

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        out = self._solver.solve(url, timeout=timeout, force=force, **kwargs)
        out.setdefault("backend", self.name())
        return out


class FlareSolverrBackend:
    """自托管 FlareSolverr 兼容服务（`POST /v1`）的客户端后端。

    为什么拿它当第二个后端：它的 HTTP 契约稳定且被大量 Go/Python 客户端实现过
    （`cmd` / `maxTimeout` / `session` / `returnOnlyCookies` / `proxy`），
    自己起一个容器就能当兜底，不涉及任何付费 API。

    ⚠️ 重要限制（不要当成主力）：
      · 上游项目已进入维护模式，不再跟新 CF 版本 → 兜底亦可能拿不到 clearance。
      · 本实现只做了契约级验证（用 httptest 打桩），**未对真实自托管实例做真机验证**。
    """

    def __init__(
        self,
        base_url: str,
        *,
        session: str = "",
        proxy: str = "",
        request_timeout: float = 90.0,
        egress_id: str = "flaresolverr",
        default_ttl: int = 1500,
    ) -> None:
        if not base_url:
            raise ValueError("base_url 不能为空")
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.proxy = proxy
        self.request_timeout = request_timeout
        self.egress_id = egress_id        # 兜底后端也有出口身份，必须回传（否则缓存键无从判断）
        self.default_ttl = default_ttl
        self._client = None

    def name(self) -> str:
        return BackendId.FLARESOLVERR.value

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _http(self):
        if self._client is None:
            import urllib.request

            self._client = urllib.request  # 零依赖：只用标准库
        return self._client

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        # 这个后端只发 GET、只回收 cookie，不支持 want=token 之类的语义（未知参数忽略）
        import urllib.error
        import urllib.request

        body = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": int(max(timeout, 5.0) * 1000),
            "returnOnlyCookies": True,
        }
        if self.session:
            body["session"] = self.session
        if self.proxy:
            body["proxy"] = {"url": self.proxy}

        t0 = time.monotonic()
        req = urllib.request.Request(
            self.base_url + "/v1",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                payload = json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise SolveError(
                ErrorCode.TRANSPORT,
                f"FlareSolverr 返回 HTTP {e.code}",
                stage="solve",
                hint="检查自托管实例是否存活、版本是否匹配",
            ) from e
        except Exception as e:  # noqa: BLE001
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE,
                f"连不上 FlareSolverr：{type(e).__name__}: {str(e)[:120]}",
                stage="admit",
                hint="确认 FLARESOLVERR_URL 可访问，且容器已就绪",
            ) from e

        elapsed = round(time.monotonic() - t0, 2)
        if str(payload.get("status", "")).lower() != "ok":
            msg = str(payload.get("message", "unknown"))[:200]
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"FlareSolverr 未解出：{msg}",
                stage="wait",
                hint="该后端已停止跟进新版 CF；把它当兜底而不是主力",
            )

        sol = payload.get("solution") or {}
        raw_cookies = sol.get("cookies") or []
        cookie_list = []
        cookies: dict[str, str] = {}
        for c in raw_cookies:
            name = c.get("name", "")
            if not name:
                continue
            cookies[name] = c.get("value", "")
            cookie_list.append(
                {
                    "name": name,
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expiry", c.get("expires", -1)),
                    "http_only": bool(c.get("httpOnly", False)),
                    "secure": bool(c.get("secure", False)),
                    "same_site": c.get("sameSite", ""),
                }
            )

        if "cf_clearance" not in cookies:
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"FlareSolverr 返回成功但无 cf_clearance（拿到 {len(cookies)} 个 cookie）",
                stage="wait",
            )

        # TTL 必须给出来且必须真实：返回 0 会让 Go 侧 `expiresAt()` 立刻判过期，
        # 于是客户端每次回放都重新求解（Critic 抓到的真实缺陷）。
        ttl = self._ttl(cookie_list)
        if ttl <= 0:
            raise SolveError(
                ErrorCode.UNSOLVED,
                "FlareSolverr 返回的 cf_clearance 已过期或剩余寿命过短，不返回"
                "（否则客户端只会拿到静默 403）",
                stage="wait",
                hint="重试一次；持续出现请检查该实例的系统时钟与出口 IP 是否与求解时一致",
            )
        now = time.time()

        return {
            "ok": True,
            "solved": True,
            "cf_clearance": cookies["cf_clearance"],
            "user_agent": sol.get("userAgent", ""),
            "cookies": cookies,
            "cookie_list": cookie_list,
            "expires_in": ttl,
            "expires_at": now + ttl,
            "elapsed": elapsed,
            "cached": False,
            "egress_id": self.egress_id,
            "backend": self.name(),
        }

    def _ttl(self, cookie_list: list[dict]) -> int:
        """取 cf_clearance 的剩余寿命；统一实现见模块级 `derive_ttl`（禁止各处自己算）。"""
        return derive_ttl(
            cookie_list, "cf_clearance", default=self.default_ttl, max_ttl=self.default_ttl
        )


#: 终止性错误：这些不是"这个后端不行，换一个试试"，而是"这次请求本身不该发"。
#: 若仍落到 fallback，就等于把 SSRF 守卫、鉴权、背压全部绕过（Critic 抓到的真实缺陷）。
TERMINAL_CODES = frozenset(
    {
        ErrorCode.INVALID_REQUEST,
        ErrorCode.UNAUTHORIZED,
        ErrorCode.BUSY,
        ErrorCode.RATE_LIMITED,
    }
)

#: 单后端至少要有这么多预算才值得再试下一个。
#: 为什么需要：`timeout` 是**整条链**的预算，不是每个后端的预算。
#: 若每个后端都拿满 timeout，四后端最坏 120+45+120+90≈375s，而 Go 客户端 240s 就先超时
#: —— 调用方拿到的是裸 net 错误而不是结构化 *APIError（这正是 R5 修过、多后端又打开的问题）。
MIN_BACKEND_BUDGET = 5.0


class BackendRouter:
    """按顺序尝试后端；未配置的记 skipped，失败的记 attempts，全败则抛结构化错误。

    纪律：**终止性错误（`TERMINAL_CODES`）立刻上抛，不进入 fallback。**
    换后端只对"这个后端解不出来"有意义；参数写错、没鉴权、队列满时换谁都一样，
    继续试只会放大攻击面与负载。
    """

    def __init__(self, backends: list[SolverBackend], *, governance=None) -> None:
        """`governance` 需提供 cache_lookup / remember / begin_backend_call / end_backend_call。

        传 None 时退化为"纯路由"（只有 Camoufox 那一棒受 Solver 的治理）。
        """
        if not backends:
            raise ValueError("至少需要一个后端")
        self._backends = backends
        self._gov = governance

    @property
    def backends(self) -> list[str]:
        return [b.name() for b in self._backends]

    def _governed_solve(self, backend, url: str, timeout: float, force: bool, **kwargs) -> dict:
        """给"不走 Solver.solve"的后端套上统一治理。

        三个动作必须成对，漏一个就会回归成 v4.0 的老毛病：
          ① 先查缓存（命中直接返回，不启子进程/浏览器）
          ② 拿 inflight 门票（拿不到立刻 BUSY，不做无界排队）
          ③ 无论成败都 end_backend_call（否则门票泄漏 → 服务"莫名其妙全 busy"）
        """
        gov = self._gov
        want = str(kwargs.get("want") or "clearance").lower()
        cacheable = want in ("clearance", "awswaf")   # 一次性 token 绝不缓存（复用必失败）

        claimed = False
        if cacheable and not force:
            hit = gov.cache_lookup(url)
            if hit is not None:
                hit.setdefault("backend", backend.name())
                return hit
            # 跨进程去重：多个 worker 同时抢同一个新域时只让一个真的解，
            # 其余等对端填好缓存（否则 4 进程 burst 会各解一次，实测 12/12）。
            claim = getattr(gov, "claim_solve", None)
            if callable(claim):
                claimed = bool(claim(url, timeout))
                if not claimed:
                    wait = getattr(gov, "wait_for_peer", None)
                    peer = wait(url, timeout) if callable(wait) else None
                    if peer is not None:
                        peer.setdefault("backend", backend.name())
                        return peer

        if not gov.begin_backend_call():
            raise SolveError(
                ErrorCode.BUSY, "并发求解数已达上限", stage="admit",
                hint="降低并发或提高 --max-inflight；注意单进程只串行求解，调大只增加排队深度",
            )

        try:
            host = urlsplit(url).hostname or ""
        except Exception:  # noqa: BLE001
            host = ""

        started = time.monotonic()
        try:
            out = backend.solve(url, timeout=timeout, force=force, **kwargs)
        except BaseException:
            gov.end_backend_call(False, time.monotonic() - started, host=host, backend=backend.name())
            if claimed:
                release = getattr(gov, "release_claim_for", None)
                if callable(release):
                    release(url)      # 失败也要放行其它进程，别让它们白等 claim 的 TTL
            raise
        gov.end_backend_call(True, time.monotonic() - started, host=host, backend=backend.name())
        if cacheable and isinstance(out, dict):
            gov.remember(url, out)
            if claimed:
                release = getattr(gov, "release_claim_for", None)
                if callable(release):
                    release(url)      # 缓存已就绪，放行其它进程
        return out

    def solve(self, url: str, timeout: float, force: bool = False,
              *, skip: frozenset[str] | set[str] | None = None, **kwargs) -> dict:
        """按顺序试后端。`skip` 是本次请求**明确要跳过**的后端名（来自 route=auto 的 prefer）。

        安全阀：如果 skip 把整条链都跳完了，则**忽略 skip** 并记录原因 ——
        否则调用方传个 `route=pure` 就会得到"没有任何可用后端"，那是自己制造的故障。
        """
        attempts: list[dict] = []
        last_error: SolveError | None = None
        skip_set = {str(x) for x in (skip or ())}
        if skip_set and all(b.name() in skip_set for b in self._backends if b.is_configured()):
            skip_set = set()          # 全被跳完 → 放弃跳过，正常尝试
        # `timeout` 是整条链的总预算：按墙钟 deadline 切分给每个后端，而不是每个都给满。
        # 这样最坏耗时 ≈ timeout（+ 单后端的排队余量），不会把客户端的超时先撞穿。
        deadline = time.monotonic() + max(MIN_BACKEND_BUDGET, float(timeout or 0))

        for backend in self._backends:
            name = backend.name()
            if not backend.is_configured():
                attempts.append({"backend": name, "skipped": True, "reason": "not-configured"})
                continue
            if name in skip_set:
                attempts.append({"backend": name, "skipped": True, "reason": "prefer-skip"})
                continue
            remaining = deadline - time.monotonic()
            if remaining < MIN_BACKEND_BUDGET:
                # 预算已经花光：再启动一个后端只会让调用方在客户端侧先超时
                attempts.append({"backend": name, "skipped": True, "reason": "deadline-exhausted"})
                continue
            try:
                if self._gov is not None and not isinstance(backend, CamoufoxBackend):
                    # 非 Solver 后端：由路由层统一施加缓存 / 准入 / 计数
                    out = self._governed_solve(backend, url, remaining, force, **kwargs)
                else:
                    out = backend.solve(url, timeout=remaining, force=force, **kwargs)
                out.setdefault("backend", name)
                out["attempts"] = attempts + [{"backend": name, "ok": True}]
                return out
            except SolveError as e:
                attempts.append(
                    {"backend": name, "ok": False, "error_code": e.code.value, "stage": e.stage}
                )
                if e.code in TERMINAL_CODES:
                    e.extra = {**e.extra, "attempts": attempts}
                    raise
                last_error = e
                continue
            except Exception as e:  # noqa: BLE001 —— 未预期异常也要能被 fallback 吞掉并记录
                last_error = SolveError(
                    ErrorCode.INTERNAL, f"{name}: {type(e).__name__}: {str(e)[:120]}", stage="solve"
                )
                attempts.append({"backend": name, "ok": False, "error_code": "internal"})
                continue

        if last_error is not None:
            last_error.extra = {"attempts": attempts}
            raise last_error
        if any(a.get("reason") == "deadline-exhausted" for a in attempts):
            raise SolveError(
                ErrorCode.TIMEOUT,
                f"{float(timeout or 0):.0f}s 预算内没能把后端链试完",
                stage="wait",
                hint="调大请求里的 timeout（上限 120s），或减少同时配置的后端数量",
                extra={"attempts": attempts},
            )
        raise SolveError(
            ErrorCode.NOT_CONFIGURED,
            "没有任何可用后端",
            stage="admit",
            hint="检查 --flaresolverr 配置，以及本地 Camoufox 是否已 fetch",
            extra={"attempts": attempts},
        )
