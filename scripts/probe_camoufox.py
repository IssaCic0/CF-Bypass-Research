"""实测 Camoufox（C++ 层指纹篡改的 Firefox）能否过 Cloudflare managed challenge。

对照上一轮 vanilla Playwright 的失败结果：
  判据 1: 最终 title 不是 "Just a moment..." / "请稍候…"
  判据 2: 拿到 cf_clearance cookie
  判据 3: body 长度 > 挑战页（拿到真实业务内容）
"""

from __future__ import annotations

import json
import time

from camoufox.sync_api import Camoufox

TARGETS = ["https://steamdb.info/", "https://www.glassdoor.com/"]


def probe(url: str, headless: bool) -> dict:
    out: dict = {"url": url, "engine": "camoufox", "headless": headless}
    t0 = time.monotonic()
    try:
        with Camoufox(headless=headless, geoip=False) as browser:
            page = browser.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            out["first_status"] = resp.status if resp else None
            deadline = time.monotonic() + 30
            passed = False
            while time.monotonic() < deadline:
                title = page.title()
                names = {c["name"] for c in page.context.cookies()}
                if "cf_clearance" in names and "just a moment" not in title.lower():
                    passed = True
                    break
                time.sleep(1)
            out["final_title"] = page.title()
            out["body_len"] = len(page.content())
            out["cookies"] = sorted({c["name"] for c in page.context.cookies()})
            out["has_cf_clearance"] = "cf_clearance" in out["cookies"]
            out["passed"] = passed
            out["elapsed_sec"] = round(time.monotonic() - t0, 1)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
        out["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return out


if __name__ == "__main__":
    for u in TARGETS:
        print(json.dumps(probe(u, headless=True), ensure_ascii=False), flush=True)
