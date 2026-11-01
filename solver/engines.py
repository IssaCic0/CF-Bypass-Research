"""浏览器引擎抽象：同一套求解逻辑，可切换三种"真实浏览器"。

为什么要这一层（而不是写死 Camoufox）：
  · 这三者的差异只在"浏览器二进制从哪来、指纹怎么改"，而我们的求解流程
    （导航 → 等/点 → 收 cookie/token）是同构的。把引擎抽出来，用户就能按自己的
    环境与许可证约束选一个，而不是被单一依赖绑死。

三条路线的定位（细节与实测见 docs/方案矩阵.md）：

| 引擎 | 指纹改动位置 | 安装 | 许可证/成本 | 备注 |
|---|---|---|---|---|
| `camoufox`（默认） | Firefox 的 C++ 实现层 | `pip install camoufox && python -m camoufox fetch` | MPL-2.0，免费 | 本仓库主力，真机 24/25 |
| `cloakbrowser` | Chromium 的 C++ 源码层（87 处补丁） | `pip install cloakbrowser`（**二进制按需下载**） | 包装 MIT，**二进制专有**，最新版需订阅 | 有免费档；不能随本仓库分发 |
| `patchright` | Chromium 的 JS/CDP 层（反检测 fork） | `pip install patchright && patchright install chromium` | Apache-2.0，免费 | 指纹弱于上面两者，胜在部署简单 |

纪律：**引擎缺失时给可执行的安装提示（NOT_CONFIGURED），不要静默退回**——
静默退回会让用户以为在用 A，其实在跑 B，排查成本极高。
"""

from __future__ import annotations

from enum import Enum


class Engine(str, Enum):
    CAMOUFOX = "camoufox"
    CLOAKBROWSER = "cloakbrowser"
    PATCHRIGHT = "patchright"

    @classmethod
    def parse(cls, raw: str | None) -> "Engine":
        value = (raw or "").strip().lower()
        if not value:
            return cls.CAMOUFOX
        for member in cls:
            if member.value == value:
                return member
        raise ValueError(f"未知 engine={raw!r}；可选：{[m.value for m in cls]}")


INSTALL_HINT = {
    Engine.CAMOUFOX: "pip install camoufox && python -m camoufox fetch",
    Engine.CLOAKBROWSER: (
        "pip install cloakbrowser —— 首次启动会下载专有 Chromium 二进制（最新版需在 "
        "cloakbrowser.dev 订阅；免费档可用旧版本），且**不可随本仓库分发**"
    ),
    Engine.PATCHRIGHT: "pip install patchright && patchright install chromium",
}


class _WrappedBrowser:
    """把不同来源的 browser 统一成"上下文管理器 → 可 new_context 的对象"。"""

    def __init__(self, browser, closer) -> None:
        self._browser = browser
        self._closer = closer

    def __enter__(self):
        return self._browser

    def __exit__(self, *exc):
        try:
            if self._closer is not None:
                self._closer()
        except Exception:  # noqa: BLE001 —— 关闭失败不该盖住真正的错误
            pass
        return False


_MODULE = {
    Engine.CAMOUFOX: "camoufox.sync_api",
    Engine.CLOAKBROWSER: "cloakbrowser",
    Engine.PATCHRIGHT: "patchright.sync_api",
}


def preflight(engine: Engine) -> None:
    """启动前确认引擎可用：缺依赖就直接给出安装命令，而不是等第一次求解才炸。"""
    import importlib

    module = _MODULE[engine]
    try:
        importlib.import_module(module)
    except ImportError as e:
        from errors import ErrorCode, SolveError  # 延迟导入，避免循环依赖

        raise SolveError(
            ErrorCode.NOT_CONFIGURED,
            f"引擎 {engine.value} 不可用：{e}",
            stage="admit",
            hint=INSTALL_HINT[engine],
        ) from e


def build_factory(engine: Engine, *, os_persona: str = ""):
    """返回一个 `factory(*, headless) -> 上下文管理器`，供 Solver 注入。

    返回的 browser 必须支持 `new_context()` / `is_connected()`（Playwright 接口），
    且 context 支持 `route()` / `cookies()` / `new_page()` —— 三条引擎都满足。
    """
    if engine is Engine.CAMOUFOX:
        return _camoufox_factory(os_persona)
    if engine is Engine.CLOAKBROWSER:
        return _cloakbrowser_factory()
    if engine is Engine.PATCHRIGHT:
        return _patchright_factory()
    raise ValueError(f"不支持的引擎：{engine}")


def _camoufox_factory(os_persona: str):
    def factory(*, headless: bool):
        from camoufox.sync_api import Camoufox   # 延迟导入：未装也不影响其它引擎

        kwargs = {"headless": headless, "geoip": False}
        if os_persona:
            kwargs["os"] = [os_persona]
        return Camoufox(**kwargs)

    return factory


def _cloakbrowser_factory():
    def factory(*, headless: bool):
        try:
            from cloakbrowser import launch
        except ImportError:  # 由上层翻译成 NOT_CONFIGURED
            raise
        # cloakbrowser.launch() 直接返回 Playwright Browser（drop-in 替换）
        browser = launch(headless=headless)
        closer = getattr(browser, "close", None)
        return _WrappedBrowser(browser, closer)

    return factory


def _patchright_factory():
    def factory(*, headless: bool):
        from patchright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=headless)
        except Exception:
            pw.stop()
            raise

        def closer():
            try:
                browser.close()
            finally:
                pw.stop()

        return _WrappedBrowser(browser, closer)

    return factory
