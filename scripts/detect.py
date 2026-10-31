"""一眼看出目标站有没有盾：命令行探测工具。

用法：
    python scripts/detect.py https://target/
    python scripts/detect.py https://a/ https://b/ --json

它只发一次普通 GET（不启浏览器、不求解），把状态码/头/cookie/body 交给 `solver/detect.py` 识别：
  · 有哪些厂商的防护（Cloudflare / Akamai / DataDome / PerimeterX / AWS WAF / reCAPTCHA …）
  · 置信度与命中证据
  · 如果是 CF，还能看出挑战类型（managed / jsd / turnstile …）
  · 以及**建议走哪条路**（直接抓 / 纯协议 / 浏览器 / want=turnstile）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "solver"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from detect import summarize  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"


def probe(url: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        },
    )
    status, headers, body = 0, {}, ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            headers = dict(response.headers.items())
            body = response.read(500_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        headers = dict(e.headers.items()) if e.headers else {}
        try:
            body = e.read(500_000).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = ""
    except Exception as e:  # noqa: BLE001
        return {"url": url, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    cookies = {}
    for part in (headers.get("Set-Cookie") or "").split(","):
        name = part.split("=", 1)[0].strip()
        if name and " " not in name:
            cookies[name] = "1"
    return summarize(url=url, status=status, headers=headers, body=body, cookies=cookies)


def main() -> int:
    ap = argparse.ArgumentParser(description="探测目标站的防护类型（只发一次 GET，不求解）")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    results = [probe(u, args.timeout) for u in args.urls]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        print("=" * 70)
        print(r["url"])
        if r.get("error"):
            print("  探测失败:", r["error"])
            continue
        providers = r.get("providers") or []
        print(f"  状态码: {r['status']}   有防护: {'是' if r['protected'] else '否'}")
        for p in providers[:4]:
            print(f"  · {p['name']}（{p['category']}, 难度 {p['difficulty']}, 置信度 {p['confidence']}）")
            print(f"    证据: {', '.join(p['hits'][:4])}")
        if r.get("cloudflare_challenge_type"):
            print("  CF 挑战类型:", r["cloudflare_challenge_type"])
        rec = r.get("recommendation") or {}
        print(f"  👉 建议: {rec.get('route')} — {rec.get('reason')}")
        if rec.get("hint"):
            print(f"     {rec['hint']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
