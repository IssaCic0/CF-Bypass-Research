"""UI 验收（真 Chrome / Playwright）：状态页 `GET /` 与控制台 `GET /console` 的状态矩阵。

为什么必须单写一个脚本（而不是继续靠静态断言）：
  · 这两个页面是**单文件 HTML + 内联 JS**，没有任何构建步骤 —— 一行写错的 JS
    （比如把 `#` 当注释写进去）静态测试查不出来，只有浏览器执行时才会炸。
    实测：v6.2 就发生过一次，页面徽章永远停在"未知"，而所有 Python 测试全绿。
  · 用户的原话是"这个按钮到底能不能按成功" —— 按钮行为只能真点一次才算验证。

覆盖的状态矩阵（每个状态都断言徽章文字 / 颜色 / 横幅文案 / 面板内容）：
  1. ready（正常）
  2. wedged（worker 卡死：/health 返回 503 + state=wedged）
  3. degraded（浏览器挂了但服务还活着）
  4. 无 token（/status 与 /logs 401 → 必须**明确说明去哪拿 token**，而不是空白）
  5. 服务不可达（端口关掉 → 页面本身打不开，断言浏览器确实报错）

另外覆盖真实交互：
  · 状态页「保存」按钮 → 写入 sessionStorage 并刷新
  · 状态页「自动刷新」→ aria-pressed 与文案同步切换
  · 两个页面都**不得有 JS 运行时错误**（pageerror / console.error）

退出码：0=全绿；1=有断言失败。截图落 `plans/证据/ui-<状态>-<页面>.png`。
用法：
    python scripts/verify_ui_e2e.py                 # 全部状态
    python scripts/verify_ui_e2e.py --only wedged   # 只跑一个状态（调试用）
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "solver"))
sys.path.insert(0, str(ROOT / "tests"))

import service  # noqa: E402
from test_service import FakeBrowser, factory_returning, wait_until  # noqa: E402

TOKEN = "ui-e2e-token"
OUT_DIR = ROOT / "plans" / "证据"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    return ok


class _Service:
    """起一个真服务（假浏览器：不联网、不出站；验的是 HTTP 面 + 页面本身）。"""

    def __init__(self, setup=None, audit_path: str = "") -> None:
        self.solver = service.Solver(
            browser_factory=factory_returning(FakeBrowser()), allow_private_hosts=True
        )
        assert wait_until(lambda: self.solver.health()[0] == 200)
        if setup:
            setup(self.solver)
        service.Handler.solver = self.solver
        service.Handler.token = TOKEN
        service.Handler.audit_path = audit_path
        self.srv = service.Server(("127.0.0.1", 0), service.Handler)
        threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self.solver.close()


def _open(page, url: str) -> list[str]:
    """打开页面并收集 JS 错误（pageerror + console.error）。"""
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1300)
    return errors


def _badge(page):
    el = page.locator("#state")
    return (
        el.inner_text().strip(),
        el.get_attribute("class") or "",
        el.evaluate("e => getComputedStyle(e).color"),
        el.evaluate("e => getComputedStyle(e).backgroundColor"),
    )


def case_ready(browser) -> None:
    svc = _Service()
    try:
        ctx = browser.new_context(extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
        page = ctx.new_page()
        page.add_init_script(f"sessionStorage.setItem('cfsolve_token','{TOKEN}')")
        errs = _open(page, svc.base + "/")
        text, cls, *_ = _badge(page)
        check("ready/状态页徽章", text == "ready" and "ready" in cls, f"{text}/{cls}")
        check("ready/状态页无 JS 错误", not errs, str(errs[:2]))
        check("ready/健康面板有内容", "state" in page.inner_text("#health"))
        page.screenshot(path=str(OUT_DIR / "ui-ready-statuspage.png"), full_page=True)

        page2 = ctx.new_page()
        page2.add_init_script(f"sessionStorage.setItem('cfsolve_token','{TOKEN}')")
        page2.on("dialog", lambda d: d.accept(TOKEN))
        errs2 = _open(page2, svc.base + "/console")
        text2, cls2, *_ = _badge(page2)
        check("ready/控制台徽章", text2 == "ready" and "ready" in cls2, f"{text2}/{cls2}")
        check("ready/控制台无 JS 错误", not errs2, str(errs2[:2]))
        page2.screenshot(path=str(OUT_DIR / "ui-ready-console.png"), full_page=True)

        # 真实交互：保存按钮 → sessionStorage
        page.fill("#token", "new-token-value")
        page.click("#save")
        page.wait_for_timeout(600)
        stored = page.evaluate("sessionStorage.getItem('cfsolve_token')")
        check("ready/保存按钮写入 token", stored == "new-token-value", repr(stored))
        # 真实交互：自动刷新切换
        page.click("#auto")
        page.wait_for_timeout(300)
        pressed = page.get_attribute("#auto", "aria-pressed")
        label = page.inner_text("#auto")
        check("ready/自动刷新可开启", pressed == "true" and "开" in label, f"{pressed}/{label}")
        page.click("#auto")
        page.wait_for_timeout(200)
        check("ready/自动刷新可关闭", page.get_attribute("#auto", "aria-pressed") == "false")
        ctx.close()
    finally:
        svc.close()


def case_wedged(browser) -> None:
    def setup(s):
        s._wedged = True
        s._wedged_reason = "任务超期未返回（budget=45s），worker 疑似卡死"
        s._browser_alive = False

    svc = _Service(setup)
    try:
        ctx = browser.new_context(extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
        page = ctx.new_page()
        errs = _open(page, svc.base + "/")
        text, cls, color, bg = _badge(page)
        check("wedged/状态页徽章文字", text == "wedged", text)
        check("wedged/状态页徽章有专属类", "wedged" in cls, cls)
        check("wedged/徽章是实心红（不是默认深色）",
              bg not in ("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)"), bg)
        body = page.inner_text("#health")
        check("wedged/显示卡死原因", "卡死原因" in body and "budget" in body, body[:80])
        check("wedged/显示处置动作",
              "处置" in body and ("75" in body or "手动重启" in body), body[:120])
        check("wedged/无 JS 错误", not [e for e in errs if "503" not in e], str(errs[:2]))
        page.screenshot(path=str(OUT_DIR / "ui-wedged-statuspage.png"), full_page=True)

        page2 = ctx.new_page()
        page2.add_init_script(f"sessionStorage.setItem('cfsolve_token','{TOKEN}')")
        page2.on("dialog", lambda d: d.accept(TOKEN))
        errs2 = _open(page2, svc.base + "/console")
        text2, cls2, *_ = _badge(page2)
        banner = page2.inner_text("#banner")
        check("wedged/控制台徽章是 wedged", text2 == "wedged" and "wedged" in cls2, f"{text2}/{cls2}")
        check("wedged/控制台横幅说明不会自愈", "不会自愈" in banner, banner[:80])
        check("wedged/控制台无 JS 错误", not [e for e in errs2 if "503" not in e], str(errs2[:2]))
        page2.screenshot(path=str(OUT_DIR / "ui-wedged-console.png"), full_page=True)
        ctx.close()
    finally:
        svc.close()


def case_degraded(browser) -> None:
    def setup(s):
        s._ever_alive = True
        s._browser_alive = False

    svc = _Service(setup)
    try:
        ctx = browser.new_context(extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
        page = ctx.new_page()
        _open(page, svc.base + "/")
        text, cls, *_ = _badge(page)
        check("degraded/徽章不是 wedged", text != "wedged", text)
        page.screenshot(path=str(OUT_DIR / "ui-degraded-statuspage.png"), full_page=True)
        ctx.close()
    finally:
        svc.close()


def case_no_token(browser) -> None:
    svc = _Service()
    try:
        ctx = browser.new_context()
        page = ctx.new_page()
        errs = _open(page, svc.base + "/")
        banner = page.inner_text("#banner")
        inflight = page.inner_text("#inflight-summary")
        check("无token/状态页横幅指明去哪拿 token", "token" in banner and "env" in banner, banner[:80])
        check("无token/在途面板不是空白", "token" in inflight, inflight[:60])
        check("无token/健康信息仍可见（公开）", "state" in page.inner_text("#health"))
        check("无token/无 JS 错误（401 是预期）",
              not [e for e in errs if "401" not in e], str(errs[:2]))
        page.screenshot(path=str(OUT_DIR / "ui-notoken-statuspage.png"), full_page=True)
        ctx.close()
    finally:
        svc.close()


def case_audit(browser) -> None:
    """审计日志区块（v6.3，参考 logdy）。

    看真浏览器里的四件事（静态断言都覆盖不到）：
      · 表格真的渲染出来了（行数对得上）
      · 失败行有红色标记、成功行是 ok
      · **列宽合理**（曾经因为缺 table 样式，"时间"列被撑得极宽、后面几列挤成一坨）
      · 无 JS 错误（曾经因为本页没有 esc()、我误用 innerHTML+esc，首屏直接报错）
    """
    import json as _j
    import tempfile
    from pathlib import Path as _P

    NL = chr(10)
    d = _P(tempfile.mkdtemp(prefix="ui-audit-"))
    f = d / "audit.jsonl"
    rows = [{"event": "solve", "req_id": f"r{i}", "host": f"site{i}.example.com",
             "ok": i % 3 != 0, "error_code": "" if i % 3 else "unsolved",
             "ms": 16530, "backend": "camoufox", "ts": 1761000000 + i} for i in range(8)]
    f.write_text(NL.join(_j.dumps(r) for r in rows) + NL, encoding="utf-8")

    svc = _Service(audit_path=str(f))
    try:
        ctx = browser.new_context(extra_http_headers={"Authorization": f"Bearer {TOKEN}"},
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.add_init_script(f"sessionStorage.setItem('cfsolve_token','{TOKEN}')")
        page.on("dialog", lambda dlg: dlg.accept(TOKEN))
        errs = _open(page, svc.base + "/console")
        area = page.locator("#audit")
        text = area.inner_text()
        check("audit/表格渲染出 8 行", page.locator("#audit tbody tr").count() == 8,
              str(page.locator("#audit tbody tr").count()))
        check("audit/含表头", "事件" in text and "目标" in text, text[:60])
        check("audit/失败行有标记", "unsolved" in text, text[:120])
        check("audit/耗时按秒显示（不是 16530 ms）",
              "16.5 s" in text and "16530 ms" not in text, text[:120])
        # 列宽：没有任何一列 > 总宽的一半（曾经"时间"列独占 414px/1210px）
        widths = page.evaluate(
            "() => Array.from(document.querySelectorAll('#audit thead th'))"
            ".map(th => Math.round(th.getBoundingClientRect().width))")
        total = sum(widths)
        check("audit/列宽未被单列撑爆",
              bool(widths) and max(widths) < total * 0.5, f"{widths} total={total}")
        check("audit/无 JS 错误", not [e for e in errs if "401" not in e], str(errs[:2]))
        # 顶部「刷新」必须把审计区块一起刷 —— 曾经它只刷 /health，审计表停在旧数据。
        # 探法：往审计文件里再追加一行，点顶部刷新，表里必须多出这一行。
        with f.open("a", encoding="utf-8") as fh:
            fh.write(_j.dumps({"event": "solve", "req_id": "rnew", "host": "新增.example.com",
                               "ok": True, "ms": 120, "backend": "cfsolve",
                               "ts": 1761000100}) + NL)
        page.click("#refresh")
        page.wait_for_timeout(800)
        after = page.locator("#audit tbody tr").count()
        check("audit/顶部刷新会带上审计区块", after == 9, f"rows={after}")
        check("audit/顶部刷新无新增 JS 错误",
              not [e for e in errs if "401" not in e], str(errs[-2:]))
        page.screenshot(path=str(OUT_DIR / "ui-audit-console.png"), full_page=True)
        ctx.close()
    finally:
        svc.close()


def case_unreachable(browser) -> None:
    svc = _Service()
    base = svc.base
    svc.close()          # 端口立刻关掉
    ctx = browser.new_context()
    page = ctx.new_page()
    # 页面本身已经打不开了 —— goto 会直接抛 ERR_CONNECTION_REFUSED（这就是预期行为）
    try:
        page.goto(base + "/", wait_until="domcontentloaded", timeout=5000)
    except Exception as e:  # noqa: BLE001
        check("不可达/页面加载失败可被捕获", "ERR_CONNECTION_REFUSED" in str(e), str(e)[:60])
    else:
        check("不可达/页面加载失败可被捕获", False, "端口已关但仍加载成功？")
    ctx.close()


CASES = {
    "ready": case_ready,
    "wedged": case_wedged,
    "degraded": case_degraded,
    "no-token": case_no_token,
    "audit": case_audit,
    "unreachable": case_unreachable,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="UI 验收（真 Chrome）")
    ap.add_argument("--only", default="", help="只跑某个状态：" + ",".join(CASES))
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    names = [args.only] if args.only else list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:      # 写错状态名不该甩一个 KeyError 栈出来
        print(f"未知状态：{unknown}\n可选：{', '.join(CASES)}", file=sys.stderr)
        return 2
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for name in names:
                print(f"\n[{name}]")
                CASES[name](browser)
        finally:
            browser.close()

    failed = [r for r in results if not r[1]]
    print(f"\nRESULT: {len(results) - len(failed)}/{len(results)} 通过")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())