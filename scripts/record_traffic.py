"""真实浏览器会话流量录制（v6.3，参考 mitmproxy 的 addon 架构）。

要解决的缺口：
  `scripts/capture_sample.py` 用 **urllib** 抓样 —— 它能拿到挑战页 HTML 和脚本正文，
  但**拿不到真实浏览器会话的任何特征**：header 顺序与真实值、JS 设的 cookie、
  子资源时序、跳转链、状态码序列、资源类型分布。而"CF 到底在什么时候、按什么顺序、
  下发哪个 cookie"恰恰是这套项目最需要的第一手材料。

  `scripts/diag_challenge.py` 用了 Playwright，但它只做**诊断**（打印 iframe/截图），
  不落盘流量。所以"手工 curl 抓样"这个洞其实只补了一半：从手工变成了脚本化 urllib。

mitmproxy 的答案是"在流量路径上挂可编程 addon"。我们不需要装它 ——
Playwright 的 `page.on("request"/"response")` 就是同一件事，且**零新依赖**。

## 三条硬约束（对应 policy.md）

1. `--dry-run` 是默认行为：不传 `--real` 只打印"会去哪儿、会录什么"，零网络；
2. 请求预算：一次会话最多 `--max-requests` 个记录（默认 60），超过硬上限拒绝 ——
   挑战页会引几十个子资源，但真机预算不可再生；
3. **凭证永不落盘**：header 走白名单、`Cookie`/`Authorization` 只记"有没有"、
   `Set-Cookie` 只记**名字与个数**（值一律不写）。这条是 policy §4 的硬要求。

## 产物

    <out>/traffic.jsonl   一行一个请求/响应配对（新的在后，便于 diff 与 grep）
    <out>/summary.json    汇总：按类型计数、状态码分布、时序、detect 结论、cookie 名清单
    <out>/manifest.json   元数据（URL/UA/时间/预算消耗/是否截断）

## 用法

    python scripts/record_traffic.py --url https://target/ --out samples/x            # dry-run
    python scripts/record_traffic.py --url https://target/ --out samples/x --real
    python scripts/record_traffic.py --url https://target/ --out samples/x --real \
        --engine camoufox --max-requests 80      # 用项目的求解引擎（最接近求解时的真实指纹）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MAX_REQUESTS = 60
HARD_BUDGET = 200

#: 允许落盘的**响应头**。与 capture_sample 同一套口径，别两处漂移。
KEEP_RESPONSE_HEADERS = frozenset({
    "content-type", "content-length", "content-encoding", "cache-control",
    "location", "server", "cf-ray", "cf-cache-status", "cf-mitigated",
    "strict-transport-security", "x-frame-options", "access-control-allow-origin",
})

#: 请求头同理。**刻意不含** cookie / authorization / x-api-key —— 那是凭证。
KEEP_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "accept-encoding", "content-type",
    "referer", "origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "user-agent",
})

#: 只记"有没有"、绝不记值的头。
PRESENCE_ONLY_HEADERS = frozenset({"cookie", "authorization", "x-api-key", "proxy-authorization"})


def redact_url(url: str) -> str:
    """抹掉 URL 里的 userinfo 口令：`scheme://u:p@host` → `scheme://u:***@host`（纯函数）。

    `--url https://user:pass@host/` 是**公开参数**，而记录里的 `url` 字段是逐条落盘的 ——
    这是本模块"凭证永不落盘"承诺上唯一的缝。口令抹掉后 `urlsplit(...).hostname`
    照常工作，`by_host` 汇总不受影响。
    **查询串一律不动**：挑战页的 `__cf_chl_*` 等参数正是要看的东西。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if "@" not in parts.netloc:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user = userinfo.split(":", 1)[0]
    return parts._replace(netloc=f"{user}:***@{hostport}").geturl()


#: 抹 `scheme://user:pass@host` 里的口令。用于**非 URL 文本**（异常消息里会内嵌 URL）。
_URL_CRED_RE = re.compile(r"(://[^:/@\s\"']+):[^@\s\"']*@")


def redact_any(text: str, url: str = "") -> str:
    """抹掉一段自由文本里的 URL 口令（纯函数）。

    为什么除了 `redact_url` 还需要它：Playwright 的异常消息形如
    `Page.goto: Timeout 30000ms exceeded. Call log: - navigating to "<url>", ...` ——
    是个**内嵌了 URL 的自由文本**，`redact_url()` 解析不了它。而"挑战页导航超时"
    恰恰是本工具最常走的分支，不处理就等于在主干路上漏口令。
    """
    out = str(text)
    if url:
        out = out.replace(url, redact_url(url))
    return _URL_CRED_RE.sub(r"\1:***@", out)


def sanitize_headers(raw, keep: frozenset[str]) -> dict:
    """把一坨 header 收敛成"白名单 + 凭证只记存在性"（纯函数，可离线测）。

    `raw` 可以是 playwright 的 dict，也可以是 list[dict]（`{name,value}` 形态）。
    大小写不敏感；值截断到 200 字符（防止某个畸形头把 jsonl 撑爆）。
    """
    items: list[tuple[str, str]] = []
    if isinstance(raw, dict):
        items = [(str(k), str(v)) for k, v in raw.items()]
    elif isinstance(raw, (list, tuple)):
        for row in raw:
            if isinstance(row, dict) and "name" in row:
                items.append((str(row.get("name", "")), str(row.get("value", ""))))
    picked: dict = {}
    for name, value in items:
        lowered = name.lower()
        if lowered in PRESENCE_ONLY_HEADERS:
            picked[lowered + "-present"] = "yes"
            continue
        if lowered in keep:
            picked[lowered] = value[:200]
    return picked


def sanitize_set_cookie(raw) -> dict:
    """`Set-Cookie` 只记**名字与个数**，值一律不写（纯函数）。

    为什么要单独一个函数而不是复用 sanitize_headers：`set-cookie` 在响应里是
    **多条同名**的（一个响应可以种好几个 cookie），dict 形态会把它们压成一条，
    而"这个挑战种了哪几个 cookie"正是要观察的东西。
    """
    rows = raw if isinstance(raw, (list, tuple)) else []
    names: list[str] = []
    for row in rows:
        text = ""
        if isinstance(row, dict):
            # ⚠️ `headers_array()` 返回的是**全部** header，不是只有 set-cookie。
            # 第一版漏了这个判断，把 content-length/date/server 全当 cookie 名解析出来
            # （实测拿到 ['28','text/html','Thu, 05 Nov...','BaseHTTP/0.6'] 这种噪声）。
            if str(row.get("name", "")).lower() != "set-cookie":
                continue
            text = str(row.get("value", ""))
        elif isinstance(row, str):
            text = row
        first = text.split(";", 1)[0].strip()
        name = first.split("=", 1)[0].strip()
        if name and name not in names:
            names.append(name)
    return {"set_cookie_names": names, "set_cookie_count": len(names)}


def _resource_kind(request) -> str:
    try:
        return str(request.resource_type or "other")
    except Exception:  # noqa: BLE001
        return "other"


def summarize(records: list[dict]) -> dict:
    """从 jsonl 记录里算出汇总（纯函数，可离线测）。"""
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    hosts: dict[str, int] = {}
    set_cookie_names: list[str] = []
    total_bytes = 0
    first_ts = last_ts = None
    failures: list[str] = []
    for rec in records:
        by_type[rec.get("type") or "other"] = by_type.get(rec.get("type") or "other", 0) + 1
        status = rec.get("status")
        key = str(status) if status is not None else "failed"
        by_status[key] = by_status.get(key, 0) + 1
        try:
            host = urlsplit(str(rec.get("url") or "")).hostname or ""
        except Exception:  # noqa: BLE001
            host = ""
        if host:
            hosts[host] = hosts.get(host, 0) + 1
        for name in rec.get("set_cookie_names") or []:
            if name not in set_cookie_names:
                set_cookie_names.append(name)
        if isinstance(rec.get("body_bytes"), int):
            total_bytes += rec["body_bytes"]
        ts = rec.get("ts")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if rec.get("error"):
            failures.append(f"{rec.get('url')}: {rec['error']}")
    return {
        "requests": len(records),
        "by_resource_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
        "by_status": dict(sorted(by_status.items(), key=lambda kv: -kv[1])),
        "by_host": dict(sorted(hosts.items(), key=lambda kv: -kv[1])),
        "set_cookie_names": set_cookie_names,
        "total_body_bytes": total_bytes,
        "span_sec": round((last_ts - first_ts), 3) if first_ts and last_ts else 0.0,
        "failures": failures[:20],
    }


def record(url: str, out_dir: Path, *, engine: str, ua: str, timeout: float,
           max_requests: int, wait_after: float) -> dict:
    """真跑一次会话并落盘流量。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    truncated = False

    with _open_browser(engine) as (_playwright, browser):
        # ⚠️ **不传 user_agent 时不传**，别硬塞一个。camoufox 的身份由 persona 决定，
        # 求解侧也是 `browser.new_context(proxy=proxy)` 不加 UA（`service.py`），
        # 并且缓存键的第三维取的是 `navigator.userAgent` 的**实测值**。
        # 强行塞一个 `Windows NT 10.0 / Firefox 156` 会让这一档"与求解时同一套指纹"
        # 变成假的：要么覆盖了 persona（录到的会话不代表求解），要么被 persona 覆盖
        # （manifest 记了个从没发出去过的 UA）。
        ctx_kw = {"locale": "en-US", "viewport": {"width": 1440, "height": 900}}
        if ua:
            ctx_kw["user_agent"] = ua
        ctx = browser.new_context(**ctx_kw)
        page = ctx.new_page()
        pending: dict = {}
        accepted = 0

        def on_request(req):
            nonlocal accepted, truncated
            # ⚠️ 预算是**单调计数器**，不是派生量。两版都栽在这上面：
            #   ① v1 只数 `len(records)`（已完成），于是并发发起的子资源全部放行 ——
            #      实测 `--max-requests 2` 录到 5 条。
            #   ② v2 改成 `len(records) + len(pending)`，看着对了，仍录到 3 条。
            #      真因是**重入**：`resp.body()` 是同步 API 的阻塞调用，会切 greenlet，
            #      把排队中的 request 事件在"本条响应还没入账"的中间隙派发进来 ——
            #      那一刻 records 与 pending 都偏小，配额就被多放了。
            # 计数器在**放行瞬间**自增，与任何列表状态无关，重入也打不歪。
            if accepted >= max_requests:
                truncated = True
                return
            accepted += 1
            pending[id(req)] = {
                "ts": round(time.time(), 3),
                "type": _resource_kind(req),
                "method": req.method,
                "url": redact_url(req.url),
                "request_headers": sanitize_headers(req.headers, KEEP_REQUEST_HEADERS),
            }

        def on_response(resp):
            rec = pending.pop(id(resp.request), None)
            if rec is None:          # 超过预算的请求不补记（预算就是预算）
                return
            # ⚠️ **先入账，再取任何可能切 greenlet 的东西**（`headers_array()` / `body()`
            # 都是方法调用，会让排队的 request 事件插进来）。入账后所有补充都是往
            # 同一个 dict 里填字段，重入也安全。
            records.append(rec)
            rec["status"] = resp.status
            # ⚠️ `headers_array` 是**方法**（Playwright 的同步 API），必须调用。
            # 写成属性访问会拿到一个 bound method，于是 Set-Cookie 一个都收不到 ——
            # 而这正是"挑战种了哪几个 cookie"的唯一来源（`resp.headers` 里根本没有
            # set-cookie，多个同名头被折叠掉了）。实测踩到过。
            try:
                raw_headers = resp.headers_array() if callable(
                    getattr(resp, "headers_array", None)) else []
            except Exception:  # noqa: BLE001
                raw_headers = []
            rec["response_headers"] = sanitize_headers(resp.headers, KEEP_RESPONSE_HEADERS)
            rec.update(sanitize_set_cookie(raw_headers))
            try:                      # body 只量大小，不落盘（挑战脚本体积大且已有专门采集）
                body = resp.body()
                rec["body_bytes"] = len(body)
            except Exception:  # noqa: BLE001
                rec["body_bytes"] = None

        def on_failed(req):
            rec = pending.pop(id(req), None)
            if rec is None:
                return
            rec.update({"status": None,
                        "error": redact_any(str(req.failure or "failed")[:160], url)})
            records.append(rec)

        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_failed)

        nav_error = ""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        except Exception as e:  # noqa: BLE001 —— 挑战页常常不"加载完成"，这不是失败
            # ⚠️ 必须先脱敏再截断：Playwright 的超时消息里**内嵌完整 URL**
            # （`... - navigating to "<url>", waiting until ...`），
            # 而"挑战页导航超时"正是本工具最常走的分支 —— 不处理就等于在主干路上漏口令。
            nav_error = redact_any(f"{type(e).__name__}: {str(e)}", url)[:160]
        # 实测 UA 是唯一可信的记录：camoufox 的身份由 persona 决定，用户传的 `--ua`
        # 未必是真正发出去的那个值（见 main() 里的说明）。
        observed_ua = ""
        try:
            observed_ua = str(page.evaluate("navigator.userAgent") or "")
        except Exception:  # noqa: BLE001 —— 拿不到就退化成用户给的值，不中断
            observed_ua = ""
        if wait_after > 0:
            page.wait_for_timeout(int(wait_after * 1000))

        ctx.close()

    jsonl = out_dir / "traffic.jsonl"
    with jsonl.open("w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = summarize(records)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "url": redact_url(url), "engine": engine,
        # `user_agent` 一律是**实测**值：camoufox 的身份由 persona 决定，把 `--ua`
        # 当作"发出去的 UA"记录会让产物与 manifest 互相矛盾。
        "user_agent": observed_ua or ua, "user_agent_override": ua,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "requests_recorded": len(records), "max_requests": max_requests,
        "truncated": truncated, "nav_error": nav_error,
        "artifacts": ["traffic.jsonl", "summary.json", "manifest.json"],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


class _open_browser:
    """上下文管理器：按引擎起浏览器（camoufox = 与求解时同一套指纹；chromium = 快）。"""

    def __init__(self, engine: str) -> None:
        self.engine = engine
        self._pw = None
        self._browser = None
        self._cm = None

    def __enter__(self):
        if self.engine == "camoufox":
            try:
                from camoufox.sync_api import Camoufox
            except ImportError as e:
                raise SystemExit(
                    "engine=camoufox 需要 camoufox（pip install camoufox && python -m camoufox fetch）。"
                    "或者用 --engine chromium（Playwright 自带）。"
                ) from e
            self._cm = Camoufox(headless=True, geoip=False)
            self._browser = self._cm.__enter__()
            self._pw = None
            return None, self._browser
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        return self._pw, self._browser

    def __exit__(self, *exc):
        try:
            if self._cm is not None:
                self._cm.__exit__(None, None, None)
            elif self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="真实浏览器会话流量录制（默认 dry-run）")
    ap.add_argument("--url", required=True, help="目标 URL")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--ua", default="",
                    help="覆盖 User-Agent（默认**不覆盖**：camoufox 的身份由 persona 决定，"
                         "与求解时保持一致；chromium 则用 Playwright 自带的真实 UA）")
    ap.add_argument("--engine", default="camoufox", choices=["camoufox", "chromium"],
                    help="camoufox=与求解时同一套指纹（默认）；chromium=Playwright 自带，起得快")
    ap.add_argument("--timeout", type=float, default=30.0, help="导航超时（秒）")
    ap.add_argument("--wait-after", type=float, default=3.0,
                    help="导航后再观察几秒（JS 挑战的 cookie 往往是延迟下发的）")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                    help=f"最多记录多少个请求（默认 {DEFAULT_MAX_REQUESTS}，硬上限 {HARD_BUDGET}）")
    ap.add_argument("--real", action="store_true", help="真的发请求（不加就是 dry-run）")
    ap.add_argument("--allow-over-budget", action="store_true",
                    help=f"允许超过硬上限 {HARD_BUDGET}")
    args = ap.parse_args(argv)

    if args.max_requests < 1:
        print("--max-requests 必须 ≥ 1", file=sys.stderr)
        return 2
    if args.max_requests > HARD_BUDGET and not args.allow_over_budget:
        print(f"拒绝：--max-requests {args.max_requests} 超过硬上限 {HARD_BUDGET}；"
              f"要突破请显式加 --allow-over-budget。", file=sys.stderr)
        return 2

    if not args.real:
        print("[dry-run] 不会发出任何网络请求。真跑请加 --real。")
        print(f"[dry-run] 目标：{redact_url(args.url)}")
        print(f"[dry-run] 引擎：{args.engine}（camoufox = 与求解时同一套指纹）")
        print(f"[dry-run] UA：{args.ua or '不覆盖（camoufox 用 persona，chromium 用自带 UA）'}")
        print(f"[dry-run] 预算：最多记录 {args.max_requests} 个请求/响应配对")
        print(f"[dry-run] 时序：导航（{args.timeout:.0f}s 超时）→ 再观察 {args.wait_after:.0f}s")
        print("[dry-run] 会录：请求/响应头（白名单）、状态码、资源类型、"
              "Set-Cookie 名（**不记值**）、body 大小、失败原因")
        print(f"[dry-run] 预计产物：{args.out}/traffic.jsonl + summary.json + manifest.json")
        return 0

    out = Path(args.out)
    started = time.monotonic()
    manifest = record(args.url, out, engine=args.engine, ua=args.ua, timeout=args.timeout,
                      max_requests=args.max_requests, wait_after=args.wait_after)
    elapsed = round(time.monotonic() - started, 2)
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    print(f"完成：{out}（{elapsed}s，{manifest['requests_recorded']} 个请求"
          f"{'，已截断' if manifest['truncated'] else ''}）")
    print(f"  资源类型：{summary['by_resource_type']}")
    print(f"  状态码  ：{summary['by_status']}")
    print(f"  Set-Cookie 名：{summary['set_cookie_names']}")
    if manifest["nav_error"]:
        print(f"  导航异常（挑战页常见，不算失败）：{manifest['nav_error'][:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())