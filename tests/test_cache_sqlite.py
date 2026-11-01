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


def test_sqlite_store_uses_single_shared_connection(tmp_path):
    """全进程只允许建**一条** SQLite 连接。

    这是实测抓到的句柄泄漏的回归保护：thread-local 时每条新 TCP 连接都会新建一条
    SQLite 连接（WAL 下 3 个文件句柄），而 `/health` 每个请求都读 `cached_domains`
    → 1 小时 soak 泄漏 1308 个内核句柄，线程数却不变。
    """
    import threading

    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)

    def work(i: int) -> None:
        for _ in range(5):
            store.set(f"k{i}|default", entry(clearance=f"C{i}"))
            store.get(f"k{i}|default")
            len(store)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert store._conn_creations == 1, (
        f"应当只建 1 条共享连接，实际建了 {store._conn_creations} 条"
        "（thread-local 会在每个 handler 线程各建一条 → 句柄泄漏）")
    assert len(store) == 16


def test_sqlite_close_then_reopen_creates_new_connection(tmp_path):
    """close() 之后再用要能自动重连（且计数如实增加）。"""
    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.set("k|default", entry())
    store.close()
    assert len(store) == 1
    assert store._conn_creations == 2


def test_concurrent_reads_and_writes_never_corrupt_rows(tmp_path):
    """并发读写共用一条连接时，**读**路径也必须进锁。

    为什么（v4.7 实测）：v4.2 把 thread-local 连接改成共享连接，修掉了句柄泄漏
    （每条新 TCP 连接 +3 句柄）。但当时只给**写**路径加了 `_conn_lock`，
    读路径的注释写的是"靠 pysqlite 内部互斥"——**这不成立**：
    pysqlite 只把单次 C 调用串行化，两条线程在同一条连接上各自 `execute()` 后，
    **游标状态是共享的**，`fetchone()` 会互相踩。实测两种表现：

      · `InterfaceError('bad parameter or other API misuse')`
      · `fetchone()` 拿到**别的语句**的行 → 本用例里就是 `expires_at=None`
        → `float(None)` → TypeError（`solver/cache.py::get` 里没有被捕获）

    在服务里的后果：并发 `/health`（读 `len`）+ `/solve`（读写）时缓存命中路径偶发异常，
    极端情况下**可能读到别的键的条目**（这里用 cf_clearance 值断言，读错就直接失败）。

    这条用例原来的兄弟 `test_sqlite_store_uses_single_shared_connection` 抓不到它 ——
    线程里的异常只变成 `PytestUnhandledThreadExceptionWarning`，**用例照样绿**。
    """
    import threading

    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    errors: list[str] = []

    def work(i: int) -> None:
        try:
            for _ in range(20):
                store.set(f"k{i}|default", entry(clearance=f"C{i}"))
                got = store.get(f"k{i}|default")
                assert got is None or got.get("cf_clearance") == f"C{i}", (
                    f"读到了别的键的条目：期望 C{i}，实际 {got!r}")
                len(store)
                store.items()
                store.host_stats()
        except BaseException as e:  # noqa: BLE001 —— 线程里的异常必须被抓出来，否则又变成 warning
            errors.append(f"thread {i}: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "线程没跑完（可能死锁）"
    assert not errors, f"并发读写出现 {len(errors)} 次异常，前 3 条：{errors[:3]}"
    assert len(store) == 16


def test_wal_switch_retries_when_database_is_locked(tmp_path, monkeypatch):
    """切 WAL 撞上 `database is locked` 时必须**重试**，而不是让服务起不来。

    背景（v4.7）：`PRAGMA journal_mode=WAL` 拿不到锁时**不走 busy handler**，
    直接抛 OperationalError。实测 `test_concurrent_first_boot_shares_one_salt`
    的 6 个子进程同时首启同一个空目录，约 1/10 概率有一个子进程在这一步起不来
    ——用 compose 就是"3 个 worker 冷启动时偶尔有一个反复重启"。

    ⚠️ 诚实说明：这条竞态本身**没能**被确定性复现（同进程持锁、跨进程持锁、
    90 次裸构造都没稳定命中），所以这里 mock 出真实的 SQLite 错误，
    只验证"重试逻辑存在且有效"。真实竞态靠降低概率 + 压力观察（见 §7 N-15）。
    """
    import sqlite3

    calls = {"n": 0}
    real = cache_mod._try_set_wal

    def flaky(conn):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(conn)

    monkeypatch.setattr(cache_mod, "_try_set_wal", flaky)
    monkeypatch.setattr(cache_mod, "WAL_RETRY_DELAY", 0.01)

    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    assert calls["n"] >= 3, f"没有重试：只尝试了 {calls['n']} 次"
    assert store._journal_mode == "wal", f"重试后仍未进 WAL：{store._journal_mode!r}"
    store.set("k|default", entry())
    assert store.get("k|default") is not None


def test_wal_degradation_is_loud_but_usable(tmp_path, monkeypatch, capsys):
    """切不过去时降级，**必须可用且必须留痕**。

    场景限定为"可写但不支持 WAL 的文件系统"（网络卷 / 某些容器卷 / NFS）。
    ⚠️ **只读挂载不在降级保障内**：那种情况下 `CREATE TABLE`、`commit()`、
    `_harden_file()` 全都写不了，服务本来就起不来 —— 别把它当成本用例覆盖的场景
    （上一版注释这么写是错的，独立审查抓到了）。
    """
    import sqlite3

    def always_locked(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cache_mod, "_try_set_wal", always_locked)
    monkeypatch.setattr(cache_mod, "WAL_RETRY_ATTEMPTS", 3)
    monkeypatch.setattr(cache_mod, "WAL_RETRY_DELAY", 0.01)

    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.set("k|default", entry())
    assert store.get("k|default") is not None, "降级后缓存仍然必须可用"

    # 断言"stats 报的就是库里真实的模式"，而不是"stats 报的就是那个属性"——
    # 后者是恒等式（stats() 直接返回 self._journal_mode），什么都证明不了。
    real = str(store._conn().execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
    assert store.stats()["journal_mode"] == real, f"stats 报的模式与库里不一致：{store.stats()} vs {real}"
    # 把"现读"这件事钉死：篡改缓存的属性值后，stats() 必须仍然报库里的真实值。
    # 不做这一步的话，"stats() 现读"和"stats() 返回缓存属性"两种实现都能通过上面那条断言。
    store._journal_mode = "wal"
    assert store.stats()["journal_mode"] == real, "stats() 没有现读，仍在返回建连时的缓存值"
    store._journal_mode = real
    # 降级后必须是**已知**的 rollback 模式（delete/truncate/persist），不能是"未知空串"：
    # 否则"真降级"和"连模式都读不出来"会被混为一谈。
    assert real in ("delete", "truncate", "persist", "memory"), f"降级后的模式不可识别：{real!r}"
    # WAL 下的 NORMAL 在 rollback journal 下会削弱掉电安全，降级后必须回到 FULL
    assert int(store._conn().execute("PRAGMA synchronous").fetchone()[0]) == 2,         "降级后 synchronous 必须回到 FULL(2)"
    # 警告走 stderr：本服务的 stdout 是 JSON lines 契约，不许被人类可读文本污染
    captured = capsys.readouterr()
    assert "未能启用 WAL" in captured.err, "降级必须打印警告（不许静默）"
    assert "未能启用 WAL" not in captured.out, "警告不许写进 stdout（会污染 JSON lines 日志）"


def test_wal_permanent_error_does_not_retry_from_the_moon(tmp_path, monkeypatch):
    """永久性错误（不是锁竞争）必须立刻放弃，不能重试 25 次把启动拖 10 秒。

    `file is not a database` / `unable to open database file` / `disk I/O error`
    这类错误重试一万次也一样；拖 10 秒还会打出一条误导性的"并发下降"警告。
    """
    import sqlite3

    calls = {"n": 0}

    def permanent(conn):
        calls["n"] += 1
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(cache_mod, "_try_set_wal", permanent)
    monkeypatch.setattr(cache_mod, "WAL_RETRY_DELAY", 0.01)
    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.close()
    assert calls["n"] == 1, f"永久错误只该试 1 次，实际 {calls['n']} 次"


def test_read_paths_actually_take_the_connection_lock(tmp_path):
    """确定性机制用例：读路径必须**真的**取锁（不是靠并发跑一遍碰运气）。

    `test_concurrent_reads_and_writes_never_corrupt_rows` 是概率门 —— 线程切换若不落在
    execute 与 fetchone 之间，它照样绿。这条用例把"取锁"本身钉成断言：
    任何一条读路径被人把 `with self._conn_lock:` 删掉，这里立刻红。
    """
    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.set("k|default", entry())

    class SpyLock:
        """记录进入次数；行为完全委托给真锁。"""
        def __init__(self, inner):
            self.inner = inner
            self.entries = 0

        def __enter__(self):
            self.entries += 1
            return self.inner.__enter__()

        def __exit__(self, *exc):
            return self.inner.__exit__(*exc)

    spy = SpyLock(store._conn_lock)
    store._conn_lock = spy
    base = spy.entries

    store.get("k|default")      # 读 + _conn() 各一次
    store.items()
    len(store)
    store.stats()
    store.host_stats()

    # 每条读路径至少 2 次（自己的 with + _conn() 的 with）→ 5 条 ≥ 10
    assert spy.entries - base >= 10, (
        f"读路径没有全部取锁：只进入 {spy.entries - base} 次（期望 ≥10）")


def test_wal_silent_downgrade_is_still_loud_and_safe(tmp_path, monkeypatch, capsys):
    """`PRAGMA journal_mode=WAL` **不抛异常**、只原值返回时，也必须降级 + 告警 + synchronous=FULL。

    这是独立审查第二轮抓到的真洞（第一轮只修了 `except` 分支）：
    在部分文件系统（网络卷/NFS）、只读库或事务中，SQLite 会**原值返回**（如 "delete"）
    而不抛异常。若成功分支不校验返回值，就变成"静默降级"：
    rollback journal + `synchronous=NORMAL`（掉电安全被削弱）+ 一句警告都没有。
    """
    calls = {"n": 0}

    def returns_delete(conn):
        calls["n"] += 1
        return "delete"                     # 不抛异常，但也没切成 WAL

    monkeypatch.setattr(cache_mod, "_try_set_wal", returns_delete)
    store = cache_mod.SqliteStore(str(tmp_path), TOKEN)
    store.set("k|default", entry())
    assert store.get("k|default") is not None, "降级后仍然必须可用"

    assert calls["n"] == 1, f"返回值降级不该重试（这不是锁竞争），实际试了 {calls['n']} 次"
    assert store.stats()["journal_mode"] == "delete"
    assert int(store._conn().execute("PRAGMA synchronous").fetchone()[0]) == 2,         "降级后 synchronous 必须回到 FULL(2)"
    err = capsys.readouterr().err
    assert "未能启用 WAL" in err, "静默降级也必须告警"
