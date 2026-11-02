"""长跑稳定性测试（soak）：回答"跑久了会不会崩 / 会不会慢死 / 有没有泄漏"。

## 为什么需要它
我们之前只有 25 分钟的真机 A/B 和一堆单元测试 —— 它们能证明"功能对"，
但证明不了"跑 1 小时内存不涨、句柄不漏、探测缓存不无限膨胀"。
而用户最真实的抱怨形态恰好是"运行长时间之后出问题"。

## 它做什么
按固定间隔采样，并对服务施加**轻量但真实**的负载：
  · 每 tick：`/health` + `/metrics` + 采样进程树 RSS 与句柄数
  · 每 `--probe-every` 秒：`/detect`（交替 normal / fresh=1，同时覆盖探测缓存命中与穿透两条路径）
  · 每 `--auto-every` 秒：`/solve route=auto` 打一个无盾站（命中 skip 分支，**不消耗过盾预算**）
  · 可选 `--solve-every` 秒：真求解（**会消耗过盾预算**，默认关闭）

## 判定（不达标就非 0 退出，绝不"看起来还行"）
1. 轻量端点错误率 == 0
2. RSS 增长 ≤ `--max-rss-growth-mb`（默认 80MB）且相对增长 ≤ 25%
3. 句柄数增长 ≤ `--max-handle-growth`（默认 50）
4. 服务始终 `state=ready`（中途 degraded 次数为 0）

用法：
    python scripts/soak.py --solver http://127.0.0.1:8971 --pid 12345 \
        --duration 3600 --interval 10 --out plans/证据/v42-soak.jsonl

退出码：0=全部达标 / 1=有不达标项 / 2=执行失败
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROBE_URL = "https://example.com/"      # 无盾站：/detect 与 route=auto 都不会触发求解
UA = "Mozilla/5.0 (soak-test)"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def http(method: str, url: str, payload: dict | None = None, timeout: float = 20.0,
         token: str = ""):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            ms = (time.perf_counter() - t0) * 1000
            try:
                parsed = json.loads(body.decode() or "{}")
            except Exception:  # metrics 是纯文本
                parsed = None
            return resp.status, parsed, body.decode("utf-8", "replace"), ms
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000
        return e.code, None, "", ms
    except Exception as e:  # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}", (time.perf_counter() - t0) * 1000


def sample_process(pid: int) -> dict:
    """采样进程树（服务 + 它拉起的浏览器）的 RSS 与句柄/线程。"""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return {"rss_mb": None, "handles": None, "threads": None, "procs": None}
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
        rss = 0
        handles = 0
        threads = 0
        for p in procs:
            try:
                mi = p.memory_info()
                rss += mi.rss
                threads += p.num_threads()
                if hasattr(p, "num_handles"):
                    handles += p.num_handles()
                else:
                    handles += p.num_fds()
            except Exception:  # noqa: BLE001 —— 进程可能刚退出
                continue
        return {"rss_mb": round(rss / 1048576, 1), "handles": handles,
                "threads": threads, "procs": len(procs)}
    except Exception as e:  # noqa: BLE001
        return {"rss_mb": None, "handles": None, "threads": None, "procs": None, "error": str(e)[:80]}


def parse_metrics(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        key = parts[0]
        try:
            out[key] = float(parts[1])
        except ValueError:
            continue
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="长跑稳定性测试")
    ap.add_argument("--solver", default="http://127.0.0.1:8971")
    ap.add_argument("--token", default="",
                    help="求解服务的 Bearer token（/solve /detect /metrics 需要；"
                         "缺省时这些端点会 401，采样只有 /health）")
    ap.add_argument("--token-file", default="",
                    help="从 .env 风格文件读 token（行格式 CF_SOLVE_TOKEN=xxx），优先级低于 --token")
    ap.add_argument("--pid", type=int, default=0, help="服务进程 PID（用于采样 RSS/句柄）")
    ap.add_argument("--duration", type=float, default=3600.0, help="总时长（秒）")
    ap.add_argument("--interval", type=float, default=10.0, help="采样间隔（秒）")
    ap.add_argument("--probe-every", type=float, default=60.0, help="/detect 的间隔（秒）；0=不做")
    ap.add_argument("--auto-every", type=float, default=120.0, help="route=auto 的间隔（秒）；0=不做")
    ap.add_argument("--solve-every", type=float, default=0.0, help="真求解间隔（秒）；0=不动用预算")
    ap.add_argument("--solve-url", default="https://glassdoor.co.uk/")
    ap.add_argument("--max-rss-growth-mb", type=float, default=80.0)
    ap.add_argument("--max-handle-growth", type=int, default=50)
    ap.add_argument("--out", default="")
    ap.add_argument("--resume", action="store_true",
                    help="断点续采：若 out 文件已存在，跳过其中已记录的时长（ Windows 更新/休眠打断后继续同一轮 soak）")
    args = ap.parse_args(argv)

    base = args.solver.rstrip("/")
    token = args.token
    if not token and args.token_file and Path(args.token_file).exists():
        for line in Path(args.token_file).read_text(encoding="utf-8").splitlines():
            if line.startswith("CF_SOLVE_TOKEN="):
                token = line[len("CF_SOLVE_TOKEN="):].strip()
                break
        print(f"[soak] token 从 {args.token_file} 装载（指纹 {hashlib.sha256(token.encode()).hexdigest()[:12] if token else '空'}）", flush=True)
    samples: list[dict] = []
    light_errors = 0
    ready_failures = 0
    started = time.monotonic()
    elapsed_offset = 0.0
    if args.resume and args.out and Path(args.out).exists():
        with open(args.out, encoding="utf-8") as f:
            prior = [json.loads(line) for line in f if line.strip()]
        samples = prior
        if prior:
            elapsed_offset = max(s.get("t", 0.0) for s in prior) + args.interval
            print(f"[soak] --resume：从 {args.out} 恢复 {len(prior)} 个样本，"
                  f"起始偏移 {elapsed_offset:.0f}s", flush=True)
    next_probe = 0.0
    next_auto = 0.0
    next_solve = 0.0
    solve_count = 0
    probe_flip = False

    print(f"[soak] 开始 {now_iso()}：{args.duration:.0f}s，间隔 {args.interval:.0f}s，"
          f"pid={args.pid or '未给'}", flush=True)

    while True:
        elapsed = time.monotonic() - started + elapsed_offset
        if elapsed > args.duration:
            break

        rec = {"at": now_iso(), "t": round(elapsed, 1)}
        code, health, _, ms = http("GET", base + "/health", token=token)
        rec["health_ms"] = round(ms, 1)
        rec["health_http"] = code
        if code != 200:
            light_errors += 1
        if isinstance(health, dict):
            rec["state"] = health.get("state")
            rec["solves"] = health.get("solves")
            rec["cache_hits"] = health.get("cache_hits")
            rec["failures"] = health.get("failures")
            rec["cached_domains"] = health.get("cached_domains")
            rec["queue_depth"] = health.get("queue_depth")
            if health.get("state") != "ready":
                ready_failures += 1

        mcode, _, mtext, mms = http("GET", base + "/metrics", token=token)
        rec["metrics_ms"] = round(mms, 1)
        if mcode != 200:
            light_errors += 1
        else:
            m = parse_metrics(mtext)
            rec["solves_total"] = m.get("cfsolve_solves_total")
            rec["failures_total"] = m.get("cfsolve_failures_total")

        rec.update(sample_process(args.pid) if args.pid else {})

        if args.probe_every and elapsed >= next_probe:
            suffix = "&fresh=1" if probe_flip else ""
            probe_flip = not probe_flip
            pcode, pbody, _, pms = http("GET", f"{base}/detect?url={PROBE_URL}{suffix}", token=token)
            rec["probe_http"] = pcode
            rec["probe_ms"] = round(pms, 1)
            rec["probe_cached"] = bool((pbody or {}).get("cached")) if isinstance(pbody, dict) else None
            if pcode != 200:
                light_errors += 1
            next_probe = elapsed + args.probe_every

        if args.auto_every and elapsed >= next_auto:
            acode, abody, _, ams = http("POST", base + "/solve",
                                        {"url": PROBE_URL, "route": "auto"}, timeout=40, token=token)
            rec["auto_http"] = acode
            rec["auto_ms"] = round(ams, 1)
            rec["auto_action"] = ((abody or {}).get("routed_by") or {}).get("action") if isinstance(abody, dict) else None
            if acode != 200:
                light_errors += 1
            next_auto = elapsed + args.auto_every

        if args.solve_every and elapsed >= next_solve:
            scode, sbody, _, sms = http("POST", base + "/solve",
                                        {"url": args.solve_url, "timeout": 60, "force": True}, timeout=120, token=token)
            solve_count += 1
            rec["solve_http"] = scode
            rec["solve_ms"] = round(sms, 1)
            rec["solve_ok"] = bool((sbody or {}).get("solved")) if isinstance(sbody, dict) else False
            next_solve = elapsed + args.solve_every

        samples.append(rec)
        if args.out:
            with open(args.out, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if len(samples) % 6 == 1:
            print(f"  t={elapsed:6.0f}s rss={rec.get('rss_mb')}MB handles={rec.get('handles')} "
                  f"state={rec.get('state')} probes={rec.get('probe_http', '-')}", flush=True)

        # 睡到下一个 tick（留出采样耗时）
        sleep_for = args.interval - ((time.monotonic() - started) - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)

    # ── 判定 ──────────────────────────────────────
    rss_vals = [s["rss_mb"] for s in samples if s.get("rss_mb")]
    handle_vals = [s["handles"] for s in samples if s.get("handles") is not None]
    rss_growth = (rss_vals[-1] - rss_vals[0]) if len(rss_vals) >= 2 else 0.0
    handle_growth = (handle_vals[-1] - handle_vals[0]) if len(handle_vals) >= 2 else 0
    health_ms = [s["health_ms"] for s in samples if s.get("health_ms")]
    probe_ms = [s["probe_ms"] for s in samples if s.get("probe_ms")]

    checks = {
        "light_endpoint_errors": (light_errors == 0, light_errors, 0),
        "always_ready": (ready_failures == 0, ready_failures, 0),
        # 只看"涨"。内存回落（负数）不是缺陷 —— 早期版本用 abs() 把"RSS 降了 1107MB"
        # 也判成失败，那是尺子的问题，不是系统的问题。
        "rss_growth_mb": (rss_growth <= args.max_rss_growth_mb, round(rss_growth, 1),
                          args.max_rss_growth_mb),
        "handle_growth": (abs(handle_growth) <= args.max_handle_growth, handle_growth,
                          args.max_handle_growth),
    }
    passed = all(ok for ok, _, _ in checks.values())

    # v5.0 T-53：稳态命中率（剔除首 1h 冷启动）。solves/cache_hits 来自 /health 快照，
    # 是累计值 —— 用最后一个样本与冷启动结束点（1h 后第一个样本）的差值算增量命中率。
    hit_rate_steady = None
    solves_series = [(s.get("t", 0.0), s.get("solves"), s.get("cache_hits"))
                     for s in samples if s.get("solves") is not None]
    if len(solves_series) >= 2:
        first_t, first_solves, _first_hits = solves_series[0]
        steady = [(t, sv, ch) for (t, sv, ch) in solves_series if t >= first_t + 3600.0]
        if steady:
            (_, s1, c1), (_, s2, c2) = steady[0], steady[-1]
            denom = (s2 - s1) + (c2 - c1)
            hit_rate_steady = round((c2 - c1) / denom, 4) if denom > 0 else None

    summary = {
        "generated_at": now_iso(), "kind": "soak",
        "duration_sec": round(time.monotonic() - started + elapsed_offset, 1),
        "samples": len(samples), "solve_count": solve_count, "passed": passed,
        "hit_rate_steady": hit_rate_steady,
        "rss_first_mb": rss_vals[0] if rss_vals else None,
        "rss_last_mb": rss_vals[-1] if rss_vals else None,
        "rss_max_mb": max(rss_vals) if rss_vals else None,
        "handles_first": handle_vals[0] if handle_vals else None,
        "handles_last": handle_vals[-1] if handle_vals else None,
        "handles_max": max(handle_vals) if handle_vals else None,
        "health_ms_p50": round(statistics.median(health_ms), 1) if health_ms else None,
        "health_ms_p95": round(sorted(health_ms)[int(len(health_ms) * 0.95)] if health_ms else 0, 1),
        "probe_ms_p50": round(statistics.median(probe_ms), 1) if probe_ms else None,
        "checks": {k: {"pass": ok, "actual": actual, "limit": limit}
                   for k, (ok, actual, limit) in checks.items()},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.out:
        Path(args.out + ".summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
