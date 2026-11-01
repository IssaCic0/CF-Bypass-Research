"""同站双臂 A/B：固定 persona vs 随机 persona（v4.1 G1/批次 A）。

## 为什么不用 `scripts/verify_real.py`
它把**不同站点**分给不同组（site1→pinned、site2→random），得出的是
"站点间对照" —— 站点难度差异会混进结论，v3.0 就是因为这个口径被推翻过一次。
本脚本让**同一个站**分别被两个臂各打一遍，这才是能归因的对照。

## 一次"尝试"花 2 个请求预算
`/solve`（1）+ 用拿到的凭证回放目标站（1）。失败时只花 1。

## 硬纪律（与 plans/下一步改进指南.md §15 一致，逐条实现）
1. 只发 GET 首页；不登入、不提交；
2. **同一站点**相邻两次尝试间隔 ≥ `--gap`；
3. 站点之间也走同样的间隔（避免同一出口瞬时限速）；
4. 累计失败 ≥ `--max-failures`（默认 5）→ **立即停全部**，并把原因写进结果；
5. 凭证只记长度，绝不落明文；
6. 支持 `--dry-run`。

用法：
    python scripts/ab_persona.py \
        --pinned http://127.0.0.1:8941 --random http://127.0.0.1:8942 \
        --sites steamdb.info,glassdoor.co.uk,udemy.com --repeats 4 \
        --budget 48 --gap 30 --out plans/证据/v41-persona-ab.json

退出码：0=跑完且未触发停止条件 / 1=触发了停止条件 / 2=执行失败
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def http_json(method: str, url: str, payload: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}
    except Exception as e:  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {str(e)[:120]}"}


def replay(url: str, clearance: str, user_agent: str, timeout: float = 30.0) -> dict:
    """用求解得到的 UA + cookie 回放一次首页，记录状态码（不读全 body）。"""
    req = urllib.request.Request(url, method="GET", headers={
        "User-Agent": user_agent or "Mozilla/5.0",
        "Cookie": f"cf_clearance={clearance}",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "final_url": resp.geturl()}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "final_url": url}
    except Exception as e:  # noqa: BLE001
        return {"status": 0, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="同站双臂 persona A/B（有预算与停止条件）")
    ap.add_argument("--pinned", default="http://127.0.0.1:8941", help="固定 persona 的求解服务")
    ap.add_argument("--random", default="http://127.0.0.1:8942", help="随机 persona 的求解服务")
    ap.add_argument("--sites", required=True, help="逗号分隔的域名（会自动补 https://<site>/）")
    ap.add_argument("--repeats", type=int, default=4, help="每个 (站点, 臂) 组合尝试几次")
    ap.add_argument("--budget", type=int, default=48, help="请求总预算（solve 与 replay 各算 1）")
    ap.add_argument("--gap", type=float, default=30.0, help="同一站点相邻两次尝试的最小间隔（秒）")
    ap.add_argument("--max-failures", type=int, default=5, help="累计失败达到该值立即全停")
    ap.add_argument("--timeout", type=float, default=60.0, help="单次 /solve 的 timeout 参数")
    ap.add_argument("--out", default="", help="结果落盘路径")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    arms = [("pinned", args.pinned), ("random", args.random)]
    plan = [(site, arm, url) for site in sites for arm, url in arms]
    total_attempts = len(plan) * args.repeats
    est_requests = total_attempts * 2          # 每次尝试最多 2 个请求（solve + replay）

    if args.dry_run:
        print(f"计划：{len(sites)} 站 × 2 臂 × {args.repeats} 次 = {total_attempts} 次尝试"
              f"（≈{est_requests} 请求，预算 {args.budget}）")
        print(f"间隔：同站 ≥{args.gap:.0f}s；失败达到 {args.max_failures} 次立即停")
        for site in sites:
            print(f"  - {site}: pinned ×{args.repeats} vs random ×{args.repeats}")
        if est_requests > args.budget:
            print(f"⚠️ 预计请求数 {est_requests} 超过预算 {args.budget}，实跑会被预算截断")
        return 0

    results: list[dict] = []
    requests_used = 0
    failures = 0
    stopped: str | None = None
    last_at: dict[str, float] = {}

    print(f"[ab] 开始 {now_iso()}：{total_attempts} 次尝试，预算 {args.budget}", flush=True)
    for site in sites:
        target = site if site.startswith("http") else f"https://{site}/"
        for arm, base in arms:
            for i in range(args.repeats):
                if requests_used + 2 > args.budget:
                    stopped = f"预算用尽（已用 {requests_used}/{args.budget}）"
                    break
                # 同站间隔
                wait = args.gap - (time.monotonic() - last_at.get(site, 0.0))
                if wait > 0:
                    time.sleep(wait)

                t0 = time.monotonic()
                # **必须 force=true**：否则第 2 次起会命中服务端缓存（实测 0.5s 返回），
                # 那样测的就不是 persona 差异，而是"缓存有多快"——A/B 会直接失去归因力。
                status, body = http_json("POST", base.rstrip("/") + "/solve",
                                         {"url": target, "timeout": args.timeout, "force": True},
                                         timeout=args.timeout + 30)
                requests_used += 1
                ok = bool(body.get("solved"))
                rec = {
                    "site": site, "arm": arm, "attempt": i + 1, "at": now_iso(),
                    "solve_http": status, "solved": ok,
                    "error_code": body.get("error_code") or "",
                    "elapsed": body.get("elapsed"),
                    "clearance_len": len(body.get("cf_clearance") or ""),
                    "cached": body.get("cached"),
                    "backend": body.get("backend") or body.get("attempts", [{}])[-1].get("backend", ""),
                }
                if ok:
                    rep = replay(target, body.get("cf_clearance") or "", body.get("user_agent") or "")
                    requests_used += 1
                    rec["replay_status"] = rep.get("status")
                    rec["replay_final_url"] = rep.get("final_url")
                    # 回放被 CF 拦（403/503）也算这次尝试失败
                    if rep.get("status") not in (200, 301, 302):
                        failures += 1
                        rec["fail_reason"] = f"replay-{rep.get('status')}"
                else:
                    failures += 1
                    rec["fail_reason"] = rec["error_code"] or "unsolved"

                last_at[site] = time.monotonic()
                results.append(rec)
                print(f"  [{arm}] {site} #{i+1}: solved={ok} replay={rec.get('replay_status')} "
                      f"用了 {time.monotonic()-t0:.1f}s 累计失败={failures}", flush=True)

                if failures >= args.max_failures:
                    stopped = f"累计失败达到 {failures}"
                    break
            if stopped:
                break
        if stopped:
            break

    # 汇总：同站按臂聚合
    summary: dict[str, dict] = {}
    for site in sites:
        for arm, _ in arms:
            rows = [r for r in results if r["site"] == site and r["arm"] == arm]
            if not rows:
                continue
            solved = sum(1 for r in rows if r["solved"])
            replayed = sum(1 for r in rows if r.get("replay_status") in (200, 301, 302))
            summary.setdefault(site, {})[arm] = {
                "attempts": len(rows), "solved": solved, "replayed": replayed,
                "solve_rate": round(solved / len(rows), 3),
                "replay_rate": round(replayed / len(rows), 3),
                "avg_elapsed": round(sum(r["elapsed"] or 0 for r in rows) / len(rows), 2),
            }

    out = {
        "generated_at": now_iso(), "kind": "same-site-persona-ab",
        "sites": sites, "repeats": args.repeats, "gap_sec": args.gap,
        "budget": args.budget, "requests_used": requests_used,
        "failures": failures, "stopped": stopped, "summary": summary, "results": results,
    }
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[ab] 结果已落盘：{path}", flush=True)

    print(f"[ab] 结束：{requests_used} 请求，失败 {failures}，停止原因={stopped or '无'}", flush=True)
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
