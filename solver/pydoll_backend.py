"""pydoll 后端：用「零 WebDriver」的 CDP 直连 Chrome 来过盾。

为什么单独做成**后端**而不是"引擎"：
  · 我们已有的引擎抽象（`solver/engines.py`）要求返回 Playwright 兼容的 browser 对象，
    而 pydoll 是**异步且自有 API**（`Chrome` / `Tab` / `expect_cloudflare_turnstile`），
    套进 Playwright 形状会很别扭；
  · 它自带 CF 求解器，一个后端调用就能独立完成"开浏览器 → 过盾 → 交 cookie"，
    正好符合我们后端协议（`solve(url) -> 产物`）的语义。

它相对 Playwright 的优势（也是我们 A/B 要测的）：
  · **零 WebDriver**：不加载 chromedriver，也没有 `navigator.webdriver` 之类的补丁痕迹；
  · 直连 CDP（WebSocket），输入事件走 `Input.dispatch*`，比 Playwright 的合成事件更像真人；
  · 自带 `expect_cloudflare_turnstile()`：内部会找 Turnstile 的 shadow root 并点 checkbox。

⚠️ 关键限制（实测 + 上游 skill 都强调）：
  · **headless 模式下 UA 会带 `HeadlessChrome`**（实测本机 Chrome 156 就是这样），
    这本身就是强检测信号 → 生产上应尽量 `headless=False`（服务器上配 xvfb）。
  · 只能拿 cookie 类产物（cf_clearance / aws-waf-token）；一次性 token 需要额外的 JS 取值逻辑，暂不支持。
"""

from __future__ import annotations

import asyncio
import os
import time

try:  # 包导入
    from .backends import DEFAULT_COOKIE_TTL, BackendId, derive_ttl
    from .errors import ErrorCode, SolveError
except ImportError:  # 直接以脚本运行
    from backends import DEFAULT_COOKIE_TTL, BackendId, derive_ttl  # type: ignore[no-redef]
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]

#: Windows / Linux 上 Chrome 的常见安装位置（找不到时让用户显式传 --chrome-path）
DEFAULT_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def find_chrome(explicit: str = "") -> str:
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    for path in DEFAULT_CHROME_CANDIDATES:
        if os.path.isfile(path):
            return path
    return ""


def _cookie_kv(cookie):
    """pydoll 3.x 的 get_cookies() 返回 dict；老版本返回对象。两种都兼容。"""
    if isinstance(cookie, dict):
        return cookie
    return {
        "name": getattr(cookie, "name", ""),
        "value": getattr(cookie, "value", ""),
        "domain": getattr(cookie, "domain", ""),
        "path": getattr(cookie, "path", "/"),
        "expires": getattr(cookie, "expires", -1),
        "httpOnly": getattr(cookie, "http_only", False),
        "secure": getattr(cookie, "secure", False),
        "sameSite": getattr(cookie, "same_site", ""),
    }


class PydollBackend:
    """用 pydoll（无 WebDriver 的 CDP 客户端）驱动真 Chrome 过盾。"""

    def __init__(
        self,
        chrome_path: str = "",
        *,
        headless: bool = False,
        wait: float = 45.0,
        keep_browser: bool = False,
        egress_id: str = "pydoll",
    ) -> None:
        self.chrome_path = find_chrome(chrome_path)
        self.headless = headless
        self.wait = wait
        self.keep_browser = keep_browser
        # 出口标识：pydoll 在自己的进程里直连（不走代理池），所以它必须是**本机出口**的标识
        self.egress_id = egress_id or "pydoll"

    def name(self) -> str:
        return BackendId.PYDOLL.value

    def is_configured(self) -> bool:
        if not self.chrome_path:
            return False
        try:
            import pydoll  # noqa: F401
        except ImportError:
            return False
        return True

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        want = str(kwargs.get("want") or "clearance").lower()
        if want not in ("clearance", "awswaf"):
            raise SolveError(
                ErrorCode.NOT_CONFIGURED,
                f"pydoll 后端只支持 cookie 类产物（clearance/awswaf），不支持 want={want}",
                stage="admit",
                hint="一次性 token 请用浏览器引擎（--engine camoufox）或纯协议后端",
            )
        if not self.is_configured():
            raise SolveError(
                ErrorCode.NOT_CONFIGURED,
                "pydoll 或 Chrome 不可用",
                stage="admit",
                hint="pip install pydoll-python，并用 --chrome-path 指定 chrome(.exe) 路径",
            )
        wait = min(self.wait, max(5.0, timeout))
        return asyncio.run(self._solve_async(url, want, wait))

    async def _solve_async(self, url: str, want: str, wait: float) -> dict:
        from pydoll.browser import Chrome
        from pydoll.browser.options import ChromiumOptions

        want_cookie = "cf_clearance" if want == "clearance" else "aws-waf-token"
        options = ChromiumOptions()
        options.headless = self.headless
        options.binary_location = self.chrome_path
        options.start_timeout = 60
        options.webrtc_leak_protection = True
        options.add_argument("--window-size=1366,900")
        if os.name != "nt":
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")

        t0 = time.monotonic()
        try:
            async with Chrome(options=options) as browser:
                tab = await browser.start()
                try:
                    async with tab.expect_cloudflare_turnstile(time_to_wait_captcha=wait):
                        await tab.go_to(url, timeout=int(wait * 1000))
                except Exception:  # noqa: BLE001 —— 上下文管理器超时/无需处理都走这里
                    pass

                raw_cookies = await tab.get_cookies()
                cookies: dict[str, str] = {}
                cookie_list: list[dict] = []
                for c in raw_cookies:
                    d = _cookie_kv(c)
                    name = d.get("name", "")
                    if not name:
                        continue
                    cookies[name] = d.get("value", "")
                    cookie_list.append({
                        "name": name,
                        "value": d.get("value", ""),
                        "domain": d.get("domain", ""),
                        "path": d.get("path", "/"),
                        "expires": d.get("expires", -1),
                        "http_only": bool(d.get("httpOnly", False)),
                        "secure": bool(d.get("secure", False)),
                        "same_site": d.get("sameSite", ""),
                    })
                ua = ""
                try:
                    raw = await tab.execute_script("return navigator.userAgent")
                    if isinstance(raw, dict):
                        ua = (((raw.get("result") or {}).get("result") or {}).get("value")) or ""
                except Exception:  # noqa: BLE001
                    ua = ""
        except SolveError:
            raise
        except Exception as e:  # noqa: BLE001 —— 启动失败/崩溃
            raise SolveError(
                ErrorCode.BROWSER_UNAVAILABLE,
                f"pydoll 启动 Chrome 失败：{type(e).__name__}: {str(e)[:150]}",
                stage="admit",
                hint="确认 --chrome-path 正确、Chrome 未被占用；服务器上可加 xvfb 并用非 headless",
            ) from e

        elapsed = round(time.monotonic() - t0, 2)
        if not cookies.get(want_cookie):
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"{elapsed}s 内未取得 {want_cookie}（pydoll）",
                stage="wait",
                hint=(
                    "headless 模式下 Chrome 的 UA 会带 HeadlessChrome，容易被判为自动化；"
                    "换成 headless=False（服务器配 xvfb）通常显著提升成功率"
                ),
            )

        # 统一算 TTL（禁止各后端自己拍 0）：0 会让 Go 侧立刻判过期、服务端缓存直接删，
        # 等于把缓存整个关掉 —— 每次回放都要重新开一次 Chrome。
        ttl = derive_ttl(cookie_list, want_cookie, default=DEFAULT_COOKIE_TTL)
        if ttl <= 0:
            raise SolveError(
                ErrorCode.UNSOLVED,
                f"pydoll 拿到的 {want_cookie} 已过期或剩余寿命过短，不返回（否则客户端只会拿到静默 403）",
                stage="wait",
                hint="检查本机时钟；持续出现说明该出口 IP 或 UA 已被提分",
            )

        return {
            "ok": True,
            "solved": True,
            "want": want,
            "cf_clearance" if want == "clearance" else "aws_waf_token": cookies[want_cookie],
            "user_agent": ua,
            "cookies": cookies,
            "cookie_list": cookie_list,
            "expires_in": ttl,
            "expires_at": time.time() + ttl,
            "elapsed": elapsed,
            "cached": False,
            "cacheable": True,
            "egress_id": self.egress_id,
            "backend": self.name(),
        }
