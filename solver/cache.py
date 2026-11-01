"""求解结果的缓存层。

为什么要单独一层：
  · 原实现是进程内 `dict`：重启即失、跨进程不共享。而"单机多进程扩容"的前提正是
    **同一域的 clearance 全局只解一次**（CF 按域+IP+UA 绑定，缓存语义天然正确）。
  · 多进程各自为政时，4 个 worker 会把同一个域解 4 次 —— 吞吐数字好看，实际是重复劳动。

两种实现：
  · `MemoryStore`：默认，单进程用。保留它是因为一旦落盘目录不可用必须能降级。
  · `FileStore`：落盘 + 加密 + 原子写 + 上限淘汰，供多进程共享。
  · `SqliteStore`：落盘 + 加密 + **WAL 事务** + TTL 索引淘汰，供多进程共享（v4.0 起的默认落盘实现）。

为什么 2026-11 又加了 SQLite（决策 D-2，理由全是实测出来的成本）：
  · FileStore 的上限淘汰按 **mtime 最旧** 来删，多进程共享同一目录时语义模糊：
    进程 A 可能把进程 B 刚写好的热条目删掉（各进程只数自己的写次数，实际上限是 N 倍）；
  · `purge_expired()` 要遍历目录并逐个解密，O(n) 次 stat + PBKDF2 解密，4096 个文件时是几百毫秒；
    而它跑在**求解完成后**的同步路径上；
  · 拿不到"每域求解次数/失败率"这类运维数据，而这正是 W3 观测升级要的东西。
SQLite 一次解决三条：TTL 索引（O(log n) 淘汰）、单事务原子性、可查询的统计表。
零新依赖（stdlib `sqlite3`），加密仍由应用层 Fernet 完成 —— 不把加密推给数据库。

安全取向（按既有决策 C-2）：
  · 落盘内容用 Fernet 加密，密钥由服务端 token 经 PBKDF2 派生 —— 换 token 自动作废旧缓存。
  · 文件权限 0600、目录 0700；`strings` 不应看到 `cf_clearance=`。
  · 读失败 / 解密失败 / schema 不符 → 视为未命中并删条目，绝不抛异常打断服务。

并发取向（禁区：不加文件锁）：
  · 写入用「同目录临时文件 + os.replace」原子替换，读者要么看到旧值要么看到新值。
  · 不做跨进程互斥：极端情况下两个进程同时解同一域，代价是多烧一次浏览器，
    比引入 flock/msvcrt 的跨平台坑更划算。
  · SqliteStore 靠 WAL + busy_timeout 处理多进程并发写；并发冲突时**降级不报错**。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
from typing import Any, Iterable, Protocol

SCHEMA_VERSION = 1
SALT_FILENAME = "salt.bin"
PBKDF2_ITERATIONS = 200_000
SQLITE_FILENAME = "cache.sqlite3"
#: 多进程写冲突时最多等多久（毫秒）。超过就抛，由调用方降级——不能无限等。
SQLITE_BUSY_TIMEOUT_MS = 5000
#: 切 WAL 的重试预算：25 × 0.2~0.6s ≈ 最长 10s。冷启动时这点等待远好过"起不来"。
WAL_RETRY_ATTEMPTS = 25
WAL_RETRY_DELAY = 0.2


def _try_set_wal(conn) -> str:
    """单次尝试把连接切到 WAL，返回实际生效的 journal 模式。

    抽成模块级函数只为一件事：让"重试逻辑"可以被用例单独验证
    （`_enable_wal` 会反复调它；用例可以把这个函数换成"前两次抛
    `database is locked`"而不去真的制造跨进程竞态）。
    """
    return str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] or "").lower()


class CacheStore(Protocol):
    """缓存存储协议。`key` 已由调用方算好（含域名+出口标识）。

    `set` 的可选元数据（host/backend）只用于统计与按域排查，不参与命中判定；
    调用方不传时不影响正确性。
    """

    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict, *, host: str = "", backend: str = "") -> None: ...
    def delete(self, key: str) -> None: ...
    def items(self) -> Iterable[tuple[str, dict]]: ...
    def purge_expired(self) -> int: ...


class MemoryStore:
    """进程内缓存（默认/降级用）。有上限，避免长跑进程无限增长。"""

    def __init__(self, max_items: int = 4096) -> None:
        self._data: dict[str, dict] = {}
        self._max = max_items

    def get(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.get("expires_at", 0) <= time.time():
            self._data.pop(key, None)
            return None
        return entry

    def set(self, key: str, value: dict, *, host: str = "", backend: str = "") -> None:
        self._data[key] = value
        while len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def items(self) -> Iterable[tuple[str, dict]]:
        return list(self._data.items())

    def purge_expired(self) -> int:
        now = time.time()
        gone = [k for k, v in self._data.items() if v.get("expires_at", 0) <= now]
        for k in gone:
            self._data.pop(k, None)
        return len(gone)

    def stats(self) -> dict:
        now = time.time()
        alive = sum(1 for v in self._data.values() if v.get("expires_at", 0) > now)
        return {"backend": "memory", "entries": alive, "max_entries": self._max}

    def __len__(self) -> int:
        return len(self._data)


class FileStore:
    """落盘缓存：加密 + 原子写 + 过期清理 + 数量上限。

    目录布局：`<cache_dir>/<sha256(key)[:32]>.bin`，内容为 Fernet 密文。
    """

    def __init__(self, cache_dir: str, token: str, max_files: int = 4096) -> None:
        self.dir = os.path.abspath(cache_dir)
        self._max_files = max_files
        os.makedirs(self.dir, exist_ok=True)
        _harden_dir(self.dir)
        self._fernet = _build_fernet(self.dir, token)
        # 近似计数：让大多数 set 不必 listdir+stat（那会落在求解完成后的同步路径上）。
        # 口径说明：它统计的是**本实例的写次数**（覆盖写也会 +1，属于高估）。
        # 多进程共享同一目录时，各进程只数自己的写，所以 --cache-max-files 的实际上限
        # 可能到 N 倍。清理只是空间优化，不影响正确性（过期项读取时仍会被判 miss 并删除）。
        self._count = len(self._list_files())

    # -- 协议实现 ----------------------------------
    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            blob = open(path, "rb").read()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(blob).decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 解密失败/坏文件一律当未命中并清理
            self._safe_unlink(path)
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            self._safe_unlink(path)
            return None
        if payload.get("expires_at", 0) <= time.time():
            self._safe_unlink(path)
            return None
        value = payload.get("value")
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict, *, host: str = "", backend: str = "") -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "expires_at": value.get("expires_at", 0),
            "created_at": time.time(),
            "value": value,
        }
        blob = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        path = self._path(key)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".bin")
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)      # 原子替换：读者不会看到半截文件
            tmp = None
            _harden_file(path)
        finally:
            if tmp:
                self._safe_unlink(tmp)
        self._count += 1
        if self._count > self._max_files:
            self._enforce_limit()

    def delete(self, key: str) -> None:
        self._safe_unlink(self._path(key))
        self._count = max(0, self._count - 1)

    def items(self) -> Iterable[tuple[str, dict]]:
        # 文件名是 key 的哈希，无法反推 key；对外只提供遍历值的能力，
        # 因此这里用文件名当 key 返回（调用方只关心 value）。
        #
        # 必须过滤过期项：`/health.cached_domains` 就是用 len(items()) 算的，
        # 把过期项算进去会让运维看到"缓存里有 200 个域"而实际一个都不可用
        # （SqliteStore 从一开始就过滤，两个实现必须一致）。
        out = []
        now = time.time()
        for name in self._list_files():
            path = os.path.join(self.dir, name)
            try:
                payload = json.loads(self._fernet.decrypt(open(path, "rb").read()).decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if payload.get("schema_version") != SCHEMA_VERSION:
                continue
            if payload.get("expires_at", 0) <= now:
                continue
            if isinstance(payload.get("value"), dict):
                out.append((name, payload["value"]))
        return out

    def purge_expired(self) -> int:
        gone = 0
        now = time.time()
        for name in self._list_files():
            path = os.path.join(self.dir, name)
            try:
                payload = json.loads(self._fernet.decrypt(open(path, "rb").read()).decode("utf-8"))
            except Exception:  # noqa: BLE001
                self._safe_unlink(path)
                gone += 1
                continue
            if payload.get("schema_version") != SCHEMA_VERSION or payload.get("expires_at", 0) <= now:
                self._safe_unlink(path)
                gone += 1
        self._count = len(self._list_files())
        return gone

    def __len__(self) -> int:
        return len(self._list_files())

    def stats(self) -> dict:
        return {"backend": "file", "entries": len(self._list_files()),
                "max_entries": self._max_files, "path": self.dir}

    # -- 内部 --------------------------------------
    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.dir, digest + ".bin")

    def _list_files(self) -> list[str]:
        """列出缓存文件。**必须排除盐文件**——否则 purge 会把它当坏文件删掉，
        进而导致所有缓存再也解不开（这个坑被 tests/test_cache.py 抓到过）。"""
        try:
            return [
                n
                for n in os.listdir(self.dir)
                if n.endswith(".bin") and not n.startswith(".tmp-") and n != SALT_FILENAME
            ]
        except OSError:
            return []

    def _enforce_limit(self) -> None:
        files = self._list_files()
        excess = len(files) - self._max_files
        if excess <= 0:
            return
        # 按 mtime 淘汰最旧的
        stamped = []
        for name in files:
            path = os.path.join(self.dir, name)
            try:
                stamped.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
        stamped.sort()
        for _, path in stamped[:excess]:
            self._safe_unlink(path)
        self._count = len(self._list_files())

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass


class SqliteStore:
    """落盘缓存（SQLite + WAL）：加密 + TTL 索引淘汰 + 多进程安全。

    与 FileStore 的接口完全一致，可互换（同一套 `tests/test_cache_sqlite.py` 双跑校验）。
    刻意**不做**旧 `.bin` 文件的迁移：cf_clearance 只有 30 分钟量级，
    重建的代价是"每域多解一次"，而迁移代码是一次性代码 + 一次性 bug。
    """

    def __init__(
        self,
        cache_dir: str,
        token: str,
        max_files: int = 4096,
        *,
        filename: str = SQLITE_FILENAME,
    ) -> None:
        self.dir = os.path.abspath(cache_dir)
        os.makedirs(self.dir, exist_ok=True)
        _harden_dir(self.dir)
        self.path = os.path.join(self.dir, filename)
        self._max = max(1, int(max_files))
        self._fernet = _build_fernet(self.dir, token)   # 复用同一套 salt.bin + PBKDF2
        # **一条共享连接 + 一把锁**，而不是 thread-local 每线程一条。
        # 实测教训：thread-local 时 HTTP 的每个连接都会新建一条 SQLite 连接
        # （WAL 下每条约占 3 个文件句柄：db / -wal / -shm），而 `/health` 每个请求都读
        # `cached_domains` → "每来一条新 TCP 连接就泄漏 3 个内核句柄"：
        # 1 小时 soak 涨了 1308 个，线程数却不变（线程退了，连接没关）。
        self._conn_lock = threading.RLock()
        self._conn_obj = None
        self._conn_creations = 0        # 供测试断言"只建过一次连接"
        self._journal_mode = ""         # 实际生效的 journal 模式（切 WAL 失败时≠wal）
        self._init_schema()

    # -- 连接管理 ----------------------------------
    def _conn(self):
        """**全进程共享一条连接**（`check_same_thread=False` + 自己的锁）。

        为什么不用 thread-local：HTTP handler 是**每连接一个线程**，
        thread-local 等于"每条 TCP 连接开一条 SQLite 连接"（WAL 下 3 个文件句柄），
        连接对象随线程死亡后句柄并不总被及时释放 —— 实测 1 小时泄漏 1308 个句柄。

        关于并发（v4.7 更正：上一版的注释在这里是**错的**）：
          · **读和写都必须走 `_conn_lock`**。曾经写的是"读路径只读、并发安全由 sqlite3
            内部互斥保证" —— 实测不成立：pysqlite 只把单次 C 调用串行化，两条线程在
            **同一条连接**上各自 execute() 之后**游标状态是共享的**，`fetchone()` 会互相踩。
            实测后果有两种：`InterfaceError('bad parameter or other API misuse')`，
            以及最坏的一种 —— `fetchone()` 拿到**别的语句**的行，即**别的键的条目**
            （问 k9 拿到 k10 的 cf_clearance）。凭证缓存串域是 P0。
            回归用例：`test_concurrent_reads_and_writes_never_corrupt_rows`
            （16 线程 × 20 轮，断言读到的 cf_clearance 必须是自己那条）。
          · 锁只覆盖到 fetch 完成（行已物化），解密/解析在锁外做，避免占锁太久。
          · 跨进程并发由 WAL + busy_timeout 处理（那是另一层问题，见 `_enable_wal`）。
        """
        import sqlite3

        with self._conn_lock:
            if self._conn_obj is None:
                conn = sqlite3.connect(self.path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
                                       check_same_thread=False)
                # busy_timeout 要在切 journal_mode **之前**生效（见 _enable_wal 的说明）。
                conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
                conn.execute("PRAGMA synchronous=NORMAL")    # WAL 下的合理折中
                self._journal_mode = self._enable_wal(conn)
                self._conn_obj = conn
                self._conn_creations += 1
            return self._conn_obj

    def _enable_wal(self, conn, attempts: int | None = None, delay: float | None = None) -> str:
        """把连接切到 WAL，带**有界重试**；仍然失败就退回当前模式（但不静默）。

        为什么需要重试：`PRAGMA journal_mode=WAL` 需要一次短暂独占锁，而它在拿不到锁时
        **不走 busy handler**，直接返回 `database is locked`。实测（v4.7）：
        `test_concurrent_first_boot_shares_one_salt` 的 6 个子进程同时首启同一个空目录，
        约 **1/10** 概率有一个子进程在这一步直接起不来 —— 等价于 compose 3 worker 冷启动。
        WAL 只是并发优化，不该成为"服务起不来"的原因。

        ⚠️ 诚实说明：这条竞态**没有**被用例确定性复现出来（试过同进程持锁、跨进程持锁、
        90 次裸构造，都没能稳定命中），所以回归用例只覆盖"重试逻辑本身"
        （`test_wal_switch_retries_when_database_is_locked`）。真实竞态靠降低概率 + 压力观察，
        见 `workflow_status.md` §7 N-15。
        """
        last = ""
        attempts = WAL_RETRY_ATTEMPTS if attempts is None else attempts
        delay = WAL_RETRY_DELAY if delay is None else delay
        for i in range(attempts):
            try:
                return _try_set_wal(conn)
            except Exception as e:  # noqa: BLE001 —— 只可能是 busy/locked/只读挂载
                last = f"{type(e).__name__}: {e}"
                time.sleep(delay * (1 + i % 3))
        print(f"[cfsolve] 警告：无法切到 WAL（{last}），已退回默认 journal 模式；"
              f"并发读性能下降，功能不受影响", flush=True)
        try:
            return str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
        except Exception:  # noqa: BLE001
            return ""

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (
              key        TEXT PRIMARY KEY,
              host       TEXT NOT NULL DEFAULT '',
              egress     TEXT NOT NULL DEFAULT '',
              backend    TEXT NOT NULL DEFAULT '',
              value      BLOB NOT NULL,
              expires_at REAL NOT NULL,
              created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_entries_expires ON entries(expires_at);
            CREATE INDEX IF NOT EXISTS idx_entries_host    ON entries(host);
            CREATE TABLE IF NOT EXISTS host_stats (
              host     TEXT PRIMARY KEY,
              solves   INTEGER NOT NULL DEFAULT 0,
              failures INTEGER NOT NULL DEFAULT 0
            );
            -- 跨进程"正在解这个域"的占位：解决"多个 worker 同时抢同一个新域"
            -- 时各解一次的问题（实测 4 进程 burst 会 4 次求解）。
            CREATE TABLE IF NOT EXISTS claims (
              key        TEXT PRIMARY KEY,
              owner      TEXT NOT NULL,
              expires_at REAL NOT NULL
            );
            """
        )
        conn.commit()
        _harden_file(self.path)

    # -- 协议实现 ----------------------------------
    def get(self, key: str) -> dict | None:
        # **读也要锁**（v4.7 实测的 P0）。
        # 曾经的理由是"写路径加锁、读路径靠 pysqlite 内部互斥"——这不成立：
        # pysqlite 只把单次 C 调用串行化，两条线程在**同一条连接**上各自 execute() 之后，
        # 游标状态是共享的，fetchone() 会互相踩。实测两种后果：
        #   1. InterfaceError('bad parameter or other API misuse')
        #   2. fetchone() 拿到**别的语句**的行 —— 最坏情况下是**别的键的条目**：
        #      实测问 `k9` 拿到了 `C10` 的 cf_clearance（凭证缓存串了域，P0）。
        # 锁只覆盖到 fetchone()（行已物化），解密/解析在锁外做，避免占锁太久。
        try:
            with self._conn_lock:
                row = self._conn().execute(
                    "SELECT value, expires_at FROM entries WHERE key = ?", (key,)
                ).fetchone()
        except Exception:  # noqa: BLE001 —— 锁冲突/库损坏都不该打断服务
            return None
        if row is None:
            return None
        blob, expires_at = row
        if not isinstance(expires_at, (int, float)):
            # 半行/坏行：NOT NULL 约束理论上挡得住，但既然共享连接能给出畸形行，
            # 这里就不能再假设它是数字（原来直接 float() 会抛 TypeError 冒到调用方）。
            self.delete(key)
            return None
        if float(expires_at) <= time.time():
            self.delete(key)
            return None
        try:
            payload = json.loads(self._fernet.decrypt(bytes(blob)).decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 解密失败（换 token / 坏行）一律当未命中并清理
            self.delete(key)
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            self.delete(key)
            return None
        value = payload.get("value")
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict, *, host: str = "", backend: str = "") -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "expires_at": value.get("expires_at", 0),
            "created_at": time.time(),
            "value": value,
        }
        blob = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        expires_at = float(value.get("expires_at", 0) or 0)
        egress = str(value.get("egress_id") or "")
        with self._conn_lock:
            conn = self._conn()
            conn.execute(
                """
                INSERT INTO entries (key, host, egress, backend, value, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  host = excluded.host, egress = excluded.egress, backend = excluded.backend,
                  value = excluded.value, expires_at = excluded.expires_at, created_at = excluded.created_at
                """,
                (key, host or "", egress, backend or "", blob, expires_at, time.time()),
            )
            conn.commit()
            self._prune(conn)

    def delete(self, key: str) -> None:
        """删除一条（**写**操作，走锁）。

        注意 `get()` 在"过期/解密失败"时也会调它 —— 那是无锁读路径里的一次写，
        所以这里必须自己加锁（RLock 可重入，不会因为调用方恰好持锁而死锁）。
        """
        try:
            with self._conn_lock:
                conn = self._conn()
                conn.execute("DELETE FROM entries WHERE key = ?", (key,))
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def items(self) -> Iterable[tuple[str, dict]]:
        out: list[tuple[str, dict]] = []
        try:
            # 同上：共享连接上的读必须进锁，否则 fetchall() 可能拿到别的语句的行。
            with self._conn_lock:
                rows = self._conn().execute(
                    "SELECT key, value, expires_at FROM entries").fetchall()
        except Exception:  # noqa: BLE001
            return out
        now = time.time()
        for key, blob, expires_at in rows:
            if float(expires_at) <= now:
                continue
            try:
                payload = json.loads(self._fernet.decrypt(bytes(blob)).decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if payload.get("schema_version") == SCHEMA_VERSION and isinstance(payload.get("value"), dict):
                out.append((str(key), payload["value"]))
        return out

    def purge_expired(self) -> int:
        try:
            with self._conn_lock:          # 写操作：与 set/try_claim 共用同一把锁
                conn = self._conn()
                cur = conn.execute("DELETE FROM entries WHERE expires_at <= ?", (time.time(),))
                conn.commit()
                return int(cur.rowcount or 0)
        except Exception:  # noqa: BLE001
            return 0

    def stats(self) -> dict:
        try:
            # 读路径同样必须进锁（共享连接 + 并发游标会串行/串数据，见 _conn 的说明）。
            with self._conn_lock:
                alive = self._conn().execute(
                    "SELECT COUNT(*) FROM entries WHERE expires_at > ?", (time.time(),)
                ).fetchone()[0]
        except Exception:  # noqa: BLE001
            alive = 0
        return {"backend": "sqlite", "entries": int(alive), "max_entries": self._max,
                "path": self.path, "journal_mode": self._journal_mode}

    def try_claim(self, key: str, ttl: float = 120.0, owner: str = "") -> bool:
        """跨进程抢占"我来解这个域"。**原子**（INSERT OR IGNORE），抢到返回 True。

        为什么需要：共享缓存只解决"先后到达"，4 个 worker **同时**抢同一个新域时会各解一次
        （实测 burst 12/12）。用一张 `claims` 表做抢占，抢不到的进程去等缓存，
        而不是各自启动浏览器。
        """
        now = time.time()
        owner = owner or f"pid{os.getpid()}"
        try:
            with self._conn_lock:
                conn = self._conn()
                conn.execute("DELETE FROM claims WHERE expires_at <= ?", (now,))
                cur = conn.execute(
                    "INSERT OR IGNORE INTO claims (key, owner, expires_at) VALUES (?, ?, ?)",
                    (key, owner, now + max(1.0, float(ttl))),
                )
                conn.commit()
                return int(cur.rowcount or 0) == 1
        except Exception:  # noqa: BLE001 —— 抢占失败不该打断求解（退化为不抢占）
            return False

    def release_claim(self, key: str) -> None:
        try:
            with self._conn_lock:
                conn = self._conn()
                conn.execute("DELETE FROM claims WHERE key = ?", (key,))
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def record_solve(self, host: str, ok: bool) -> None:
        """按域累计求解结果（只放计数，不放任何凭证）。用于 `/metrics` 的按域维度。"""
        if not host:
            return
        try:
            with self._conn_lock:          # 写操作：与其它写路径共用同一把锁
                conn = self._conn()
                conn.execute(
                    "INSERT INTO host_stats (host, solves, failures) VALUES (?, 1, ?) "
                    "ON CONFLICT(host) DO UPDATE SET solves = solves + 1, failures = failures + excluded.failures",
                    (host, 0 if ok else 1),
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

    def host_stats(self, limit: int = 20) -> list[dict]:
        try:
            # 同上：读路径必须进锁（共享连接 + 并发游标 = 行串台）。
            with self._conn_lock:
                rows = self._conn().execute(
                    "SELECT host, solves, failures FROM host_stats ORDER BY solves DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        except Exception:  # noqa: BLE001
            return []
        return [{"host": r[0], "solves": int(r[1]), "failures": int(r[2])} for r in rows]

    def __len__(self) -> int:
        try:
            # 同上：`/health` 每个请求都会走这里，和求解线程的写并发。
            with self._conn_lock:
                return int(self._conn().execute(
                    "SELECT COUNT(*) FROM entries").fetchone()[0])
        except Exception:  # noqa: BLE001
            return 0

    def close(self) -> None:
        with self._conn_lock:
            conn, self._conn_obj = self._conn_obj, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    # -- 内部 --------------------------------------
    def _prune(self, conn) -> None:
        """过期清理由 TTL 索引完成（O(log n)）；超限时按**到期时间**淘汰而不是文件 mtime。

        为什么按 expires_at 而不是 created_at：缓存的语义是"凭证还能用多久"，
        最早失效的那条最没保留价值；而 mtime 在多进程共享目录下连"谁写的"都说不清。
        """
        now = time.time()
        conn.execute("DELETE FROM entries WHERE expires_at <= ?", (now,))
        total = int(conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
        if total > self._max:
            conn.execute(
                "DELETE FROM entries WHERE key IN "
                "(SELECT key FROM entries ORDER BY expires_at ASC LIMIT ?)",
                (total - self._max,),
            )
        conn.commit()


def _build_fernet(cache_dir: str, token: str):
    """由 token + 盐派生 Fernet 密钥。盐首次生成后固定下来（原子写）。"""
    from cryptography.fernet import Fernet  # 延迟导入：纯内存模式不需要该依赖

    salt_path = os.path.join(cache_dir, SALT_FILENAME)
    salt = _load_or_create_salt(salt_path)
    key = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, PBKDF2_ITERATIONS, 32)
    return Fernet(base64.urlsafe_b64encode(key))


def _read_salt(path: str) -> bytes | None:
    """读盐；不足 16 字节视为"还没写完"，返回 None 让调用方重试。"""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    if len(data) >= 16:
        return data[:16]
    return None


def _load_or_create_salt(path: str) -> bytes:
    """取盐；不存在则**原子地**创建一个。

    这里原实现用「写临时文件 + os.replace」，在多进程首次同时启动时会炸：
    Windows 上两个进程同时 os.replace 到同一个目标，后到的那个抛
    `PermissionError: [WinError 5] 拒绝访问`（目标正被另一进程替换）。
    实测：4 个 worker 同时首启，约 1/4 概率有一个进程起不来。

    改法：用 `O_CREAT|O_EXCL` 抢占（谁先 open 成功谁负责写），
    失败者退化为"轮询等待对端把盐写完整"。
    关键不变量：**所有进程最终必须拿到同一份盐**，否则各自派生出不同密钥，
    共享目录里的缓存会互相解不开（比崩溃更难排查）。
    """
    salt = _read_salt(path)
    if salt is not None:
        return salt

    # 总等待窗口给到 10s。为什么会需要这么久：机器高负载时（压测/多 worker 同时起），
    # 子进程"启动 + import + 抢文件"本身就可能花几秒；窗口太短会表现为
    # "并发首启偶发失败"（实测在跑压测时 3s 窗口仍会偶发耗尽）。
    # 窗口只是上限：正常情况下第一条路径就拿到盐，一秒都用不到。
    WINDOW = 10.0
    deadline = time.monotonic() + WINDOW
    attempt = 0
    while True:
        attempt += 1
        if attempt > 2000 or time.monotonic() > deadline:
            break
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # 别人正在写（或已写好）：等一小会儿再读
            time.sleep(min(0.002 * attempt, 0.05))
            salt = _read_salt(path)
            if salt is not None:
                return salt
            # 占位文件存在但内容为空、且已经空转了一段时间 ⇒ 对端在 open 与 write 之间崩了。
            # 这种"半成品盐"会让服务永远起不来，所以允许晚到的进程清理后重试；
            # 但必须用**墙钟**判断（而不是轮次），否则慢机器上会把对端正在写的文件删掉。
            if time.monotonic() > deadline - (WINDOW / 2):
                try:
                    if os.path.getsize(path) == 0:
                        os.unlink(path)
                except OSError:
                    pass
                deadline = time.monotonic() + WINDOW  # 清理后重新给一个窗口
                attempt = 0
            continue
        except OSError:
            # 目录都不可写：交给上层降级（build_store 会捕获并说明原因）
            raise
        try:
            os.write(fd, os.urandom(16))
            os.fsync(fd)
        finally:
            os.close(fd)
        _harden_file(path)
        salt = _read_salt(path)
        if salt is not None:
            return salt

    raise OSError("无法创建或读取缓存盐文件（多进程竞态未收敛）")


def _harden_dir(path: str) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _harden_file(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def build_store(
    cache_dir: str | None,
    token: str,
    max_files: int = 4096,
    *,
    backend: str = "auto",
) -> tuple[Any, str]:
    """按配置构造缓存。返回 (store, 实际类型描述)。

    `backend` 取值（v4.0 新增，默认 auto）：
      · `auto`   —— SQLite → FileStore → Memory 逐级降级（推荐）
      · `sqlite` —— 只试 SQLite，失败降级到 Memory
      · `file`   —— 只试 FileStore，失败降级到 Memory（v3 的默认行为，保留为逃生门）
      · `memory` —— 不落盘

    落盘失败（缺 cryptography / 目录不可写 / SQLite 打不开）时**降级而不是启动失败**，
    但必须把原因写进返回值，由调用方打进启动日志 —— 不允许静默降级。
    """
    if not cache_dir:
        return MemoryStore(), "memory"

    preferred = (backend or "auto").strip().lower()
    if preferred == "memory":
        return MemoryStore(), "memory"
    order = {
        "auto": ("sqlite", "file"),
        "sqlite": ("sqlite",),
        "file": ("file",),
    }.get(preferred)
    if order is None:
        # 配置里写了不认识的值：不静默当成 auto（否则运维以为换成 sqlite 了其实没有）
        return MemoryStore(), f"memory(fallback: 未知 cache backend {backend!r}；可选 auto/sqlite/file/memory)"

    reasons: list[str] = []
    for kind in order:
        try:
            if kind == "sqlite":
                return SqliteStore(cache_dir, token, max_files=max_files), f"sqlite:{os.path.abspath(cache_dir)}"
            return FileStore(cache_dir, token, max_files=max_files), f"file:{os.path.abspath(cache_dir)}"
        except Exception as e:  # noqa: BLE001 —— 落盘不可用不该让服务起不来
            reasons.append(f"{kind}: {type(e).__name__}: {str(e)[:80]}")
            continue
    return MemoryStore(), "memory(fallback: " + " | ".join(reasons) + ")"
