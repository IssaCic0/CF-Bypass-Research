"""worker 卡死看门狗（v6.2）。

要守住的缺陷：浏览器"半死"时（`is_connected()` 为真但 `ctx.new_page()` 永不返回），
worker 线程永久阻塞，`_submit` 的超时只让调用方拿到 504 —— 服务事实上已死，
/health 还停在 starting。修法：任务超预算 + 宽限仍未返回 → 标记 wedged →
（服务模式下）进程以 75 退出交给 supervisor 重启。

这些用例**不**验证退出码（那会杀掉测试进程），退出码另有 subprocess 用例。
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import service as svc  # noqa: E402
from test_service import wait_until  # noqa: E402


class _Page:
    def evaluate(self, js):
        return "Mozilla/5.0 (Test)"


class _HangingCtx:
    def new_page(self):
        time.sleep(3600)          # 永不返回：模拟 Playwright 调用卡死
    def close(self):
        pass


class _GoodCtx:
    def __init__(self):
        self._page = None
    def new_page(self):
        if self._page is None:
            self._page = _Page()
        return self._page
    def close(self):
        pass


class _Browser:
    """第一次 new_context（读 UA）正常，之后的 context 全部卡死。"""
    def __init__(self):
        self.n = 0
    def is_connected(self):
        return True
    def new_context(self, **k):
        self.n += 1
        return _HangingCtx() if self.n > 1 else _GoodCtx()


def _make_solver(monkeypatch, *, grace=0.5, restart=False, interval=0.2):
    monkeypatch.setattr(svc, "_WATCH_INTERVAL", interval)
    monkeypatch.setattr(svc, "_WEDGE_CONFIRM", 2)
    s = svc.Solver(
        browser_factory=lambda **k: contextlib.nullcontext(_Browser()),
        allow_private_hosts=True, wedge_grace=grace, wedge_restart=restart,
    )
    return s


def test_watchdog_marks_wedged_when_task_never_returns(monkeypatch):
    s = _make_solver(monkeypatch)
    try:
        assert wait_until(lambda: s.health()[1].get("state") == "ready", timeout=15)
        with pytest.raises(Exception):
            s.solve("https://example.com/", timeout=3)
        assert wait_until(lambda: s.health()[1].get("state") == "wedged", timeout=20), \
            "任务卡死超宽限后必须进入 wedged，而不是永远 starting/degraded"
        code, body = s.health()
        assert code == 503 and "卡死" in body.get("wedged_reason", "")
        # 卡死是独立状态：不能混进 degraded（degraded 会自愈，卡死不会）
        assert body["state"] != "degraded"
    finally:
        s.close()


def test_watchdog_survives_long_but_legal_task(monkeypatch):
    """不误杀：任务在预算内跑完（虽然超过 WATCH_INTERVAL 很多倍）不该判卡死。"""
    s = _make_solver(monkeypatch, grace=30.0)
    try:
        assert wait_until(lambda: s.health()[1].get("state") == "ready", timeout=15)
        # 这个 Browser 会在 goto 抛错，但会正常返回 → 不卡死
        with pytest.raises(Exception):
            s.solve("https://example.com/", timeout=5)
        time.sleep(1.0)
        assert s.health()[1].get("state") != "wedged", "正常返回的任务被误判成卡死"
    finally:
        s.close()


def test_wedge_restart_exits_with_code_75_subprocess():
    """服务模式下卡死必须**以 75 退出**——这是 supervisor 唯一能识别的信号。"""
    import subprocess
    import textwrap

    root = Path(__file__).resolve().parents[1]
    code = textwrap.dedent('''
        import contextlib, sys, threading, time
        sys.path.insert(0, "solver")
        import service as svc

        class Page:
            def evaluate(self, js): return "UA"
        class Ctx:
            def __init__(self, hang): self.hang = hang
            def new_page(self):
                if self.hang: time.sleep(3600)
                return Page()
            def close(self): pass
        class Browser:
            def __init__(self): self.n = 0
            def is_connected(self): return True
            def new_context(self, **k):
                self.n += 1
                return Ctx(hang=self.n > 1)

        svc._WATCH_INTERVAL = 0.2
        svc._WEDGE_CONFIRM = 2
        s = svc.Solver(browser_factory=lambda **k: contextlib.nullcontext(Browser()),
                       allow_private_hosts=True, wedge_grace=0.5, wedge_restart=True)
        threading.Thread(target=lambda: (time.sleep(2), s.solve("https://example.com/", timeout=3)),
                         daemon=True).start()
        time.sleep(25)
        print("STILL RUNNING")     # 走到这里说明看门狗没生效
    ''')
    proc = subprocess.run([sys.executable, "-c", code], cwd=root,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 75, f"期望退出码 75，实际 {proc.returncode}: {proc.stdout}{proc.stderr}"
    assert "STILL RUNNING" not in proc.stdout


def test_wedge_restart_default_off_in_library(monkeypatch):
    """库/测试里构造 Solver 默认不自杀：否则任何人 import 一下就可能杀掉宿主进程。"""
    s = _make_solver(monkeypatch)
    try:
        assert s.wedge_restart is False
    finally:
        s.close()
