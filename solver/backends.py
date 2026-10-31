"""多后端接缝：后端协议 + 有序路由 + 统一错误码。

这一层的存在理由（而不是"为了架构好看"）：
  · 求解能力未来可能来自不同地方：本地 Camoufox、自托管的 FlareSolverr 兼容服务、
    第三方人机平台。它们的失败语义完全不同（"没解出来" / "余额不足" / "429"），
    如果不归一化，上层就只能看到 `{"error": "..."}` 而无法决定要不要换后端或重试。
  · 有序列表 = 优先级：先试谁、跳过谁（未配置就 skip 而不是报错）、每个尝试过谁，
    都必须能回传出来（`attempts`），否则线上排查只能靠猜。

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

try:  # 包导入
    from .errors import ErrorCode, SolveError
except ImportError:  # 直接以脚本运行
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]


class BackendId(str, Enum):
    """后端白名单。新增后端 = 加一个枚举值 + 一个分支，绝不接受配置里的任意字符串。"""

    CAMOUFOX = "camoufox"
    FLARESOLVERR = "flaresolverr"

    @classmethod
    def parse(cls, raw: str) -> "BackendId":
        for member in cls:
            if member.value == (raw or "").strip().lower():
                return member
        raise ValueError(f"未知后端 {raw!r}；可选：{[m.value for m in cls]}")


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

        # TTL 必须给出来：返回 0 会让 Go 侧 `expiresAt()` 立刻判过期，
        # 于是客户端每次回放都重新求解（Critic 抓到的真实缺陷）。
        ttl = self._ttl(cookie_list)
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
        """取 cf_clearance 的剩余寿命；拿不到就用 default_ttl。"""
        now = time.time()
        for c in cookie_list:
            if c.get("name") == "cf_clearance":
                exp = c.get("expires", -1)
                if isinstance(exp, (int, float)) and exp > 0:
                    remaining = int(exp - now)
                    if remaining > 0:
                        return min(remaining, self.default_ttl)
                break
        return self.default_ttl


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


class BackendRouter:
    """按顺序尝试后端；未配置的记 skipped，失败的记 attempts，全败则抛结构化错误。

    纪律：**终止性错误（`TERMINAL_CODES`）立刻上抛，不进入 fallback。**
    换后端只对"这个后端解不出来"有意义；参数写错、没鉴权、队列满时换谁都一样，
    继续试只会放大攻击面与负载。
    """

    def __init__(self, backends: list[SolverBackend]) -> None:
        if not backends:
            raise ValueError("至少需要一个后端")
        self._backends = backends

    @property
    def backends(self) -> list[str]:
        return [b.name() for b in self._backends]

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        attempts: list[dict] = []
        last_error: SolveError | None = None

        for backend in self._backends:
            name = backend.name()
            if not backend.is_configured():
                attempts.append({"backend": name, "skipped": True, "reason": "not-configured"})
                continue
            try:
                out = backend.solve(url, timeout=timeout, force=force, **kwargs)
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
        raise SolveError(
            ErrorCode.NOT_CONFIGURED,
            "没有任何可用后端",
            stage="admit",
            hint="检查 --flaresolverr 配置，以及本地 Camoufox 是否已 fetch",
            extra={"attempts": attempts},
        )
