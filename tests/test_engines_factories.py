"""引擎工厂的**分支级**测试（v6.2，借鉴 web-crawler 的"双档覆盖率"思路）。

要解决的问题：`solver/engines.py` 的覆盖率长期停在 57%，`docs/coverage-policy.json`
为此登记了 waiver，理由写的是"cloakbrowser 是专有二进制，本机与 CI 都装不了，
剩余分支无法执行"。

**这个理由不成立**。工厂函数的分支逻辑（"按引擎名选实现、包成统一上下文管理器、
失败时清理"）与二进制本身无关 —— 用**注入假模块**就能把三个工厂的全部路径跑一遍：
  · `cloakbrowser` → 假 `launch()` 返回一个带 `close()` 的对象
  · `patchright` → 假 `sync_playwright()`，覆盖成功 + launch 失败（必须 stop 掉 pw）
  · `camoufox` → 只**构造**不 enter（不启动浏览器进程）

这就是"双档覆盖率"的含义：**逻辑档**（不装依赖也能测）与**集成档**（真二进制，另有豁免）。
把逻辑档测满之后，waiver 就该收窄到"真实二进制行为"这一小块，而不是整个模块。

诚实的边界：这些用例证明的是**我们的封装逻辑**，不是"cloakbrowser 真能过盾"。
后者仍需真机，见 policy.md §2 的预算台账。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import engines as eng  # noqa: E402


class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = 0

    def close(self) -> None:
        self.closed += 1


def _install(monkeypatch, name: str, **attrs):
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


# ── cloakbrowser 工厂 ───────────────────────────────


def test_cloakbrowser_factory_wraps_browser(monkeypatch):
    seen = {}

    def fake_launch(**kwargs):
        seen.update(kwargs)
        return _FakeBrowser()

    _install(monkeypatch, "cloakbrowser", launch=fake_launch)
    ctx = eng.build_factory(eng.Engine.CLOAKBROWSER)(headless=True)
    assert isinstance(ctx, eng._WrappedBrowser)
    with ctx as browser:
        assert isinstance(browser, _FakeBrowser)
    # launch 的入参必须原样透传（headless 是求解模式的一部分）
    assert seen == {"headless": True}


def test_cloakbrowser_factory_closes_browser(monkeypatch):
    browser = _FakeBrowser()
    _install(monkeypatch, "cloakbrowser", launch=lambda **kw: browser)
    with eng.build_factory(eng.Engine.CLOAKBROWSER)(headless=True):
        pass
    assert browser.closed == 1, "退出上下文时必须关掉浏览器"


def test_cloakbrowser_factory_propagates_import_error(monkeypatch):
    """缺依赖必须以 ImportError 冒出去（由上层翻译成 NOT_CONFIGURED + 安装提示）。"""
    # sys.modules 里设为 None → 下次 import 直接抛 ImportError（作用域干净，
    # 不会像 patch builtins.__import__ 那样污染 pytest 自己的报告路径）
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    with pytest.raises(ImportError):
        eng.build_factory(eng.Engine.CLOAKBROWSER)(headless=True)


def test_wrapped_browser_survives_closer_failure(monkeypatch):
    """关闭失败不能盖住真正的错误（__exit__ 必须吞掉 closer 的异常）。"""
    def bad_launch(**kw):
        b = _FakeBrowser()
        b.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))  # type: ignore[method-assign]
        return b

    _install(monkeypatch, "cloakbrowser", launch=bad_launch)
    with eng.build_factory(eng.Engine.CLOAKBROWSER)(headless=True):
        pass          # 不抛 = 通过


def test_wrapped_browser_handles_no_closer():
    """closer 为 None（某些引擎没有 close）也必须能进出上下文。"""
    b = _FakeBrowser()
    with eng._WrappedBrowser(b, None) as inner:
        assert inner is b


# ── patchright 工厂 ─────────────────────────────────


def _fake_playwright(monkeypatch, *, launch_error: Exception | None = None):
    calls = {"stop": 0, "launch": 0}

    class _PW:
        chromium = types.SimpleNamespace()

        def start(self):
            return self

        def stop(self):
            calls["stop"] += 1

    def launch(**kwargs):
        calls["launch"] += 1
        if launch_error is not None:
            raise launch_error
        return _FakeBrowser()

    module = _install(monkeypatch, "patchright.sync_api", sync_playwright=lambda: _PW())
    module_ok = _PW()
    _PW.chromium.launch = staticmethod(launch)
    # sync_playwright 每次返回同一份实例，便于断言
    monkeypatch.setattr(module, "sync_playwright", lambda: module_ok)
    return calls


def test_patchright_factory_launches_and_stops(monkeypatch):
    calls = _fake_playwright(monkeypatch)
    with eng.build_factory(eng.Engine.PATCHRIGHT)(headless=True) as b:
        assert isinstance(b, _FakeBrowser)
    assert calls["launch"] == 1
    assert calls["stop"] == 1, "退出时必须 stop()，否则 playwright 进程泄漏"


def test_patchright_stops_playwright_when_launch_fails(monkeypatch):
    """launch 失败必须**先 stop()** 再抛 —— 否则每失败一次泄漏一个 driver 进程。"""
    calls = _fake_playwright(monkeypatch, launch_error=RuntimeError("launch failed"))
    with pytest.raises(RuntimeError):
        eng.build_factory(eng.Engine.PATCHRIGHT)(headless=True)
    assert calls["stop"] == 1


# ── camoufox 工厂（只构造，不启动）────────────────────


def test_camoufox_factory_passes_persona(monkeypatch):
    """persona 必须进 kwargs —— 它是"固定 OS 指纹"的唯一入口。"""
    seen = {}

    class FakeCamoufox:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    _install(monkeypatch, "camoufox.sync_api", Camoufox=FakeCamoufox)
    eng.build_factory(eng.Engine.CAMOUFOX, os_persona="windows")(headless=True)
    assert seen["os"] == ["windows"]
    assert seen["headless"] is True
    assert seen["geoip"] is False


def test_camoufox_factory_omits_persona_when_empty(monkeypatch):
    seen = {}

    class FakeCamoufox:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    _install(monkeypatch, "camoufox.sync_api", Camoufox=FakeCamoufox)
    eng.build_factory(eng.Engine.CAMOUFOX)(headless=True)
    assert "os" not in seen, "没指定 persona 时不该塞 os 参数（让 Camoufox 自己随机）"


# ── 兜底分支 ────────────────────────────────────────


def test_build_factory_rejects_unknown_engine():
    with pytest.raises(ValueError) as ei:
        eng.build_factory("selenium")
    assert "selenium" in str(ei.value)


@pytest.mark.parametrize("module,engine", [
    ("cloakbrowser", eng.Engine.CLOAKBROWSER),
    ("patchright.sync_api", eng.Engine.PATCHRIGHT),
])
def test_preflight_reports_install_hint(monkeypatch, module, engine):
    """缺引擎时必须给**可执行的安装命令**，不能只报一句 ImportError。"""
    monkeypatch.setitem(sys.modules, module, None)
    with pytest.raises(Exception) as ei:
        eng.preflight(engine)
    msg = str(ei.value)
    assert engine.value in msg or "not-configured" in msg
    assert "pip install" in msg or getattr(ei.value, "hint", ""), "必须给安装提示"
