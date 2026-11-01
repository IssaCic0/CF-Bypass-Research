"""缓存层测试（落盘 / 加密 / 原子写 / 过期 / 上限 / 跨进程共享）。

跑法：python -m pytest tests/test_cache.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import cache as cache_mod  # noqa: E402

TOKEN = "unit-test-token"


def cache_files(d):
    """只数缓存文件，排除 salt.bin（盐不是缓存项）。"""
    return [p for p in d.glob("*.bin") if p.name != "salt.bin"]


def entry(ttl: int = 3600, **extra):
    base = {
        "ok": True,
        "solved": True,
        "cf_clearance": "SECRET_CLEARANCE_VALUE",
        "user_agent": "UA/1.0",
        "cookies": {"cf_clearance": "SECRET_CLEARANCE_VALUE"},
        "cookie_list": [{"name": "cf_clearance", "value": "SECRET_CLEARANCE_VALUE", "domain": ".e.com"}],
        "expires_at": time.time() + ttl,
        "egress_id": "default",
    }
    base.update(extra)
    return base


# ── MemoryStore ─────────────────────────────────────
def test_memory_store_roundtrip_and_expiry():
    s = cache_mod.MemoryStore()
    s.set("k", entry())
    assert s.get("k")["cf_clearance"] == "SECRET_CLEARANCE_VALUE"

    s.set("expired", entry(ttl=-1))
    assert s.get("expired") is None, "过期项必须当作未命中"


def test_memory_store_has_hard_limit():
    s = cache_mod.MemoryStore(max_items=3)
    for i in range(10):
        s.set(f"k{i}", entry())
    assert len(s) <= 3, "内存缓存也必须有上限，长跑进程不能无限增长"


# ── FileStore ───────────────────────────────────────
def test_filestore_roundtrip(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN)
    s.set("host|default", entry())
    got = s.get("host|default")
    assert got is not None and got["cf_clearance"] == "SECRET_CLEARANCE_VALUE"


def test_filestore_does_not_leak_plaintext(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN)
    s.set("host|default", entry())
    blob = b"".join(p.read_bytes() for p in cache_files(tmp_path))
    assert b"SECRET_CLEARANCE_VALUE" not in blob, "落盘内容必须加密，strings 不应看到 clearance"
    assert b"cf_clearance" not in blob


def test_filestore_token_change_invalidates_all(tmp_path):
    a = cache_mod.FileStore(str(tmp_path), TOKEN)
    a.set("host|default", entry())
    b = cache_mod.FileStore(str(tmp_path), "another-token")
    assert b.get("host|default") is None, "换 token 必须作废旧缓存"
    # 而且不能抛异常，只是未命中
    assert b.get("host|default") is None


def test_filestore_corrupt_file_is_treated_as_miss_and_cleaned(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN)
    s.set("host|default", entry())
    # 必须排除 salt.bin：它也匹配 *.bin，Linux 上 glob 顺序会先返回它，
    # 于是这条用例实际是把盐弄坏、而不是把缓存条目弄坏（CI 在 Linux 上抓到的真 bug）。
    files = cache_files(tmp_path)
    assert len(files) == 1
    target = files[0]
    target.write_bytes(b"not-a-fernet-token")
    assert s.get("host|default") is None
    assert not target.exists(), "坏文件应被清理"


def test_filestore_expired_entry_is_miss_and_removed(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN)
    s.set("host|default", entry(ttl=-1))
    assert s.get("host|default") is None
    assert len(cache_files(tmp_path)) == 0


def test_filestore_max_files_evicts_oldest(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN, max_files=3)
    for i in range(6):
        s.set(f"h{i}|default", entry())
        time.sleep(0.01)
    assert len(cache_files(tmp_path)) <= 3


def test_filestore_purge_expired_counts(tmp_path):
    s = cache_mod.FileStore(str(tmp_path), TOKEN)
    s.set("a|d", entry(ttl=-1))
    s.set("b|d", entry())
    assert s.purge_expired() == 1
    assert s.get("b|d") is not None


def test_filestore_is_shared_across_processes(tmp_path):
    """真正的跨进程验证：子进程写，父进程读（多进程扩容的前提）。"""
    script = (
        "import sys, time;"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'solver')!r});"
        "import cache;"
        f"s = cache.FileStore({str(tmp_path)!r}, {TOKEN!r});"
        "s.set('shared|default', {'ok': True, 'solved': True, 'cf_clearance': 'FROM-CHILD',"
        " 'user_agent': 'UA', 'expires_at': time.time() + 600})"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr

    reader = cache_mod.FileStore(str(tmp_path), TOKEN)
    got = reader.get("shared|default")
    assert got is not None and got["cf_clearance"] == "FROM-CHILD", "子进程写入的缓存必须能被父进程读到"


# ── build_store 降级 ────────────────────────────────
def test_build_store_memory_when_no_dir():
    store, kind = cache_mod.build_store(None, TOKEN)
    assert kind == "memory" and isinstance(store, cache_mod.MemoryStore)


def test_build_store_defaults_to_sqlite_when_dir_given(tmp_path):
    """v4.0 起落盘缓存的默认实现是 SQLite（决策 D-2），FileStore 作为降级。"""
    store, kind = cache_mod.build_store(str(tmp_path), TOKEN)
    assert kind.startswith("sqlite:") and isinstance(store, cache_mod.SqliteStore)


def test_build_store_file_backend_is_still_available_as_escape_hatch(tmp_path):
    """`backend="file"` 必须仍然可用：SQLite 在某些共享盘上锁冲突时要有逃生门。"""
    store, kind = cache_mod.build_store(str(tmp_path), TOKEN, backend="file")
    assert kind.startswith("file:") and isinstance(store, cache_mod.FileStore)


def test_build_store_unknown_backend_is_not_silently_treated_as_auto(tmp_path):
    store, kind = cache_mod.build_store(str(tmp_path), TOKEN, backend="redis")
    assert isinstance(store, cache_mod.MemoryStore)
    assert "未知 cache backend" in kind, "配置写错必须说出来，不能静默当成 auto"


def test_build_store_falls_back_loudly_on_bad_dir(tmp_path):
    bad = tmp_path / "afile"
    bad.write_text("x", encoding="utf-8")   # 目录位置被文件占了
    store, kind = cache_mod.build_store(str(bad), TOKEN)
    assert isinstance(store, cache_mod.MemoryStore)
    assert "fallback" in kind, "降级必须把原因说清楚，不能静默"


# ── 与 Solver 的接线 ────────────────────────────────
def test_solver_uses_injected_store_and_reports_backend():
    from test_service import FakeBrowser, factory_returning, wait_until  # 复用替身

    service = pytest.importorskip("service")
    store = cache_mod.MemoryStore()
    s = service.Solver(browser_factory=factory_returning(FakeBrowser()),
                       allow_private_hosts=True, store=store)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve("https://example.com/", timeout=10)
        assert r["cached"] is False
        assert s.health()[1]["cache_backend"] == "injected"
        assert s.health()[1]["cached_domains"] == 1
        # 第二次必须命中注入的 store（而不是内部另存的 dict）
        assert s.solve("https://example.com/", timeout=10)["cached"] is True
        assert store.get(s.cache_key("example.com")) is not None
    finally:
        s.close()
