"""滚动重启（本地语义的"热更新"）演练 —— 输出可复现的证据，而不是一句"已验证"。

`docs/部署-容器.md` §4 承诺了这条链路：

    记当前版本 → 起新进程 → 健康检查通过 → **才**停旧进程 → 失败立即回滚（旧进程不停）

`run.ps1 smoke` 与 `scripts/e2e_local.py` 证明的是"单进程能起、能关"。
它们证明不了这条链路真正的风险点，也就是本脚本要打的三个：

  A. **排空（drain）**：`/shutdown` 之后，**已经在途**的请求还能不能拿到响应？
     不能的话，滚动重启就是"每次发布砍掉几个用户请求"，而日志里看不出任何异常
     （服务自己 exit=0，只有客户端拿到连接重置）。这是 T-43 当初的真缺陷。
  B. **滚动期可用性**：逐个重启 worker 时，**别的** worker 必须一直可用。
     一起停 = 全站 503；这是"多进程"这个设计唯一的价值所在。
  C. **新进程先行（blue-green）**：能不能先在备用端口起新进程、健康检查过了再摘旧的？
     这是"失败可回滚"的前提 —— 旧进程不停，回滚就只是"别切流量"。

三个都断言失败数，不靠人眼看日志。全程离线（目标站是本地静态页）。

用法：
    python scripts/hot_update_drill.py
    python scripts/hot_update_drill.py --json-out evidence/hot-update.json
    python scripts/hot_update_drill.py --skip-slow     # 跳过 A（要真跑一次浏览器，约 10s）

退出码：0 全过；1 有失败；2 环境起不来。
"""

from __future__ import annotations

import argparse
import http.server
import json
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

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""), flush=True)
    return ok


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def call(url: str, *, token: str = "", method: str = "GET", payload: dict | None = None,
         timeout: float = 30.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, _json(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json(e.read())


def _json(raw: bytes) -> dict:
    try:
        v = json.loads(raw or b"{}")
        return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_evidence(path: Path, task_id: str, results: list[dict], failed: list[dict],
                   duration: float, cmd: str, limitations: list[str]) -> None:
    """按 `evidence/README.md` 的必填字段落盘（同 `scripts/e2e_local.py`）。

    自定义一个"看起来像证据"的 JSON 等于绕过仓库自己的门禁 —— 下次接手的人没法机器比对。
    `raw_output_tail` 取的是**真实打印过的行**，不是预期结果。
    """
    try:
        baseline = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                  cwd=str(ROOT), capture_output=True, text=True,
                                  timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001 —— 不在 git 仓库里跑也允许
        baseline = ""
    tail = "".join(
        f"[{'PASS' if r['ok'] else 'FAIL'}] {r['name']}"
        + (f"  — {r['detail']}" if r.get("detail") else "") + "\n"
        for r in results[-12:]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "task_id": task_id,
        "status": "done" if not failed else "partial",
        "result": "passed" if not failed else "failed",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "baseline_commit": baseline,
        "duration_sec": round(duration, 1),
        "commands_executed": [{"cmd": cmd, "exit_code": 0 if not failed else 1,
                               "raw_output_tail": tail}],
        "files_changed": [],
        "tests": [{"name": r["name"], "type": "e2e", "status": "pass" if r["ok"] else "fail",
                   "evidence": r.get("detail") or ""} for r in results],
        "checks": results,
        "coverage_summary": None,
        "seams_touched": ["进程生命周期：启动 → ready → /shutdown(202) → 排空 → exit 0",
                          "多 worker 可用性：逐个摘 vs 同时摘"],
        "mock_scenarios_covered": [],
        "browser_validation": None,
        "limitations": limitations,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"结果已落盘：{path}", flush=True)


class Worker:
    """一个 worker 进程，参数与 `docker-compose.yml` / `run.ps1` 保持一致。"""

    def __init__(self, port: int, workdir: Path, tag: str, extra: list[str] | None = None) -> None:
        self.port = port
        self.tag = tag
        self.token_file = workdir / ".env"
        self.cache_dir = workdir / "cache"
        self.log_path = workdir / f"{tag}-{port}.log"
        self.extra = list(extra or ["--allow-private-hosts"])
        self._fp = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "solver/service.py",
             "--port", str(port), "--cache-dir", str(self.cache_dir),
             "--token-file", str(self.token_file), *self.extra],
            cwd=str(ROOT), stdout=self._fp, stderr=subprocess.STDOUT,
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
                status, body = call(f"{self.base}/health", timeout=3)
                if status == 200 and body.get("state") == "ready":
                    self.token = self._read_token()
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        return False

    def _read_token(self) -> str:
        if not self.token_file.exists():
            return ""
        for line in self.token_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(TOKEN_LINE_PREFIX):
                return line[len(TOKEN_LINE_PREFIX):].strip()
        return ""

    def health(self) -> dict:
        try:
            return call(f"{self.base}/health", token=self.token, timeout=5)[1]
        except Exception:  # noqa: BLE001
            return {}

    def shutdown(self, wait: float = 40.0) -> tuple[int | None, int | None]:
        """返回 (http_status, exit_code)。exit_code=None 表示没在 wait 秒内退出。"""
        try:
            status, _b = call(f"{self.base}/shutdown", token=self.token, method="POST",
                              payload={}, timeout=10)
        except Exception:  # noqa: BLE001 —— 排空期间连接可能已被对端关闭
            status = None
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return status, self.proc.returncode
            time.sleep(0.1)
        return status, None

    def log_text(self) -> str:
        try:
            self._fp.flush()
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

        为什么不能直接 kill：worker 下面挂着整个浏览器进程树（几百 MB）。
        硬杀只杀掉父进程，浏览器可能变成孤儿 —— 脚本多跑几次就会攒一堆看不见的常驻进程，
        而这台机器还要用来跑压测。所以先试 `/shutdown`（它会连浏览器一起关），
        拿不到响应再退化到 kill。
        """
        try:
            if self.proc.poll() is None:
                self.shutdown(wait=wait)
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
            self._fp.close()
        except Exception:  # noqa: BLE001
            pass


class Site:
    """本地静态站：让 `fixed` 路径真的跑一次浏览器而不用碰公网。"""

    BODY = b"<html><body>hot-update-drill</body></html>"

    def __init__(self) -> None:
        body = self.BODY

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        self.port = free_port()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.srv.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)

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


# --------------------------------------------------------------------------- A


def drill_drain(workdir: Path, site: Site) -> None:
    """A：在途请求必须在 `/shutdown` 之后拿到响应（排空，不是硬杀）。"""
    print("\n=== A：排空（在途请求 + /shutdown） ===", flush=True)
    w = Worker(free_port(), workdir, "drain")
    try:
        if not record("A01_worker_ready", w.wait_ready(), f"port={w.port}"):
            return
        box: dict = {}

        def slow_solve() -> None:
            t0 = time.monotonic()
            try:
                status, body = call(f"{w.base}/solve", token=w.token, method="POST",
                                    payload={"url": site.url, "timeout": 6}, timeout=90)
                box.update(status=status, body=body, exception=None,
                           elapsed=round(time.monotonic() - t0, 2))
            except Exception as e:  # noqa: BLE001
                box.update(status=None, body={}, exception=f"{type(e).__name__}: {e}",
                           elapsed=round(time.monotonic() - t0, 2))

        th = threading.Thread(target=slow_solve, daemon=True)
        th.start()
        time.sleep(1.0)          # 让它真的进到"浏览器在跑"的状态再发关停
        queue_depth = w.health().get("queue_depth", "n/a")
        status, exit_code = w.shutdown()
        th.join(timeout=60)

        record("A02_shutdown_returns_202", status == 202, f"http={status}")
        record("A03_shutdown_exit_code_0", exit_code == 0, f"exit={exit_code}")
        record("A04_inflight_request_survived",
               box.get("exception") is None and box.get("status") is not None,
               f"status={box.get('status')} elapsed={box.get('elapsed')}s "
               f"exception={box.get('exception')}")
        body = box.get("body") or {}
        record("A05_inflight_returns_structured_body",
               bool(body) and ("solved" in body or "error_code" in body),
               f"keys={sorted(body)[:6]} solved={body.get('solved')} "
               f"error_code={body.get('error_code')}")
        record("A06_no_traceback_in_log", "Traceback" not in w.log_text(),
               f"queue_depth_at_shutdown={queue_depth}")
    finally:
        w.stop()


# --------------------------------------------------------------------------- B / C


class Hammer:
    """后台压测线程：在滚动重启期间持续打一个**没被重启**的 worker。"""

    def __init__(self, worker: Worker, interval: float = 0.05) -> None:
        self.worker = worker
        self.interval = interval
        self.ok = 0
        self.fail = 0
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        n = 0
        while not self._stop.is_set():
            n += 1
            path = "/health" if n % 2 else "/metrics"
            try:
                status, _body = call(f"{self.worker.base}{path}",
                                     token=self.worker.token, timeout=5)
                if status == 200:
                    self.ok += 1
                else:
                    self.fail += 1
                    self.errors.append(f"{path} -> {status}")
            except Exception as e:  # noqa: BLE001
                self.fail += 1
                self.errors.append(f"{path} -> {type(e).__name__}")
            time.sleep(self.interval)

    def __enter__(self):
        self._th.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._th.join(timeout=10)
        return False


def drill_rolling(workdir: Path) -> None:
    """B：逐个重启；被重启的 worker 之外必须全程可用。C：新进程先行。"""
    print("\n=== B：滚动重启（逐个摘） + C：新进程先行（blue-green） ===", flush=True)
    p1, p2 = free_port(), free_port()
    w1 = Worker(p1, workdir, "roll")
    w2 = Worker(p2, workdir, "roll")
    try:
        ok1, ok2 = w1.wait_ready(), w2.wait_ready()
        if not record("B01_two_workers_ready", ok1 and ok2, f"p1={p1} ready={ok1} p2={p2} ready={ok2}"):
            return
        # 两个 worker 共享 token 与 cache 目录（compose 的契约）—— 顺带核对指纹一致
        h1, h2 = w1.health(), w2.health()
        record("B02_shared_cache_and_token",
               bool(w1.token) and w1.token == w2.token
               and h1.get("cache_backend") == h2.get("cache_backend"),
               f"token_same={w1.token == w2.token} cache={h1.get('cache_backend')}")

        # 压测窗口**只覆盖"正在摘 w1"这一段时间**。
        # C 段会故意把 w2 也停掉（那是"切流"本身），把它算进可用性会得到假的失败
        # —— 第一版就是这么错判的（fail=2 全落在 C03 关停瞬间）。
        with Hammer(w2) as hammer:
            # ① 按文档流程重启 w1（stop → start，最坏情况下有一小段空窗）
            status, exit_code = w1.shutdown()
            record("B03_restart_shutdown_202_exit0",
                   status == 202 and exit_code == 0, f"http={status} exit={exit_code}")
            w1b = Worker(p1, workdir, "roll-b")
            try:
                record("B04_restarted_worker_ready", w1b.wait_ready(), f"port={p1}")
                status, _b = call(f"{w1b.base}/metrics", token=w1b.token, timeout=5)
                record("B05_restarted_worker_serves", status == 200, f"/metrics -> {status}")
            finally:
                w1b.stop()

        # `ok > 10` 不是凑数：如果压测线程根本没跑起来（异常退出/0 次循环），
        # `fail == 0` 会假通过 —— "什么都没测"和"测了全对"必须能区分开。
        record("B06_other_worker_zero_failures_during_roll",
               hammer.fail == 0 and hammer.ok > 10,
               f"ok={hammer.ok} fail={hammer.fail} errors={hammer.errors[:3]}")

        # ② C：新进程先在**备用端口**起好、健康检查过了，再摘旧的 —— 零空窗
        spare = Worker(free_port(), workdir, "roll-spare")
        try:
            record("C01_spare_worker_ready", spare.wait_ready(), f"port={spare.port}")
            status, _b = call(f"{w2.base}/health", timeout=5)
            record("C02_old_still_serving_before_cutover", status == 200, f"/health -> {status}")
            status, exit_code = w2.shutdown()
            record("C03_old_drained_after_new_ready",
                   status == 202 and exit_code == 0, f"http={status} exit={exit_code}")
            status, _b = call(f"{spare.base}/metrics", token=spare.token, timeout=5)
            record("C04_new_serving_after_cutover", status == 200, f"/metrics -> {status}")
        finally:
            spare.stop()

        record("B07_no_traceback_in_logs",
               "Traceback" not in w1.log_text() and "Traceback" not in w2.log_text(), "")
    finally:
        w1.stop()
        w2.stop()


def main() -> int:
    ap = argparse.ArgumentParser(description="滚动重启（热更新）演练：排空 / 可用性 / 新进程先行")
    ap.add_argument("--json-out", default="", help="结果落盘路径（建议 evidence/hot-update.json）")
    ap.add_argument("--skip-slow", action="store_true", help="跳过 A 段（A 段会真跑一次浏览器）")
    ap.add_argument("--keep-workdir", action="store_true", help="保留临时工作目录以便排障")
    args = ap.parse_args()

    started = time.time()
    workdir = Path(tempfile.mkdtemp(prefix="cfsolve-roll-"))
    try:
        with Site() as site:
            if not args.skip_slow:
                drill_drain(workdir, site)
            else:
                print("[SKIP] A 段（--skip-slow）", flush=True)
            drill_rolling(workdir)
    finally:
        if args.keep_workdir:
            print(f"工作目录已保留：{workdir}", flush=True)
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    failed = [r for r in RESULTS if not r["ok"]]
    print("\n=== 汇总 ===", flush=True)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过，耗时 {time.time() - started:.1f}s", flush=True)
    for r in failed:
        print(f"  FAILED: {r['name']} — {r['detail']}", flush=True)

    if args.json_out:
        out = Path(args.json_out)
        if not out.is_absolute():
            out = ROOT / out
        write_evidence(out, "hot-update-drill", RESULTS, failed, time.time() - started,
                       "python scripts/hot_update_drill.py"
                       + (" --skip-slow" if args.skip_slow else ""),
                       ["本机多进程语义：没有真负载均衡/反向代理，端口切换由操作者完成",
                        "容器滚动另行验证：evidence/v47-container.md §5/§6",
                        "--skip-slow 时 A 段（排空）未执行"])

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
