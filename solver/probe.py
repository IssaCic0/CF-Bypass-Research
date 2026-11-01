"""探测目标站的防护类型（`/detect` 与 `route=auto` 共用）。

从 `service.py` 搬出来（只搬不改，v4.4）：service.py 已经 2.4k 行，
探测这条链路是**独立能力**（只发一次普通 GET、不碰浏览器/缓存/队列），
放在主服务文件里既难读也不好单独测。

三条不能丢的语义（原实现里都有注释，这里保留）：
  · **逐跳**校验重定向 —— urllib 默认自动跟随且跟随过程中不再回看守卫，
    一个 302 就能把内网地址塞进来（`/solve` 早就修过这个洞，`/detect` 曾漏掉）；
  · 只跟随 http/https，其它协议（`file:`/`gopher:`/`ftp:`）直接拒绝；
  · 跳数上限 `PROBE_MAX_REDIRECTS`，防止重定向环把线程挂死。
"""

from __future__ import annotations

import json  # noqa: F401  （原实现里由 service.py 提供，搬过来要保持同名导入）
from urllib.parse import urljoin, urlsplit

try:  # 包导入
    from .errors import ErrorCode, SolveError
    from .detect import summarize as detect_summarize
except ImportError:  # 直接以脚本运行
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]
    from detect import summarize as detect_summarize  # type: ignore[no-redef]

#: `/detect` 跟随重定向的跳数上限
PROBE_MAX_REDIRECTS = 3

#: 探测用 UA 的 persona 兜底（浏览器还没起来时用；起来了就用它的真实 UA）
_PROBE_UA_BY_OS = {
    "windows": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
    "macos": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:156.0) Gecko/20100101 Firefox/156.0",
    "linux": "Mozilla/5.0 (X11; Linux x86_64; rv:156.0) Gecko/20100101 Firefox/156.0",
}
DEFAULT_PROBE_UA = _PROBE_UA_BY_OS["windows"]


def probe_protection(url: str, timeout: float = 15.0, *, guard=None, user_agent: str = "") -> dict:
    """发一次普通 GET（**逐跳**校验重定向），把状态码/头/cookie/body 交给探测器。

    `guard(url)` 是 SSRF 守卫，**每一跳都要调一次**。为什么必须逐跳：
    `urllib.request.urlopen` 默认自动跟随最多 10 次重定向，且**跟随过程中不再回看守卫**
    —— 一个 302 就能把内部地址塞进来。这是 `/solve` 早就修过（见
    `_install_request_guard`）、`/detect` 却漏掉的同一个洞。

    这里改成"自己跟随、自己校验"：关闭 urllib 的自动跟随，按 `Location` 逐跳解析，
    每一跳先问守卫，再发下一跳。跳数上限 `PROBE_MAX_REDIRECTS`，防止重定向环挂死线程。
    """
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        """不让 urllib 自己跟：重定向由下面的循环逐跳校验后再发。"""

        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    request_headers = {
        # UA 必须与求解侧是同一个身份：用另一套身份探出来的"有没有盾/什么盾"，
        # 与真实求解时看到的不一定一致（CF 的判定就吃这一层）。
        "User-Agent": user_agent or DEFAULT_PROBE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    status = 0
    headers: dict = {}
    body = ""
    final_url = url
    current = url
    hops = 0

    while True:
        if guard is not None:
            guard(current)                 # 每一跳（含首跳）都过守卫
        try:
            with opener.open(urllib.request.Request(current, headers=request_headers),
                             timeout=timeout) as response:
                status = response.status
                headers = dict(response.headers.items())
                body = response.read(500_000).decode("utf-8", "replace")
                final_url = response.geturl() or current
            break
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                location = (e.headers.get("Location") if e.headers else "") or ""
                try:
                    e.close()
                except Exception:  # noqa: BLE001
                    pass
                if not location:
                    status = e.code
                    headers = dict(e.headers.items()) if e.headers else {}
                    body = ""
                    final_url = current
                    break
                hops += 1
                if hops > PROBE_MAX_REDIRECTS:
                    raise SolveError(
                        ErrorCode.TRANSPORT,
                        f"探测重定向超过 {PROBE_MAX_REDIRECTS} 跳，已中止",
                        stage="navigate",
                        hint="目标可能在重定向环里；如需探测最终页，请直接用最终 URL",
                    ) from e
                current = urljoin(current, location)
                continue
            status = e.code
            headers = dict(e.headers.items()) if e.headers else {}
            try:
                body = e.read(500_000).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
            final_url = current
            break
        except SolveError:
            raise
        except Exception as e:  # noqa: BLE001
            raise SolveError(
                ErrorCode.TRANSPORT, f"探测失败：{type(e).__name__}: {str(e)[:120]}", stage="navigate",
                hint="检查目标可达性；探测只发一次普通 GET，不需要浏览器",
            ) from e

    cookies = {}
    # urllib 会把多条 Set-Cookie 合并，这里按分号粗切出 cookie 名即可（只用于识别）
    set_cookie = headers.get("Set-Cookie") or ""
    for part in set_cookie.split(","):
        name = part.split("=", 1)[0].strip()
        if name and " " not in name:
            cookies[name] = "1"

    result = detect_summarize(url=url, status=status, headers=headers, body=body, cookies=cookies)
    result["final_url"] = final_url
    result["backend"] = "detect"
    return result
