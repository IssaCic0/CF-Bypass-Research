"""CF 过盾求解服务 —— 本仓库的最终方案实现（路线 B：Camoufox 求解 + HTTP 回放）。

为什么是这个方案：
  · 纯算/纯 JS 引擎路线已实测拿不到 cf_clearance（见 docs/ 报告 §14-16）
  · Camoufox 在 C++ 层篡改指纹，是唯一实测 2/2 通过的路线
  · 求解放进独立进程：崩溃隔离、可单独扩容、Go 网关只做 HTTP 客户端

服务契约（极简，只做一件事）：
  POST /solve   {"url": "https://target/", "timeout": 45}
                → {"ok":true, "cf_clearance":"...", "user_agent":"...",
                   "cookies":{...}, "expires_in":1500, "elapsed":6.2, "cached":false}
                → 失败 {"ok":false, "error":"..."}（HTTP 200，便于客户端统一处理）
  GET  /health  → {"ok":true, "browser":"camoufox", "cached_domains":N, "solves":N}
  GET  /stats   → 命中率 / 平均耗时

设计要点：
  · 浏览器实例**复用**（每次求解只开一个隔离 context），避免每次付 3.4s 启动
  · 同一域名结果**缓存**（cf_clearance 天生按域+IP+UA 绑定，缓存是语义正确的）
  · 求解串行化（Camoufox 单实例，用锁保护），并发靠多进程/多端口横向扩
  · 全程不落盘、不写日志里的 cookie 值

用法：
  pip install camoufox && python -m camoufox fetch
  python service.py --port 8878
  然后：curl -X POST localhost:8878/solve -d '{"url":"https://steamdb.info/"}'
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from camoufox.sync_api import Camoufox

# ── 常量 ───────────────────────────────────────────
DEFAULT_TTL = 1500          # cf_clearance 通常 30 分钟量级，留安全余量
MAX_URL_LEN = 2048


class Solver:
    """单 worker 线程持有 Camoufox，串行求解 + 按域缓存。

    为什么必须单线程：Playwright/Camoufox 的 **sync API 绑定创建它的线程**，
    在 ThreadingHTTPServer 的其他线程里直接调用会报
    "cannot switch to a different thread"。所以浏览器只在 worker 线程里创建与使用，
    HTTP 请求通过队列派发过去。
    """

    def __init__(self, headless: bool = True, ttl: int = DEFAULT_TTL) -> None:
        self.ttl = ttl
        self._headless = headless
        self._cache: dict[str, dict] = {}
        self._solves = 0
        self._hits = 0
        self._total_elapsed = 0.0
        self._lock = threading.Lock()      # 保护缓存与计数
        self._tasks: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._worker, name="cfsolve-worker", daemon=True)
        self._thread.start()

    # -- worker：浏览器只活在这个线程里 ----------------
    def _worker(self) -> None:
        cm = Camoufox(headless=self._headless, geoip=False)
        browser = cm.__enter__()
        try:
            while True:
                task = self._tasks.get()
                if task is None:      # 退出信号
                    return
                fn, box = task
                try:
                    box["result"] = fn(browser)
                except Exception as e:  # noqa: BLE001
                    box["error"] = e
                finally:
                    box["done"].set()
        finally:
            try:
                cm.__exit__(None, None, None)
            except Exception:
                pass

    def _submit(self, fn):
        """把浏览器任务派发到 worker 线程并等待结果。"""
        box = {"done": threading.Event(), "result": None, "error": None}
        self._tasks.put((fn, box))
        if not box["done"].wait(timeout=300):
            raise TimeoutError("worker 无响应（>300s）")
        if box["error"] is not None:
            raise box["error"]
        return box["result"]

    # -- 单次求解（在 worker 线程里执行）--------------
    def _do_solve(self, browser, url: str, timeout: float) -> dict:
        ctx = browser.new_context()
        try:
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            deadline = time.time() + timeout
            cookies: dict[str, str] = {}
            while time.time() < deadline:
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                if "cf_clearance" in cookies:
                    break
                time.sleep(1.0)
            ua = page.evaluate("navigator.userAgent")
            title = ""
            try:
                title = page.title()
            except Exception:
                pass
            return {"cookies": cookies, "ua": ua, "title": title}
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    # -- 对外入口 -----------------------------------
    def solve(self, url: str, timeout: float = 45.0, force: bool = False) -> dict:
        host = _host_of(url)
        if not host:
            return {"ok": False, "error": f"无法解析域名: {url}"}

        with self._lock:
            if not force:
                c = self._cache.get(host)
                if c and c["expires_at"] > time.time():
                    self._hits += 1
                    return {**c, "cached": True, "ok": True}

        t0 = time.monotonic()
        try:
            r = self._submit(lambda b: self._do_solve(b, url, timeout))
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}",
                    "elapsed": round(time.monotonic() - t0, 2)}

        elapsed = round(time.monotonic() - t0, 2)
        with self._lock:
            self._solves += 1
            self._total_elapsed += elapsed

        cookies = r["cookies"]
        if "cf_clearance" not in cookies:
            return {"ok": False, "error": f"{timeout}s 内未取得 cf_clearance",
                    "elapsed": elapsed, "title": r["title"]}

        result = {
            "cf_clearance": cookies["cf_clearance"],
            "user_agent": r["ua"],
            "cookies": cookies,
            "expires_in": self.ttl,
            "expires_at": time.time() + self.ttl,
            "elapsed": elapsed,
            "cached": False,
            "ok": True,
        }
        with self._lock:
            self._cache[host] = result
        return result

    def stats(self) -> dict:
        with self._lock:
            avg = round(self._total_elapsed / self._solves, 2) if self._solves else 0.0
            live = sum(1 for c in self._cache.values() if c["expires_at"] > time.time())
            return {
                "ok": True,
                "browser": "camoufox",
                "cached_domains": live,
                "solves": self._solves,
                "cache_hits": self._hits,
                "avg_elapsed_sec": avg,
            }

    def close(self) -> None:
        self._tasks.put(None)
        self._thread.join(timeout=10)


def _host_of(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return (urlsplit(url).hostname or "").lower()
    except Exception:
        return ""


# ── HTTP 服务 ──────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    solver: Solver  # 由 main 注入

    def log_message(self, fmt, *args):  # 打日志但不打 body（避免凭证入日志）
        print(f"[cfsolve] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True, **self.solver.stats()})
        elif self.path == "/stats":
            self._send(200, self.solver.stats())
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/solve":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 64 * 1024:
                self._send(400, {"ok": False, "error": "bad content-length"})
                return
            req = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            self._send(400, {"ok": False, "error": f"bad json: {e}"})
            return

        url = str(req.get("url") or "")
        timeout = float(req.get("timeout") or 45)
        force = bool(req.get("force") or False)

        # 输入校验：只允许 http(s)，长度受限（防止被当成任意协议跳板）
        if not url.startswith(("http://", "https://")) or len(url) > MAX_URL_LEN:
            self._send(400, {"ok": False, "error": "url 必须是 http(s) 且长度合法"})
            return

        try:
            result = self.solver.solve(url, timeout=timeout, force=force)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            result = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        self._send(200, result)


def main() -> None:
    ap = argparse.ArgumentParser(description="Cloudflare cf_clearance 求解服务（Camoufox）")
    ap.add_argument("--port", type=int, default=8878)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL, help="缓存有效期（秒）")
    ap.add_argument("--headed", action="store_true", help="有头模式（调试用）")
    args = ap.parse_args()

    solver = Solver(headless=not args.headed, ttl=args.ttl)
    Handler.solver = solver

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[cfsolve] 监听 http://{args.host}:{args.port}  (camoufox, ttl={args.ttl}s)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[cfsolve] 退出", flush=True)
    finally:
        solver.close()
        srv.server_close()


if __name__ == "__main__":
    main()
