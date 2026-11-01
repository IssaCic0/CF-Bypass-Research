"""本机真进程 E2E 验收（**离线可跑**，不需要任何真实目标站）。

为什么需要它（而不是继续扩 `run.ps1 smoke`）：

  `run.ps1 smoke` 只覆盖 `/health` + `/detect` + SSRF 守卫三项，且用 `--no-auth`；
  而 `tests/` 里的用例是 **in-process**（同一个解释器里起 ThreadingHTTPServer），
  它们证明得了逻辑，证明不了这条**交付链路**：

      按文档起进程 → 读 .env 里的 token → 带 Bearer 调用 → 审计落盘 → /shutdown → 退出码 0

  这条链路上真正会咬人的恰恰是 in-process 用例**天然跳过**的部分：
  token 文件的生成与读取、401 的响应体、`/metrics` 的 content-type、
  202 之后进程**真的**退出、退出码是不是 0、审计文件里有没有混进凭证。

设计取舍（为什么是两段式）：

  SSRF 守卫与"打本机回环目标"互斥 —— 前者要拦 127.0.0.1，后者要放行。
  用一个 `--allow-private-hosts` 的服务同时测两件事，就只能靠"守卫被关掉时什么行为"
  来蒙混，测不出真拦。所以起两次进程：

    段 A（严格模式）：401 / `/metrics` / 守卫拦截 / 畸形请求 / 404 / `/shutdown` 退出码
    段 B（--allow-private-hosts + --audit-log）：本地静态页 → `/detect` → `route=auto`
                                                     → `fixed` 真跑浏览器 → 审计日志

段 B 的最后一项会真的拉起 camoufox 访问 `http://127.0.0.1:<port>/`：
它不是"mock 浏览器"，是本项目最贵的那条路径的**真跑**（找不到 clearance 会返回结构化失败）。
用 `timeout` 把它压到 5s 内，避免验收脚本变成 20s 的空等。

用法：
    python scripts/e2e_local.py                          # 全量，离线
    python scripts/e2e_local.py --json-out evidence/e2e-local.json
    python scripts/e2e_local.py --skip-browser           # 跳过段 B 的真浏览器项（CI 无浏览器时）
    python scripts/e2e_local.py --case timeout-filter    # 只跑名字包含它的用例

退出码：0 = 全部通过；1 = 有失败；2 = 环境问题（起不来服务）。
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_LINE_PREFIX = "CF_SOLVE_TOKEN="
TOKEN_ENV = "CF_SOLVE_TOKEN"

# Windows 控制台默认 GBK：直接 print 非 ASCII 符号会 UnicodeEncodeError 打断整个验收。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RESULTS: list[dict] = []
# `--case` 只影响**记录**，不影响执行：两段流程本身不方便按用例拆开，
# 而"跑全量、只打印关心的几条"在调试时够用，也不会因为过滤而漏掉前置步骤。
CASE_FILTER = ""


def record(name: str, ok: bool, detail: str = "") -> bool:
    if CASE_FILTER and CASE_FILTER not in name:
        return bool(ok)
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def request(
    url: str,
    *,
    token: str = "",
    method: str = "GET",
    payload: dict | None = None,
    raw_body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, dict, bytes]:
    """返回 (status, headers, body)。4xx/5xx 不抛异常 —— 验收要断言的就是状态码本身。"""
    data = None
    headers = {}
    if raw_body is not None:
        data = raw_body
        headers["Content-Type"] = "application/json"
    elif payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


def json_or_empty(body: bytes) -> dict:
    try:
        v = json.loads(body or b"{}")
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _traceback_excerpt(log: str) -> str:
    """日志里没有 Traceback 就给个行数；有就把**第一段** traceback 摘出来。"""
    if "Traceback" not in log:
        return f"日志行数={len(log.splitlines())}"
    lines = log.splitlines()
    for i, ln in enumerate(lines):
        if "Traceback" in ln:
            return " | ".join(x.strip() for x in lines[i:i + 12] if x.strip())[:600]
    return "Traceback（定位失败）"


# --------------------------------------------------------------------------- 进程


class Service:
    """按文档的方式起 `solver/service.py`（真进程，真 .env，真 stdout）。"""

    def __init__(self, extra_args: list[str], workdir: Path, name: str) -> None:
        self.port = free_port()
        self.workdir = workdir
        self.name = name
        self.token_file = workdir / ".env"
        self.cache_dir = workdir / "cache"
        self.log_path = workdir / f"{name}.log"
        self._log_fp = open(self.log_path, "wb")
        args = [
            sys.executable, "solver/service.py",
            "--port", str(self.port),
            "--cache-dir", str(self.cache_dir),
            "--token-file", str(self.token_file),
            *extra_args,
        ]
        self.proc = subprocess.Popen(
            args, cwd=str(ROOT), stdout=self._log_fp, stderr=subprocess.STDOUT,
        )
        self.token = ""

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def wait_ready(self, timeout: float = 180.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return False
            try:
                status, _h, body = request(f"{self.base}/health", timeout=3)
                if status == 200 and json_or_empty(body).get("state") == "ready":
                    self.token = self._read_token()
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        return False

    def _read_token(self) -> str:
        if not self.token_file.exists():
            return ""
        for line in self.token_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(TOKEN_LINE_PREFIX):
                return line[len(TOKEN_LINE_PREFIX):].strip()
        return ""

    def health(self, with_token: bool = True) -> dict:
        _s, _h, body = request(f"{self.base}/health", token=self.token if with_token else "")
        return json_or_empty(body)

    def shutdown(self, timeout: float = 40.0) -> tuple[int | None, str]:
        status, _h, _b = request(f"{self.base}/shutdown", token=self.token, method="POST", payload={})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return self.proc.returncode, str(status)
            time.sleep(0.2)
        return None, str(status)

    def log_text(self) -> str:
        """服务日志。stdout 被重定向时 Windows 上 Python 用 locale 编码（GBK）→ 先 UTF-8 再 GBK 兜底。

        不这么做，中文日志读出来是乱码，而乱码会让"日志里有没有 Traceback"这类断言
        看起来像在通过（Traceback 是 ASCII 所以仍能命中，但排障时看不到任何上下文）。
        """
        try:
            self._log_fp.flush()
            raw = self.log_path.read_bytes()
        except Exception:  # noqa: BLE001
            return ""
        for enc in ("utf-8", "gbk"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def stop(self, wait: float = 20.0) -> None:
        """尽力优雅收尾，最后才硬杀。

        为什么不能直接 kill：服务下面挂着整个浏览器进程树（几百 MB）。硬杀只杀父进程，
        浏览器可能变孤儿 —— 脚本多跑几次，这台机器就悄悄少掉几个 G。
        先试 `/shutdown`（它会连浏览器一起关），拿不到响应再退化到 kill。
        """
        try:
            if self.proc.poll() is None:
                self.shutdown(timeout=wait)
        except Exception:  # noqa: BLE001 —— 收尾失败不该盖住真正的断言结果
            pass
        finally:
            self.kill()

    def kill(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._log_fp.close()
        except Exception:  # noqa: BLE001
            pass


class StaticSite:
    """一个"什么盾都没有"的本地站点 —— 用来让 `/detect` 判 direct、`route=auto` 判 skip。"""

    BODY = b"<html><head><title>e2e</title></head><body>hello from e2e</body></html>"

    def __init__(self) -> None:
        body = self.BODY

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):  # 静音
                pass

        self.port = free_port()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.srv.shutdown()
        self.srv.server_close()
        return False


# --------------------------------------------------------------------------- 两段验收


def phase_a(workdir: Path) -> None:
    print("\n=== 段 A：严格模式（守卫开、鉴权开） ===", flush=True)
    svc = Service([], workdir, "strict")
    try:
        if not record("A01_service_ready", svc.wait_ready(),
                      f"port={svc.port} token_file={'有' if svc.token else '无'}"):
            record("A01b_startup_log", False, svc.log_text()[-800:])
            return

        # 只写长度：证据文件要能入库，绝不能把 token 原文（哪怕前缀）带进去。
        record("A02_token_file_created", bool(svc.token), f"token 长度={len(svc.token)}")

        # /health 免鉴权：只给最小视图。带 token 才给运维全量视图 —— 这是**防泄露**的契约。
        status, _h, body = request(f"{svc.base}/health")
        b = json_or_empty(body)
        record("A03_health_unauth_200", status == 200 and b.get("state") == "ready",
               f"status={status} state={b.get('state')}")
        leaked = sorted({"cache_backend", "last_error", "proxies", "ua_fingerprint",
                         "cached_domains", "backends"} & set(b))
        record("A04_health_unauth_no_internal_fields", not leaked,
               "泄露字段=" + (",".join(leaked) if leaked else "无"))

        h = svc.health()
        record("A05_health_auth_full_view", h.get("cache_backend", "").startswith("sqlite"),
               f"cache={h.get('cache_backend')} solves={h.get('solves')}")

        # 401 必须是**结构化 JSON**，不是空体/断连 —— 文档承诺了 error_code。
        status, _h, body = request(f"{svc.base}/metrics")
        j = json_or_empty(body)
        record("A06_metrics_without_token_401",
               status == 401 and j.get("error_code") == "unauthorized",
               f"status={status} error_code={j.get('error_code')}")

        status, headers, body = request(f"{svc.base}/metrics", token=svc.token)
        text = body.decode("utf-8", "replace")
        record("A07_metrics_with_token",
               status == 200 and "text/plain" in (headers.get("Content-Type") or "")
               and ("cfsolve_" in text),
               f"status={status} type={(headers.get('Content-Type') or '')[:24]} bytes={len(body)}")

        # SSRF 守卫：内网 URL 必须在**选后端之前**被拒（N-01 回归）。
        status, _h, body = request(f"{svc.base}/detect?url={svc.base}/health", token=svc.token)
        j = json_or_empty(body)
        record("A08_detect_blocks_internal_url",
               status == 400 and j.get("error_code") == "invalid-request",
               f"status={status} error_code={j.get('error_code')}")

        status, _h, body = request(
            f"{svc.base}/solve", token=svc.token, method="POST",
            payload={"url": "http://169.254.169.254/latest/meta-data/"},
        )
        j = json_or_empty(body)
        record("A09_solve_blocks_metadata_ip",
               status == 400 and j.get("error_code") == "invalid-request",
               f"status={status} error_code={j.get('error_code')}")

        status, _h, body = request(f"{svc.base}/solve", token=svc.token, method="POST", payload={})
        record("A10_solve_missing_url_400",
               status == 400 and json_or_empty(body).get("error_code") == "invalid-request",
               f"status={status}")

        status, _h, body = request(f"{svc.base}/solve", token=svc.token, method="POST",
                                raw_body=b"{not json")
        record("A11_solve_malformed_json_400",
               status == 400 and json_or_empty(body).get("error_code") == "invalid-request",
               f"status={status}")

        status, _h, body = request(f"{svc.base}/solve", token=svc.token, method="POST",
                                payload={"url": "https://example.com/", "route": "nonsense"})
        record("A12_solve_bad_route_400",
               status == 400 and json_or_empty(body).get("error_code") == "invalid-request",
               f"status={status}")

        status, _h, body = request(f"{svc.base}/nope", token=svc.token)
        record("A13_unknown_path_404",
               status == 404 and json_or_empty(body).get("error_code") == "not-found",
               f"status={status}")

        # /shutdown：202 + 真退出 + 退出码 0（这是"滚动重启不掉请求"的地基）。
        code, http_status = svc.shutdown()
        record("A14_shutdown_202", http_status == "202", f"http={http_status}")
        record("A15_shutdown_exit_code_0", code == 0, f"exit={code}")

        log = svc.log_text()
        # 真进程日志里出现 Traceback = 有异常没被接住（服务仍在跑，但已经"静默报错"）。
        # 必须抓现场：直接失败的用例不给出那几行，等于让人再跑一次 60s 去复现。
        record("A16_no_traceback_in_log", "Traceback" not in log, _traceback_excerpt(log))
    finally:
        svc.stop()


def phase_b(workdir: Path, skip_browser: bool) -> None:
    print("\n=== 段 B：allow-private-hosts + 审计日志 ===", flush=True)
    audit_path = workdir / "audit.jsonl"
    svc = Service(["--allow-private-hosts", "--audit-log", str(audit_path)], workdir, "permissive")
    try:
        if not record("B01_service_ready", svc.wait_ready(), f"port={svc.port}"):
            record("B01b_startup_log", False, svc.log_text()[-800:])
            return
        with StaticSite() as site:
            status, _h, body = request(f"{svc.base}/detect?url={site.url}", token=svc.token)
            d = json_or_empty(body)
            route = (d.get("recommendation") or {}).get("route")
            record("B02_detect_plain_site_is_direct",
                   status == 200 and d.get("protected") is False and route == "direct",
                   f"status={status} protected={d.get('protected')} route={route}")

            # 核心收益项（G1）：无盾站不启浏览器。断言「快」+「solves 计数不动」。
            before = svc.health().get("solves")
            t0 = time.monotonic()
            status, _h, body = request(
                f"{svc.base}/solve", token=svc.token, method="POST",
                payload={"url": site.url, "route": "auto"},
            )
            elapsed = time.monotonic() - t0
            j = json_or_empty(body)
            after = svc.health().get("solves")
            record("B03_route_auto_skips_solve",
                   status == 200 and j.get("no_challenge") is True and j.get("solved") is False
                   and before == after == 0,
                   f"status={status} no_challenge={j.get('no_challenge')} "
                   f"solves={before}->{after} elapsed={elapsed:.2f}s")
            record("B04_route_auto_is_fast", elapsed < 10.0, f"elapsed={elapsed:.2f}s (<10s)")
            record("B05_route_auto_returns_decision",
                   isinstance(j.get("routed_by"), dict)
                   and j["routed_by"].get("action") == "skip",
                   f"routed_by={json.dumps(j.get('routed_by') or {}, ensure_ascii=False)[:160]}")

            if skip_browser:
                print("[SKIP] B06-B07 真浏览器路径（--skip-browser）", flush=True)
            else:
                # 真跑最贵的那条路径：拉起 camoufox 去访问本地页，拿不到 clearance → 结构化失败。
                # 断言的不是"能过盾"，是"这条路径不崩、不裸抛、服务事后还活着"。
                status, _h, body = request(
                    f"{svc.base}/solve", token=svc.token, method="POST",
                    payload={"url": site.url, "timeout": 5}, timeout=120,
                )
                j = json_or_empty(body)
                # 契约（docs/API.md §错误码）：`unsolved` 是 **200 + ok:false**（求解失败 ≠ 请求失败），
                # 其余错误码才是非 2xx。所以这里断言的是"结构化"，不是"成功"。
                structured = (
                    (status == 200 and j.get("solved") is True)
                    or (status == 200 and j.get("ok") is False and j.get("error_code") == "unsolved")
                    or (status >= 400 and "error_code" in j)
                )
                record("B06_fixed_path_structured_result", structured,
                       f"status={status} solved={j.get('solved')} ok={j.get('ok')} "
                       f"error_code={j.get('error_code')}")
                h = svc.health()
                record("B07_alive_after_browser_run",
                       h.get("state") == "ready",
                       f"state={h.get('state')} failures={h.get('failures')}")

        # 审计日志：JSONL、有 solve 事件、且**不含 token 原文**（凭证不能进审计）。
        ok = audit_path.exists()
        lines = []
        if ok:
            lines = [ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        parsed = []
        for ln in lines:
            try:
                parsed.append(json.loads(ln))
            except Exception:  # noqa: BLE001
                parsed.append({"_bad": ln[:80]})
        record("B08_audit_log_written", ok and len(parsed) > 0, f"{len(parsed)} 行")
        record("B09_audit_has_solve_event", any(p.get("event") == "solve" for p in parsed),
               "events=" + ",".join(sorted({str(p.get("event")) for p in parsed}))[:120])
        record("B10_audit_has_no_token", bool(svc.token) and svc.token not in
               (audit_path.read_text(encoding="utf-8") if ok else ""),
               "token 未出现在审计文件中" if ok else "审计文件缺失")

        code, http_status = svc.shutdown()
        record("B11_shutdown_202", http_status == "202", f"http={http_status}")
        record("B12_shutdown_exit_code_0", code == 0, f"exit={code}")
        record("B13_no_traceback_in_log", "Traceback" not in svc.log_text(), "")
    finally:
        svc.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="本机真进程 E2E 验收（离线）")
    ap.add_argument("--json-out", default="", help="把结果落盘为 JSON（建议 evidence/e2e-local.json）")
    ap.add_argument("--skip-browser", action="store_true",
                    help="跳过需要 camoufox 的用例（无浏览器环境/CI 用）")
    ap.add_argument("--skip-phase", default="", choices=["", "a", "b"], help="只跑某一段")
    ap.add_argument("--case", default="", help="只跑名字包含该子串的用例（调试用）")
    ap.add_argument("--keep-workdir", action="store_true",
                    help="保留临时工作目录（里面有 .env / 审计文件 / 服务进程日志，排障用）")
    args = ap.parse_args()

    global CASE_FILTER
    CASE_FILTER = args.case

    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="cfsolve-e2e-"))
    try:
        if args.skip_phase in ("", "a"):
            phase_a(workdir)
        if args.skip_phase in ("", "b"):
            phase_b(workdir, args.skip_browser)
    finally:
        if args.keep_workdir:
            print(f"工作目录已保留：{workdir}", flush=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)
        os.environ.pop(TOKEN_ENV, None)

    total = len(RESULTS)
    failed = [r for r in RESULTS if not r["ok"]]
    print("\n=== 汇总 ===", flush=True)
    print(f"{total - len(failed)}/{total} 通过，耗时 {time.time() - started:.1f}s", flush=True)
    for r in failed:
        print(f"  FAILED: {r['name']} — {r['detail']}", flush=True)

    if args.json_out:
        out = Path(args.json_out)
        if not out.is_absolute():
            out = ROOT / out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "task_id": "e2e-local",
            "status": "passed" if not failed else "failed",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "duration_sec": round(time.time() - started, 1),
            "checks": RESULTS,
            "limitations": [
                "离线验收：目标站是本地静态页，不能证明任何真实站点的过盾成功率",
                "未覆盖多进程/多容器共享缓存（见 run.ps1 bench 与 docs/部署-容器.md）",
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已落盘：{out}", flush=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
