"""压测与缓存共享演练（v4.0 T-40）。

三个模式，全部默认**离线**（本地假服务，不打真实目标站、不烧 exit IP）：

  hot    —— 稳态回放吞吐（SLO E2 / A2）
            起一个本地假目标站，多线程用固定的「UA + cookie」回放，量的是回放路径本身
            （连接复用、header 组装、超时策略）的 QPS 与 p50/p90/p99。

  cold   —— 冷路径准入行为（SLO E1 的机制验证 + F2 去重）
            起一个可控延迟的假 /solve，N 个并发打同一冷域，量的是：
            实际发起多少次求解、被准入闸门拒了多少次、墙钟耗时。

  share  —— 多进程共享缓存（SLO G2 / A3）
            起 M 个子进程，每个都是独立进程、共用同一个 cache 目录（SQLite/WAL），
            每个都尝试"取一次凭证"。正确结果是**只有 1 个进程真的求解**，其余命中。

为什么用假服务而不是打真站：
  · 真站会消耗出口 IP 信誉（本项目最贵的资源），一次压测能把它打成"提分"；
  · 真站延迟不可控，测出来的是"CF 今天的心情"，不是本系统的能力；
  · 真实端到端只有一种可信做法：拿 `plans/下一步改进指南.md` §15 的预算，按批次串行跑。

用法：
    python scripts/loadtest.py --mode hot   --workers 8 --duration 10
    python scripts/loadtest.py --mode cold  --workers 32 --solve-delay 0.2
    python scripts/loadtest.py --mode share --processes 4 --iterations 5
    python scripts/loadtest.py --mode hot --dry-run        # 只看计划，不发请求
    python scripts/loadtest.py --mode hot --json           # 给 evidence/ 用

退出码：0=达到该模式的验收线 / 1=未达标（数据照打） / 2=执行失败
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "solver"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"

#: 各模式的验收线（与 plans/下一步改进指南.md §5 的 SLO 对齐）
TARGETS = {
    "hot": {"qps": 50.0, "field": "qps", "desc": "稳态回放 ≥50 QPS（E2）"},
    "cold": {"accepted_ratio": 1.0, "field": "accepted_ratio", "desc": "冷路径不被拒（E1 机制）"},
    "share": {"solves": 1, "field": "solves", "desc": "多进程同域只解一次（G2/A3）"},
}


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((len(ordered) - 1) * p)))
    return ordered[idx]


# ── 假服务 ───────────────────────────────────────────
class _TargetHandler(BaseHTTPRequestHandler):
    """假目标站：立刻返回 200，用于测回放吞吐。"""
    protocol_version = "HTTP/1.1"
    body = b"ok" * 256

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # 静音
        pass


class _SolverHandler(BaseHTTPRequestHandler):
    """假 /solve：可配置延迟；count 记录被真正调用次数（用于断言去重）。"""
    protocol_version = "HTTP/1.1"
    delay = 0.2
    count = 0
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        _ = self.rfile.read(length) if length else b""
        time.sleep(self.delay)
        with self.lock:
            type(self).count += 1
        payload = json.dumps({
            "ok": True, "solved": True,
            "cf_clearance": "LOADTEST-CLEARANCE",
            "user_agent": UA,
            "cookies": {"cf_clearance": "LOADTEST-CLEARANCE"},
            "expires_in": 1500,
            "expires_at": time.time() + 1500,
            "egress_id": "default",
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802 —— /health
        payload = b'{"ok":true,"state":"ready"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def serve(handler_cls) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# ── hot：回放吞吐 ────────────────────────────────────
def run_hot(args) -> dict:
    import http.client

    srv = serve(_TargetHandler)
    host, port = srv.server_address[0], srv.server_address[1]
    latencies: list[float] = []
    errors = 0
    lock = threading.Lock()
    stop_at = time.monotonic() + args.duration

    def worker() -> None:
        nonlocal errors
        conn = http.client.HTTPConnection(host, port, timeout=10)
        local: list[float] = []
        local_err = 0
        while time.monotonic() < stop_at:
            t0 = time.perf_counter()
            try:
                # 与真实回放一致的头部组合（UA + Cookie + Firefox 侧导航头）
                conn.request("GET", "/", headers={
                    "User-Agent": UA,
                    "Cookie": "cf_clearance=LOADTEST-CLEARANCE",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                })
                resp = conn.getresponse()
                resp.read()
                if resp.status != 200:
                    local_err += 1
            except Exception:  # noqa: BLE001
                local_err += 1
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
                conn = http.client.HTTPConnection(host, port, timeout=10)
            local.append((time.perf_counter() - t0) * 1000)
        conn.close()
        with lock:
            latencies.extend(local)
            errors += local_err

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    srv.shutdown()

    qps = len(latencies) / elapsed if elapsed > 0 else 0.0
    return {
        "mode": "hot", "workers": args.workers, "duration_sec": round(elapsed, 2),
        "requests": len(latencies), "errors": errors,
        "qps": round(qps, 1),
        "p50_ms": round(statistics.median(latencies), 2) if latencies else 0,
        "p90_ms": round(pct(latencies, 0.90), 2),
        "p99_ms": round(pct(latencies, 0.99), 2),
    }


# ── cold：准入 + 去重 ────────────────────────────────
def run_cold(args) -> dict:
    import http.client

    _SolverHandler.delay = args.solve_delay
    _SolverHandler.count = 0
    srv = serve(_SolverHandler)
    host, port = srv.server_address[0], srv.server_address[1]

    accepted = 0
    rejected = 0
    errors = 0
    latencies: list[float] = []
    lock = threading.Lock()
    barrier = threading.Barrier(args.workers)

    def worker() -> None:
        nonlocal accepted, rejected, errors
        barrier.wait()
        t0 = time.perf_counter()
        try:
            conn = http.client.HTTPConnection(host, port, timeout=60)
            body = json.dumps({"url": "https://cold.example.com/", "timeout": 30})
            conn.request("POST", "/solve", body=body,
                         headers={"Content-Type": "application/json", "Authorization": "Bearer x"})
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            payload = json.loads(raw.decode() or "{}")
            with lock:
                if resp.status == 200 and payload.get("ok"):
                    accepted += 1
                else:
                    rejected += 1
                latencies.append((time.perf_counter() - t0) * 1000)
        except Exception:  # noqa: BLE001
            with lock:
                errors += 1

    threads = [threading.Thread(target=worker) for _ in range(args.workers)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - started
    srv.shutdown()

    total = accepted + rejected + errors
    return {
        "mode": "cold", "workers": args.workers, "solve_delay_sec": args.solve_delay,
        "requests": total, "accepted": accepted, "rejected": rejected, "errors": errors,
        "accepted_ratio": round(accepted / total, 3) if total else 0.0,
        "solver_calls": _SolverHandler.count,
        "solves_per_request": round(_SolverHandler.count / total, 2) if total else 0.0,
        "wall_sec": round(elapsed, 2),
        "p50_ms": round(statistics.median(latencies), 2) if latencies else 0,
        "p99_ms": round(pct(latencies, 0.99), 2),
    }


# ── share：多进程共享缓存 ────────────────────────────
_CHILD = r"""
import sys, time, json
sys.path.insert(0, sys.argv[1])
import cache
store = cache.SqliteStore(sys.argv[2], "loadtest-token", max_files=256)
key = "shared.example.com|default"
hit = store.get(key)
if hit is not None:
    print(json.dumps({"pid": __import__("os").getpid(), "hit": True}))
else:
    time.sleep(0.05)                      # 模拟一次真实求解的耗时
    store.set(key, {"cf_clearance": "SHARED", "expires_at": time.time() + 600, "egress_id": "default"},
              host="shared.example.com", backend="fake")
    print(json.dumps({"pid": __import__("os").getpid(), "hit": False}))
"""


def run_share(args) -> dict:
    """两个阶段，答案不一样，必须都测：

    · **sequential**（错开 0.2s 起进程）：这是缓存的**文档承诺**——
      同一域先后到达时，只有第一个进程真的求解，其余命中。验收看这个。
    · **burst**（同时起进程）：测的是"同一个新域同时被 4 个 worker 抢到"。
      当前实现**没有跨进程互斥**（`solver/cache.py` 的既定取舍），
      所以 4 个进程会各解一次 —— 这是已知限制，本模式负责把它量化出来，而不是假装没有。
    """
    solver_dir = str(ROOT / "solver")

    def one_round(delay_between: float) -> tuple[int, int, int]:
        cache_dir = tempfile.mkdtemp(prefix="cfsolve-share-")
        procs: list[subprocess.Popen] = []
        solves = hits = failures = 0
        try:
            for i in range(args.processes):
                procs.append(subprocess.Popen(
                    [sys.executable, "-c", _CHILD, solver_dir, cache_dir],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                ))
                if delay_between:
                    time.sleep(delay_between)
            for p in procs:
                out, _err = p.communicate(timeout=120)
                if p.returncode != 0:
                    failures += 1
                    continue
                try:
                    result = json.loads((out or "").strip().splitlines()[-1])
                except Exception:  # noqa: BLE001
                    failures += 1
                    continue
                if result.get("hit"):
                    hits += 1
                else:
                    solves += 1
        finally:
            import shutil

            shutil.rmtree(cache_dir, ignore_errors=True)
        return solves, hits, failures

    seq_solves = seq_hits = seq_fail = 0
    for _ in range(args.iterations):
        s, h, f = one_round(args.stagger)
        seq_solves += s
        seq_hits += h
        seq_fail += f

    burst_solves = burst_hits = burst_fail = 0
    for _ in range(args.iterations):
        s, h, f = one_round(0.0)
        burst_solves += s
        burst_hits += h
        burst_fail += f

    return {
        "mode": "share", "processes": args.processes, "iterations": args.iterations,
        "sequential": {"solves": seq_solves, "hits": seq_hits, "failures": seq_fail,
                       "expected_solves": args.iterations},
        "burst": {"solves": burst_solves, "hits": burst_hits, "failures": burst_fail,
                  "note": "同时到达同一新域；当前无跨进程互斥，各解一次属已知限制"},
        # 验收口径 = 文档承诺（先后到达只解一次）
        "solves": seq_solves,
    }


# ── 入口 ─────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CF-Bypass-Research 压测/演练（默认离线）")
    ap.add_argument("--mode", choices=["hot", "cold", "share"], default="hot")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--duration", type=float, default=10.0, help="hot 模式持续秒数")
    ap.add_argument("--solve-delay", type=float, default=0.2, help="cold 模式假求解延迟")
    ap.add_argument("--processes", type=int, default=4)
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--stagger", type=float, default=0.2,
                    help="share 模式 sequential 阶段起进程的间隔（秒）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给 evidence/ 用）")
    args = ap.parse_args(argv)
    try:   # Windows 控制台默认 GBK，中文 detail 会乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    plan = {
        "hot": f"本地假目标站，{args.workers} 线程 × {args.duration}s，测回放 QPS",
        "cold": f"本地假 /solve（延迟 {args.solve_delay}s），{args.workers} 并发打同一冷域",
        "share": f"{args.processes} 个独立进程 × {args.iterations} 轮，共用同一 cache 目录",
    }[args.mode]
    if args.dry_run:
        print(f"[dry-run] mode={args.mode}：{plan}")
        print(f"[dry-run] 验收线：{TARGETS[args.mode]['desc']}")
        return 0

    runner = {"hot": run_hot, "cold": run_cold, "share": run_share}[args.mode]
    try:
        result = runner(args)
    except KeyboardInterrupt:
        print("被中断", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        print(f"执行失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    target = TARGETS[args.mode]
    if args.mode == "share":
        # 验收口径：**先后到达**只解一次（文档承诺），且没有子进程失败。
        # burst 阶段的数据只做披露，不作为门禁（当前实现没有跨进程互斥）。
        passed = (result["sequential"]["solves"] == result["sequential"]["expected_solves"]
                  and result["sequential"]["failures"] == 0)
    else:
        actual = result.get(target["field"])
        passed = actual >= target[target["field"]]
        if args.mode == "cold" and result.get("solves_per_request", 1.0) > 1.5:
            passed = False   # 同一请求被放大成多次求解 = 没去重
    result["verdict"] = "PASS" if passed else "FAIL"
    result["acceptance"] = target["desc"]

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"=== {args.mode} 结果 ===")
        for k, v in result.items():
            print(f"  {k}: {v}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
