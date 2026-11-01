"""SQLite 缓存层测试（v4.0 T-36 / 决策 D-2）。

三条主线，全部是可复现的行为断言，不是"能不能 new 出来"的水测试：
  1. **契约等价**：同一套用例参数化跑 FileStore 与 SqliteStore，行为必须一致
     （这样换存储不会悄悄改变语义）；
  2. **多进程共享**：子进程写 → 父进程读到（这是"单机多进程 + 共享目录"的前提）；
  3. **历史坑回归**：purge 不能把盐删掉（盐一没，整个库都解不开）。

跑法：python -m pytest tests/test_cache_sqlite.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import cache as cache_mod  # noqa: E402

TOKEN = "unit-test-token"


def entry(ttl: float = 3600, clearance: str = "SECRET_CLEARANCE_VALUE", **extra):
    base = {
        "ok": True,
        "solved": True,
        "cf_clearance": clearance,
        "user_agent": "UA/1.0",
        "cookies": {"cf_clearance": clearance},
        "cookie_list": [{"name": "cf_clearance", "value": clearance, "domain": ".e.com"}],
        "expires_at": time.time() + ttl,
        "egress_id": "default",
    }
    base.update(extra)
    return base


@pytest.fixture(params=["file", "sqlite"])
def store(request, tmp_path):
    """同一套契约测试双跑两种落盘实现。"""
    kind = request.param
    if kind == "file":
        return cache_mod.FileStore(str(tmp_path), TOKEN)
    return cache_mod.SqliteStore(str(tmp_path), TOKEN)


# ── 1. 契约等价 ─────────────────────────────────────
def test_store_roundtrip(store):
    store.set("shared|default", entry())
    got = store.get("shared|default")
    assert got is not None
    assert got["cf_clearance"] == "SECRET_CLEARANCE_VALUE"
    assert got["egress_id"] == "default"


def test_store_miss_returns_none(store):
    assert store.get("never-written|default") is None


def test_store_expired_entry_is_miss_and_removed(store):
    store.set("expired|default", entry(ttl=-1))
    assert store.get("expired|default") is None
    assert store.get("expired|default") is None, "过期项必须被删掉，不能每次都解密一遍"


def test_store_delete(store):
    store.set("k|default", entry())
    assert store.get("k|default") is not None
    store.delete("k|default")
    assert store.get("k|default") is None


def test_store_purge_expired_counts(store):
    store.set("alive|default", entry())
    # 用"先活着、后过期"的两条：SqliteStore 在 set 时就会顺手清掉已过期的，
    # 如果直接用 ttl=-1 写入，两种实现的行为会不同（这是实现差异，不是缺陷）。
    store.set("dead1|default", entry(ttl=0.05))
    store.set("dead2|default", entry(ttl=0.05))
    time.sleep(0.12)
    assert store.purge_expired() >= 2
    assert store.get("alive|default") is not None
    assert store.get("dead1|default") is None


def test_store_items_reports_live_entries_only(store):
    store.set("a|default", entry(clearance="A"))
    store.set("b|default", entry(ttl=-1))
    items = dict(store.items())
    values = [v.get("cf_clearance") for v in items.values()]
    assert "A" in values
    assert len(values) == 1, f"过期项不该出现在 items 里: {values}"


def test_store_limit_evicts_oldest_by_expiry(tmp_path):
    """上限淘汰按"最早失效"而不是文件 mtime —— 这是 SQLite 方案相对 FileStore 的核心差异。

    只对 SqliteStore 断言：FileStore 的语义就是 mtime，改它等于改既有行为。
    """
    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN, max_files=3)
    # 写 4 条，expires_at 递增
    for i in range(4):
        svc.set(f"k{i}|default", entry(ttl=100 + i, clearance=f"C{i}"))
    live = [v["cf_clearance"] for _, v in svc.items()]
    assert len(live) <= 3, f"超过上限必须淘汰，实际 {len(live)} 条"
    assert "C0" not in live, "最早失效的那条应先被淘汰"
    assert "C3" in live, "最新写入的不能被淘汰"


def test_store_encrypts_at_rest(store, tmp_path):
    """磁盘上不能出现明文凭证（这是 C-2 决策的底线）。"""
    store.set("secret|default", entry(clearance="PLAINTEXT_MARKER_12345"))
    for p in tmp_path.rglob("*"):
        if p.is_file():
            raw = p.read_bytes()
            assert b"PLAINTEXT_MARKER_12345" not in raw, f"{p} 里出现了明文凭证"


def test_store_wrong_token_cannot_decrypt(tmp_path):
    """换 token = 作废旧缓存（密钥由 token 派生），且失败必须表现为 miss 而不是抛异常。"""
    kind_cls = cache_mod.SqliteStore
    a = kind_cls(str(tmp_path), "token-A")
    a.set("k|default", entry())
    b = kind_cls(str(tmp_path), "token-B")
    assert b.get("k|default") is None


# ── 2. 多进程共享 ───────────────────────────────────
def test_sqlite_cross_process_visibility(tmp_path):
    code = (
        "import sys, time;"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'solver')!r});"
        "import cache;"
        f"s = cache.SqliteStore({str(tmp_path)!r}, {TOKEN!r});"
        "s.set('shared|default', {'cf_clearance': 'FROM-CHILD', 'expires_at': time.time()+600,"
        " 'egress_id': 'default'});"
        "print('ok')"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, f"子进程失败: {out.stderr}"

    reader = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    got = reader.get("shared|default")
    assert got is not None and got["cf_clearance"] == "FROM-CHILD", \
        "子进程写入的缓存必须能被父进程读到（多进程共享的前提）"


def test_sqlite_write_survives_concurrent_lock(tmp_path):
    """别的进程持着写锁时，set 应在 busy_timeout 内重试成功，而不是立刻抛异常。"""
    import sqlite3
    import threading

    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    # check_same_thread=False：这条连接是"另一个进程持锁"的替身，要在子线程里释放
    holder = sqlite3.connect(str(tmp_path / cache_mod.SQLITE_FILENAME), timeout=1.0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")

    def release_later():
        time.sleep(0.2)
        holder.rollback()
        holder.close()

    threading.Thread(target=release_later, daemon=True).start()

    started = time.time()
    svc.set("locked|default", entry())
    elapsed = time.time() - started

    assert svc.get("locked|default") is not None, "锁释放后写入必须成功"
    assert elapsed < cache_mod.SQLITE_BUSY_TIMEOUT_MS / 1000.0, "不应耗尽整个 busy_timeout"


# ── 3. 历史坑回归 ───────────────────────────────────
def test_sqlite_purge_keeps_salt_file(tmp_path):
    """盐文件是解密前提，purge 绝不能把它当缓存删掉（FileStore 时代踩过这个坑）。"""
    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    svc.set("k|default", entry(ttl=-1))
    svc.purge_expired()
    assert (tmp_path / cache_mod.SALT_FILENAME).exists(), "purge 把盐删了 → 之后所有缓存都解不开"

    # 盐还在的前提：同一目录新建实例仍能读到之前写入的活条目
    svc.set("live|default", entry())
    again = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    assert again.get("live|default") is not None


def test_sqlite_host_stats_are_counts_only(tmp_path):
    """按域统计只放计数，不放任何凭证（/metrics 的数据来源）。"""
    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    svc.record_solve("a.example.com", ok=True)
    svc.record_solve("a.example.com", ok=False)
    svc.record_solve("b.example.com", ok=True)
    stats = {row["host"]: row for row in svc.host_stats()}
    assert stats["a.example.com"]["solves"] == 2
    assert stats["a.example.com"]["failures"] == 1
    assert stats["b.example.com"]["solves"] == 1

    raw = (tmp_path / cache_mod.SQLITE_FILENAME).read_bytes()
    assert b"SECRET_CLEARANCE_VALUE" not in raw, "统计表与凭证表分离，库里不能有明文凭证"


# ── 4. 多进程首启竞态（本轮实测到的真实 bug 的回归保护）─────────────
_SALT_RACE_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1])
import cache
store = cache.SqliteStore(sys.argv[2], "race-token", max_files=32)
store.set("child" + sys.argv[3] + "|default",
          {"cf_clearance": "CHILD-" + sys.argv[3], "expires_at": time.time() + 600,
           "egress_id": "default"})
print("ok")
"""


def test_concurrent_first_boot_shares_one_salt(tmp_path):
    """N 个进程同时首启同一个空缓存目录，必须全部成功且拿到同一份盐。

    这是本轮 loadtest burst 阶段抓到的真实 bug：
    原实现用「写临时文件 + os.replace」建盐，Windows 上两个进程同时 replace
    到同一个目标会抛 `PermissionError: [WinError 5]`，4 worker 同时首启约有 1/4
    概率有一个进程起不来（或静默降级成内存缓存，跨进程共享直接失效）。

    断言方式：每个子进程写自己那条凭证；父进程能**全部解密读出** ——
    只有"所有进程派生出同一密钥"才可能。
    """
    solver_dir = str(Path(__file__).resolve().parents[1] / "solver")
    n = 6
    procs = [
        subprocess.Popen([sys.executable, "-c", _SALT_RACE_CHILD, solver_dir, str(tmp_path), str(i)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for i in range(n)
    ]
    failures = []
    for i, p in enumerate(procs):
        out, err = p.communicate(timeout=120)
        if p.returncode != 0:
            failures.append(f"child{i}: {err.strip()[-300:]}")
    assert not failures, "并发首启有子进程失败（盐创建竞态）:\n" + "\n".join(failures)

    reader = cache_mod.SqliteStore(str(tmp_path), "race-token")
    missing = [i for i in range(n) if reader.get(f"child{i}|default") is None]
    assert not missing, f"这些子进程写入的条目父进程解不开（说明盐不同源）: {missing}"


# ── 5. 降级与损坏容错（把"坏数据不该打断服务"这条底线钉住）──────────
def test_stats_for_all_three_backends(tmp_path):
    mem = cache_mod.MemoryStore(max_items=8)
    mem.set("k|default", entry())
    assert mem.stats()["backend"] == "memory" and mem.stats()["entries"] == 1

    fil = cache_mod.FileStore(str(tmp_path / "f"), TOKEN)
    fil.set("k|default", entry())
    assert fil.stats()["backend"] == "file" and fil.stats()["entries"] == 1

    sq = cache_mod.SqliteStore(str(tmp_path / "s"), TOKEN)
    sq.set("k|default", entry())
    assert sq.stats()["backend"] == "sqlite" and sq.stats()["entries"] == 1


def test_sqlite_corrupted_blob_is_miss_and_removed(tmp_path):
    """坏行（被外部工具改过 / 换 token 后的残留）必须表现为 miss，而不是抛异常。"""
    import sqlite3

    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    svc.set("bad|default", entry())
    conn = sqlite3.connect(str(tmp_path / cache_mod.SQLITE_FILENAME))
    conn.execute("UPDATE entries SET value = ? WHERE key = ?", (b"not-a-fernet-token", "bad|default"))
    conn.commit()
    conn.close()

    assert svc.get("bad|default") is None
    assert len(svc) == 0, "坏行应被删掉，不能每次读都失败一次"


def test_sqlite_items_skips_expired_and_corrupt(tmp_path):
    import sqlite3

    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    svc.set("live|default", entry(clearance="LIVE"))
    svc.set("old|default", entry(ttl=0.05, clearance="OLD"))
    time.sleep(0.1)
    conn = sqlite3.connect(str(tmp_path / cache_mod.SQLITE_FILENAME))
    conn.execute(
        "INSERT INTO entries (key, host, egress, backend, value, expires_at, created_at) "
        "VALUES (?, '', '', '', ?, ?, ?)",
        ("corrupt|default", b"garbage", time.time() + 600, time.time()),
    )
    conn.commit()
    conn.close()

    values = [v.get("cf_clearance") for _, v in svc.items()]
    assert values == ["LIVE"], f"items 只能返回活着且能解密的条目，实际 {values}"


def test_sqlite_close_and_reconnect(tmp_path):
    svc = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    svc.set("k|default", entry())
    svc.close()
    svc.close()                      # 幂等：重复 close 不许抛
    assert svc.get("k|default") is not None, "close 之后再用应当自动重连"


def test_partial_salt_file_is_repaired(tmp_path):
    """半成品盐（0 字节）会让服务永远起不来 —— 必须能自愈。"""
    (tmp_path / cache_mod.SALT_FILENAME).write_bytes(b"")
    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.set("k|default", entry())
    assert store.get("k|default") is not None
    assert (tmp_path / cache_mod.SALT_FILENAME).stat().st_size >= 16


def test_build_store_memory_backend_explicit(tmp_path):
    store, kind = cache_mod.build_store(str(tmp_path), TOKEN, backend="memory")
    assert kind == "memory" and isinstance(store, cache_mod.MemoryStore)
