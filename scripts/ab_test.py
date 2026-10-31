"""A/B 实测：三种过盾方法的成功率与耗时。

方法
  · cfsolve  —— 纯协议（Rust CLI，无浏览器进程，回放录制的环境快照）
  · pydoll   —— 浏览器（CDP 直连真 Chrome，零 WebDriver，自带 CF 求解器）
  · camoufox —— 浏览器（Playwright + Firefox，C++ 层改指纹）

用法
  python scripts/ab_test.py --sites https://steamdb.info/,https://www.glassdoor.com/
  python scripts/ab_test.py --methods cfsolve,pydoll --out plans/证据/ab.json

纪律
  · 同一站点同一方法只打一次（失败不重试，避免污染结论）
  · 每个方法之间 sleep --gap 秒，避免同时压同一站点
  · 结果落盘到 plans/证据/
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "solver"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass


def method_cfsolve(binary: str, profile: str):
    from cfsolve_backend import CfsolveBackend

    backend = CfsolveBackend(binary, profile, timeout=120)

    def run(url: str) -> dict:
        if not backend.is_configured():
            return {"ok": False, "error": "cfsolve 未配置（缺二进制或 profile）", "elapsed": 0.0}
        t0 = time.monotonic()
        try:
            out = backend.solve(url, timeout=90)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                    "elapsed": round(time.monotonic() - t0, 2)}
        return {"ok": True, "elapsed": out.get("elapsed", round(time.monotonic() - t0, 2)),
                "product": (out.get("cf_clearance") or out.get("token") or "")[:12],
                "ua": (out.get("user_agent") or "")[:40]}

    return run


def method_pydoll(chrome: str, headless: bool):
    from pydoll_backend import PydollBackend

    backend = PydollBackend(chrome, headless=headless, wait=45)

    def run(url: str) -> dict:
        if not backend.is_configured():
            return {"ok": False, "error": "pydoll/Chrome 未配置", "elapsed": 0.0}
        t0 = time.monotonic()
        try:
            out = backend.solve(url, timeout=60)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                    "elapsed": round(time.monotonic() - t0, 2)}
        return {"ok": True, "elapsed": out.get("elapsed", round(time.monotonic() - t0, 2)),
                "product": (out.get("cf_clearance") or "")[:12],
                "ua": (out.get("user_agent") or "")[:40]}

    return run


def method_camoufox(os_persona: str):
    """直接用 Camoufox（与我们服务里同一条路径），测纯求解耗时。"""
    from camoufox.sync_api import Camoufox

    def run(url: str) -> dict:
        t0 = time.monotonic()
        cm = None
        try:
            kwargs = {"headless": True, "geoip": False}
            if os_persona:
                kwargs["os"] = [os_persona]
            cm = Camoufox(**kwargs)
            browser = cm.__enter__()
            ctx = browser.new_context()
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                deadline = time.monotonic() + 45
                clearance = ""
                while time.monotonic() < deadline:
                    for c in ctx.cookies():
                        if c.get("name") == "cf_clearance":
                            clearance = c.get("value", "")
                            break
                    if clearance:
                        break
                    time.sleep(0.15)
                ua = page.evaluate("navigator.userAgent")
                return {"ok": bool(clearance), "elapsed": round(time.monotonic() - t0, 2),
                        "product": clearance[:12], "ua": (ua or "")[:40],
                        "error": "" if clearance else "45s 内无 cf_clearance"}
            finally:
                try:
                    ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}",
                    "elapsed": round(time.monotonic() - t0, 2)}
        finally:
            if cm is not None:
                try:
                    cm.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

    return run


def main() -> int:
    ap = argparse.ArgumentParser(description="三种过盾方法的 A/B 实测")
    ap.add_argument("--sites", default="https://steamdb.info/,https://www.glassdoor.com/,https://www.udemy.com/")
    ap.add_argument("--methods", default="cfsolve,pydoll,camoufox")
    ap.add_argument("--cfsolve-bin", default=os.environ.get("CFSOLVE_BIN", ""))
    ap.add_argument("--cfsolve-profile", default=os.environ.get("CFSOLVE_PROFILE", ""))
    ap.add_argument("--chrome-path", default=os.environ.get("CHROME_PATH", ""))
    ap.add_argument("--browser-os", default="windows")
    ap.add_argument("--pydoll-headless", action="store_true")
    ap.add_argument("--gap", type=float, default=5.0, help="方法之间的间隔（秒）")
    ap.add_argument("--out", default="", help="结果落盘路径")
    args = ap.parse_args()

    from pydoll_backend import find_chrome

    chrome = find_chrome(args.chrome_path)
    builders = {
        "cfsolve": lambda: method_cfsolve(args.cfsolve_bin, args.cfsolve_profile),
        "pydoll": lambda: method_pydoll(chrome, args.pydoll_headless),
        "camoufox": lambda: method_camoufox(args.browser_os),
    }
    methods = [m.strip() for m in args.methods.split(",") if m.strip()]
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]

    print(f"chrome={chrome or '未找到'} | methods={methods} | sites={len(sites)}", flush=True)

    results = []
    for name in methods:
        runner = builders[name]()
        for site in sites:
            print(f"[{name}] {site} ...", end=" ", flush=True)
            r = runner(site)
            r.update({"method": name, "site": site})
            results.append(r)
            print(f"ok={r['ok']} {r['elapsed']}s {r.get('error','')[:60]}", flush=True)
            time.sleep(args.gap)

    # 按方法汇总
    summary = {}
    for name in methods:
        rows = [r for r in results if r["method"] == name]
        oks = [r for r in rows if r["ok"]]
        summary[name] = {
            "attempts": len(rows),
            "success": len(oks),
            "success_rate": round(len(oks) / len(rows), 3) if rows else 0.0,
            "avg_ok_sec": round(statistics.mean([r["elapsed"] for r in oks]), 2) if oks else None,
            "min_ok_sec": min((r["elapsed"] for r in oks), default=None),
            "max_ok_sec": max((r["elapsed"] for r in oks), default=None),
        }

    payload = {"summary": summary, "results": results}
    out = args.out or os.path.join("plans", "证据", f"ab-methods-{time.strftime('%Y%m%d-%H%M%S')}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print("\n=== 汇总（按平均成功耗时升序）===")
    for name, s in sorted(summary.items(), key=lambda kv: (kv[1]["avg_ok_sec"] or 1e9)):
        print(f"{name:10s} 成功 {s['success']}/{s['attempts']}  平均 {s['avg_ok_sec']}s  "
              f"区间 {s['min_ok_sec']}–{s['max_ok_sec']}s")
    print(f"\n结果已落盘：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
