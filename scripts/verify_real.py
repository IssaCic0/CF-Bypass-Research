"""真机过盾复测（V-1 实验卫生规范）。

这个脚本会**真的向公网站点发请求**，因此内置硬性约束（见 plans/ 的 §20）：

  · 总预算默认 **50 个目标请求**（一次求解算 1、一次回放算 1），花完即停
  · **累计失败 ≥ 5 立即停止**（继续打只会加深"这个 IP 是脚本"的印象）
  · 同站串行，站内间隔 ≥ `--gap`（默认 30s）
  · 不重试失败项（重试会污染结论，也违反"累计失败即停"）
  · 结果逐条落盘到 `plans/证据/`，**即使中途异常也会落盘**（真机实验很贵，不能丢）

A/B 设计（回答"固定 persona 是否有收益"）：
  · 前一半站点打"固定 persona"的服务端，后一半打"随机 persona"的服务端
  · ⚠️ 这是**站点间对照**，站点本身的难度差异会混入结论 —— 报告里必须写明，
    不能当成交互效应显著的结论。

用法：
  # 起两组服务（A 固定、B 随机）
  python solver/service.py --port 8878 --browser-os windows --cache-dir ./cfA
  python solver/service.py --port 8879 --cache-dir ./cfB

  python scripts/verify_real.py --solver-pinned http://127.0.0.1:8878 \
      --solver-random http://127.0.0.1:8879 --token-file .env --budget 50 --gap 30
  python scripts/verify_real.py --dry-run            # 只打印计划，不发请求
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

# Windows 控制台默认 GBK：直接 print 非 ASCII 符号会 UnicodeEncodeError 把整个实验打断。
# 真机上真的发生过——跑到停止条件时崩在 print 上，结果一条都没落盘。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 —— 老版本 / 输出被重定向时忽略
    pass

# 默认站点：取自侦察阶段的采样（公开可访问、会触发 CF 挑战的高流量站点）
DEFAULT_SITES = [
    "https://steamdb.info/",
    "https://www.glassdoor.com/",
    "https://www.epicgames.com/",
    "https://www.quora.com/",
    "https://www.udemy.com/",
    "https://www.canva.com/",
    "https://www.g2.com/",
    "https://www.glassdoor.co.uk/",
    "https://www.patreon.com/",
    "https://www.trustpilot.com/",
]

STOP_AFTER_FAILURES = 5


def read_token(path: str) -> str:
    env = os.environ.get("CF_SOLVE_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CF_SOLVE_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def post_json(url: str, payload: dict, token: str, timeout: float = 200.0):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return e.code, {"ok": False, "error": f"http {e.code}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"ok": False, "error": f"{type(e).__name__}: {e}"}


def replay(target: str, ua: str, cookie: str, timeout: float = 40.0) -> dict:
    req = urllib.request.Request(target)
    req.add_header("User-Agent", ua)
    req.add_header("Cookie", cookie)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "en-US,en;q=0.5")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(200_000)
            return {
                "status": r.status,
                "len": len(body),
                "challenge": b"Just a moment" in body or b"cf-mitigated" in body,
                "elapsed": round(time.time() - t0, 2),
            }
    except urllib.error.HTTPError as e:
        body = e.read(200_000) if hasattr(e, "read") else b""
        return {
            "status": e.code,
            "len": len(body),
            "challenge": b"Just a moment" in body or b"cf-mitigated" in body,
            "elapsed": round(time.time() - t0, 2),
        }
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": f"{type(e).__name__}: {e}", "elapsed": round(time.time() - t0, 2)}


def summarize(results: list[dict], arm: str) -> dict:
    rows = [r for r in results if r["arm"] == arm]
    oks = [r for r in rows if r["ok"]]
    reps = [r for r in rows if "replay_ok" in r]
    return {
        "attempts": len(rows),
        "solve_ok": len(oks),
        "solve_rate": round(len(oks) / len(rows), 3) if rows else 0.0,
        "avg_solve_sec": round(statistics.mean([r["elapsed"] for r in oks]), 2) if oks else None,
        "replays": len(reps),
        "replay_ok": sum(1 for r in reps if r["replay_ok"]),
    }


def dump(path: str, results: list[dict], failures: int, used: int, budget: int, started: float) -> dict:
    payload = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "duration_sec": round(time.time() - started, 1),
        "budget": budget,
        "used": used,
        "failures": failures,
        "stopped_early": failures >= STOP_AFTER_FAILURES,
        "arms": {"pinned": summarize(results, "pinned"), "random": summarize(results, "random")},
        "caveat": "A/B 是站点间对照，站点难度差异会混入结论；不得当成交互效应显著。",
        "results": results,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="真机过盾复测（有预算与停止条件，别乱跑）")
    ap.add_argument("--solver", default="http://127.0.0.1:8878", help="求解服务地址（两组的默认值）")
    ap.add_argument("--solver-pinned", default="", help="A 组（固定 persona）的地址；默认用 --solver")
    ap.add_argument("--solver-random", default="", help="B 组（随机 persona）的地址；默认用 --solver")
    ap.add_argument("--token-file", default=".env")
    ap.add_argument("--token", default="")
    ap.add_argument("--budget", type=int, default=50, help="目标请求总预算（求解+回放各算 1）")
    ap.add_argument("--gap", type=float, default=30.0, help="同站两次尝试的最小间隔（秒）")
    ap.add_argument("--sites", default="", help="逗号分隔覆盖默认站点列表")
    ap.add_argument("--out", default="", help="结果落盘路径（默认 plans/证据/real-verify-<ts>.json）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发任何请求")
    args = ap.parse_args()

    sites = [s.strip() for s in (args.sites.split(",") if args.sites else DEFAULT_SITES) if s.strip()]
    token = args.token or read_token(args.token_file)
    endpoints = {
        "pinned": (args.solver_pinned or args.solver).rstrip("/"),
        "random": (args.solver_random or args.solver).rstrip("/"),
    }

    half = max(1, len(sites) // 2)
    per_site = max(1, args.budget // len(sites))
    plan: list[tuple[str, str, int]] = []
    for idx, site in enumerate(sites):
        arm = "pinned" if idx < half else "random"
        for attempt in range(1, per_site + 1):
            plan.append((site, arm, attempt))
    plan = plan[: args.budget]

    print(
        f"计划：{len(plan)} 次求解，预算 {args.budget}，同站间隔 {args.gap}s，"
        f"失败达到 {STOP_AFTER_FAILURES} 次即停",
        flush=True,
    )
    if args.dry_run:
        for site, arm, attempt in plan:
            print(f"  [{arm}] {site} #{attempt}")
        return 0
    if not token:
        print("[FAIL] 没有 token：用 --token 或 --token-file（或环境变量 CF_SOLVE_TOKEN）", flush=True)
        return 2

    out_path = args.out or os.path.join(
        "plans", "证据", f"real-verify-{time.strftime('%Y%m%d-%H%M%S')}.json"
    )

    results: list[dict] = []
    failures = 0
    used = 0
    started = time.time()
    last_by_site: dict[str, float] = {}
    payload: dict = {}

    try:
        for site, arm, attempt in plan:
            if failures >= STOP_AFTER_FAILURES:
                print(f"[STOP] 累计失败 {failures} 次，按停止条件中止（这是有意为之，不是脚本坏了）", flush=True)
                break
            if used >= args.budget:
                print("[STOP] 预算已用尽", flush=True)
                break

            wait = args.gap - (time.time() - last_by_site.get(site, 0.0))
            if wait > 0:
                time.sleep(wait)

            t0 = time.time()
            status, body = post_json(
                f"{endpoints[arm]}/solve", {"url": site, "timeout": 60, "force": True}, token
            )
            used += 1
            last_by_site[site] = time.time()
            ok = bool(body.get("ok"))
            rec: dict = {
                "site": site,
                "arm": arm,
                "solver": endpoints[arm],
                "attempt": attempt,
                "http": status,
                "ok": ok,
                "error_code": body.get("error_code"),
                "stage": body.get("stage"),
                "elapsed": round(time.time() - t0, 2),
            }
            if ok:
                rec["egress_id"] = body.get("egress_id")
                rec["expires_in"] = body.get("expires_in")
                rec["ua"] = (body.get("user_agent") or "")[:80]
                # 每个站点第 1 次成功后，再花 1 个预算做一次回放（验证同 UA + 同 cookie 是否 200）
                if attempt == 1 and used < args.budget:
                    cookie = body.get("cf_clearance", "")
                    rep = replay(site, body.get("user_agent", ""), f"cf_clearance={cookie}" if cookie else "")
                    used += 1
                    rec["replay"] = rep
                    rec["replay_ok"] = rep.get("status") == 200 and not rep.get("challenge")
            else:
                failures += 1

            results.append(rec)
            line = (
                f"[{arm}] {site} #{attempt} -> ok={ok} {rec.get('error_code') or ''} {rec['elapsed']}s"
                + (f" replay={rec['replay']['status']}" if "replay" in rec else "")
            )
            try:
                print(line, flush=True)
            except Exception:  # noqa: BLE001 —— 打印失败绝不能打断实验
                pass
    finally:
        payload = dump(out_path, results, failures, used, args.budget, started)

    print("\n=== 汇总 ===")
    print(json.dumps({k: payload[k] for k in ("used", "failures", "stopped_early", "arms")},
                     ensure_ascii=False, indent=2))
    print(f"\n结果已落盘：{out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
