"""T-78 真实浏览器验证（policy §6 要求，一次性脚本，不入 CI）。

流程：起真实服务（真 token）→ Playwright 打开 /console → 填 token（模拟 prompt）→
断言四区块渲染 → 截图落 plans/证据/console-20261104.png。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import service
from test_service import FakeBrowser, factory_returning, wait_until

TOKEN = "e2e-console-token-4f8a2b"


def main() -> int:
    from playwright.sync_api import sync_playwright

    # 1. 起真实服务（假浏览器后端：不联网、不出站；验证的是 HTTP 面 + 页面本身）
    browser = FakeBrowser()
    solver = service.Solver(browser_factory=factory_returning(browser), allow_private_hosts=True)
    assert wait_until(lambda: solver.health()[0] == 200)
    service.Handler.solver = solver
    service.Handler.token = TOKEN
    srv = service.Server(("127.0.0.1", 0), service.Handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    print(f"[1] 服务已起 {base}")

    try:
        with sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            ctx = b.new_context(extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
            page = ctx.new_page()
            console_errors: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

            # 页面前端从 sessionStorage 读 token —— 用 add_init_script 在页面脚本运行前注入
            # （等价于用户在 prompt() 里输入并被保存后的状态；prompt 交互另行用 dialog 钩子兜底）
            page.add_init_script(f"sessionStorage.setItem('cfsolve_token', '{TOKEN}')")
            page.on("dialog", lambda d: d.accept(TOKEN))

            # 2. 页面加载（/console 带 token 才给 HTML；页面内的 fetch 用 sessionStorage 的 token）
            page.goto(f"{base}/console", wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            # 4. 断言四区块渲染
            svc_txt = page.locator("#svc").inner_text()
            cache_txt = page.locator("#cache").inner_text()
            research_txt = page.locator("#research").inner_text()
            err_txt = page.locator("#err").inner_text()
            state_txt = page.locator("#state").inner_text()
            print(f"[3] 状态徽章: {state_txt}")
            print(f"    服务状态区块: {svc_txt[:80]!r}")
            print(f"    缓存区块: {cache_txt[:80]!r}")
            print(f"    研究资产区块: {research_txt[:80]!r}")
            print(f"    最近错误区块: {err_txt[:60]!r}")

            assert "ready" in state_txt, "服务状态应为 ready"
            assert "求解" in cache_txt and "命中" in cache_txt, "缓存区块应渲染计数"
            assert "语料变体" in research_txt, "研究资产区块应渲染"
            assert "无错误" in err_txt or "错误发生于" in err_txt, "错误区块应有内容"

            # 5. 页面内无 token 残留（含 sessionStorage 之外的 DOM）
            html = page.content()
            assert TOKEN not in html, "token 不得出现在页面 DOM"
            assert "cf_clearance" not in html
            # sessionStorage 里应当有（这是设计行为），DOM 里没有
            ss = page.evaluate("sessionStorage.getItem('cfsolve_token')")
            print(f"[4] sessionStorage 有 token: {bool(ss)}（设计行为：关标签页即失效）")

            # 6. CSP/no-store 头（浏览器视角）
            resp = page.request.get(f"{base}/console", headers={"Authorization": f"Bearer {TOKEN}"})
            print(f"[5] CSP 头: {resp.headers.get('content-security-policy', '')[:60]}...")
            assert "default-src 'none'" in resp.headers.get("content-security-policy", "")
            assert resp.headers.get("cache-control") == "no-store"

            # 7. 无 console error
            real_errors = [e for e in console_errors if "401" not in e]  # 首次 401 是预期
            assert not real_errors, f"console error: {real_errors}"
            print(f"[6] console 无非预期错误")

            # 8. 截图
            out = Path(__file__).resolve().parents[1] / "plans" / "证据" / "console-20261104.png"
            page.screenshot(path=str(out), full_page=True)
            print(f"[7] 截图已落: {out} ({out.stat().st_size} bytes)")
            b.close()
        print("RESULT: PASS")
        return 0
    finally:
        srv.shutdown()
        srv.server_close()
        solver.close()


if __name__ == "__main__":
    raise SystemExit(main())
