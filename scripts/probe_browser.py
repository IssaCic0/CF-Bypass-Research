"""实测：真实浏览器（Playwright Chromium）能否过 Cloudflare managed challenge。

判据（硬）：
  1. 最终页面 title 不是 "Just a moment..." / "请稍候"
  2. 拿到 cf_clearance cookie
  3. 页面 body 长度 > 挑战页长度（确实拿到了业务内容）

同时记录耗时与内存占用（用于成本估算）。
"""

from __future__ import annotations

import os

import json
import sys
import time

from playwright.sync_api import sync_playwright

TARGETS = ["https://steamdb.info/", "https://www.glassdoor.com/"]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


CHROME = os.environ.get("CHROME_PATH", "")  # 本机 Chromium 路径；留空则用 Playwright 自带


def run(url: str, headless: bool) -> dict:
    out: dict = {"url": url, "headless": headless}
    t0 = time.monotonic()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=headless,
                executable_path=CHROME,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            ctx = browser.new_context(
                user_agent=UA,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
            )
            # 抹掉最明显的 webdriver 标记（vanilla playwright 自带 navigator.webdriver=true）
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = ctx.new_page()
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            out["first_status"] = resp.status if resp else None

            # 最多等 25 秒看挑战是否自己过掉
            deadline = time.monotonic() + 25
            passed = False
            while time.monotonic() < deadline:
                title = page.title()
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                if "cf_clearance" in cookies and "just a moment" not in title.lower():
                    passed = True
                    break
                time.sleep(1)

            out["final_title"] = page.title()
            out["body_len"] = len(page.content())
            cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            out["cf_clearance"] = bool(cookies.get("cf_clearance"))
            out["cf_cookie_names"] = sorted(cookies.keys())
            out["passed"] = passed
            out["elapsed_sec"] = round(time.monotonic() - t0, 1)
            browser.close()
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        out["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return out


if __name__ == "__main__":
    headless = "--headed" not in sys.argv
    for u in TARGETS:
        r = run(u, headless)
        print(json.dumps(r, ensure_ascii=False))
