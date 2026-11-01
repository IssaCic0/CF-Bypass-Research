# N-15 / N-16：缓存层的两个并发缺陷（v4.7 实测 → 修复 → 复验）

> 发现方式：跑完整的 `run.ps1 verify` 时，pytest 的 `-q` 汇总里出现一条
> `PytestUnhandledThreadExceptionWarning` —— **304 个用例全绿**，但线程里在抛 `TypeError`。
> 顺着这条 warning 挖出两个缺陷，其中一个是**凭证缓存串域**（P0）。

---

## N-15（P0）读路径不加锁：共享 sqlite 连接上游标互相踩

### 现象

```
tests/test_cache_sqlite.py:336: in work
    store.get(f"k{i}|default")
solver/cache.py:375: in get
    if float(expires_at) <= time.time():
TypeError: float() argument must be a string or a real number, not 'NoneType'
```

只是一条 **warning**：`test_sqlite_store_uses_single_shared_connection` 照样 pass
（它只断言"只建了一条连接"和 `len == 16`）。

### 根因

v4.2 为了修句柄泄漏（thread-local 连接 = 每条 TCP 连接 3 个句柄，1 小时涨 1308 个），
把连接改成**全进程共享一条**，但只给**写**路径加了 `_conn_lock`，读路径留了注释：

> 读路径（get/items/__len__/stats/host_stats）**不取锁** —— 只读，并发安全由 `sqlite3`
> 自身（`check_same_thread=False` + 单连接内部互斥）保证

**这个结论是错的。** pysqlite 只把单次 C 调用串行化；两条线程在同一条连接上
各自 `execute()` 之后，**游标状态是共享的**，`fetchone()` / `fetchall()` 会互相踩。

### 最坏后果：读到**别的键**的条目

新增用例 `test_concurrent_reads_and_writes_never_corrupt_rows`（16 线程 × 20 轮，
每轮 set 自己的键再 get 回来并校验 `cf_clearance`）在**修复前**的失败输出：

```
assert not errors, f"并发读写出现 {len(errors)} 次异常，前 3 条：{errors[:3]}"
E  AssertionError: 并发读写出现 11 次异常，前 3 条：
   ["thread 9: AssertionError: 读到了别的键的条目：期望 C9，实际
     {'cf_clearance': 'C10', ...}",
    "thread 14: TypeError: float() argument must be a string or a real number, not 'NoneType'",
    "thread 13: AssertionError: 读到了别的键的条目：期望 C13，实际
     {'cf_clearance': 'C4', ...}"]
```

即：**问 k9 拿到了 k10 的 `cf_clearance`**。这不是"偶发异常"，是**凭证缓存串域** ——
回放时会用别的域的凭证（稳定 403），更糟的是它违反"域名 → 凭证"这个最基本的隔离。

### 修法

四条读路径（`get` / `items` / `__len__` / `host_stats`，外加 `stats()`）全部进
`_conn_lock`（`RLock`，所以 `get()` 内部调 `delete()` 不会死锁）。
锁只覆盖到 fetch 完成（行已物化），解密/解析在锁外做。

另外给 `expires_at` 加了类型防御：坏行按未命中处理，而不是把 `TypeError` 抛给调用方。

### 复验

```
$ python -m pytest tests/ -q
307 passed            # 连跑 3 次均 307 passed
```

性能没有退化（锁只在 DB 调用期间持有，求解是秒级、DB 是微秒级）：

| 项 | 修复前 | 修复后 |
|---|---|---|
| `loadtest --mode hot` | 5615.6 QPS / p99 2.92ms | **5959.6 QPS / p99 2.72ms** |

---

## N-16（中）切 WAL 不带重试：多进程冷启动约 1/10 有进程起不来

### 现象

```
child2: ... _init_schema → conn = self._conn() → conn.execute("PRAGMA journal_mode=WAL")
sqlite3.OperationalError: database is locked
```

即 `test_concurrent_first_boot_shares_one_salt` **间歇性红**。

### 是不是本轮引入的？不是 —— 在原始 HEAD 上复现过

把 `solver/cache.py` 的改动 `git stash` 掉、只跑这一条用例 10 轮：

```
1 passed / 1 passed / 1 failed / 1 passed / ... （10 轮里 1 次红）
```

所以是**既有缺陷**，与本次改动无关；只是它让"全绿"这件事每次都要看运气。

### 根因

`PRAGMA journal_mode=WAL` 需要一次短暂独占锁，而它在拿不到锁时**不走 busy handler**，
直接返回 `SQLITE_BUSY`。冷启动时 N 个进程同时建库 + 切 WAL，就会撞上。

### 修法

- `busy_timeout` 改到切 WAL **之前**设置；
- 切 WAL 走**有界重试**（25 次 × 0.2~0.6s ≈ 最长 10s），抽成 `_try_set_wal` 以便单测；
- 仍然失败则退回默认 journal 模式，**打印警告**并把真实模式暴露在 `stats().journal_mode`
  —— 可用性优先，但不静默（这也是本项目对"降级"的一贯要求）。

### 复验

```
$ for i in $(seq 1 10); do pytest tests/test_cache_sqlite.py::test_concurrent_first_boot_shares_one_salt -q; done
10 × 1 passed
```

新增两条用例：
- `test_wal_switch_retries_when_database_is_locked`（mock 真实 SQLite busy，断言重试后进 WAL）
- `test_wal_degradation_is_loud_but_usable`（mock 永久 busy，断言降级后可用 + 有警告 + stats 可见）

### 诚实边界

这条竞态**没有**被用例确定性复现（同进程持锁、跨进程持锁、90 次裸构造都没稳定命中）。
回归用例只覆盖"重试逻辑本身"，真实竞态靠降低概率 + 压力观察。
`workflow_status.md` §7 的 R13 记着这条边界，不要读成"已经不可能再发生"。
