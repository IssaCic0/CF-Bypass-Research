"""全局测试夹具（v5.0 T-55 测试卫生）。

问题：一批用例构造 `SqliteStore` 后没有 `close()`，`ResourceWarning:
unclosed database` 一发 100+ 条，把 pytest 的 warning 区撑爆 —— 而 warning 区
是抓真信号的地方（N-15 就是从这里发现的），不能被资源噪音污染。

做法：autouse fixture 用 monkeypatch 跟踪测试期间创建的 `SqliteStore`
（以及 `FileStore`，为对称起见），teardown 时统一 `close()`。
不改任何用例本体；显式 `close()` 过的 store 重复 close 是无害的
（`SqliteStore.close` 幂等）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _close_tracked_sqlite_stores(monkeypatch):
    from solver import cache as cache_mod

    tracked: list[object] = []
    for cls_name in ("SqliteStore", "FileStore"):
        cls = getattr(cache_mod, cls_name, None)
        if cls is None:
            continue
        orig_init = cls.__init__

        def make_init(orig, bucket):
            def init(self, *args, **kwargs):
                orig(self, *args, **kwargs)
                bucket.append(self)
            return init

        monkeypatch.setattr(cls, "__init__", make_init(orig_init, tracked))

    yield

    for store in tracked:
        close = getattr(store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass  # teardown 清理失败不掩盖测试本身的结论
