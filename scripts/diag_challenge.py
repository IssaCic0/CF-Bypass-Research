"""诊断：managed challenge 卡在哪一步（widget? checkbox? 静默等待?）。

输出：iframe 清单、可见交互元素、截图、二次等待后状态。
"""

from __future__ import annotations

import os

import json
import time

from playwright.sync_api import sync_playwright

CHROME = os.environ.get("CHROME_PATH", "")  # 本机 Chromium 路径；留空则用 Playwright 自带
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
URL = "https://steamdb.info/"


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch(
            headless=False,
            executable_path=CHROME,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)

        info: dict = {}
        info["title"] = page.title()
        info["iframes"] = [
            {"src": f.get_attribute("src"), "visible": f.is_visible()}
            for f in page.query_selector_all("iframe")
        ]
        info["checkboxes"] = page.query_selector_all("input[type=checkbox]").__len__()
        info["buttons"] = [
            (btn.inner_text() or "").strip()[:40]
            for btn in page.query_selector_all("button")
            if btn.is_visible()
        ][:10]
        info["inputs"] = page.query_selector_all("input").__len__()
        info["body_head"] = page.content()[:300].replace("\n", " ")

        # 尝试点 widget 内的 checkbox（challenges.cloudflare.com iframe）
        clicked = []
        for fr in page.frames:
            if "challenges.cloudflare.com" in (fr.url or ""):
                try:
                    for sel in ["input[type=checkbox]", "#challenge-stage input", "label", "body"]:
                        el = fr.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=3000)
                            clicked.append(f"{fr.url[:60]} :: {sel}")
                            break
                except Exception as e:  # noqa: BLE001
                    clicked.append(f"click-fail {type(e).__name__}")
        info["clicked"] = clicked

        # 再等 20s 看是否放行
        deadline = time.monotonic() + 20
        passed = False
        while time.monotonic() < deadline:
            cookies = {c["name"] for c in ctx.cookies()}
            if "cf_clearance" in cookies:
                passed = True
                break
            time.sleep(1)
        info["passed_after_click"] = passed
        info["final_title"] = page.title()
        info["cookies"] = sorted({c["name"] for c in ctx.cookies()})
        page.screenshot(path="challenge_screenshot.png", full_page=False)
        print(json.dumps(info, ensure_ascii=False, indent=1))
        b.close()


if __name__ == "__main__":
    main()
