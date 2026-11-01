# 句柄泄漏：定位到修复的完整证据链

> 2026-11-02 · 由 1 小时 soak 抓出 · 代码基线 v4.2.0 → 修复见 v4.2.1
> 原始数据：`plans/证据/v42-soak.jsonl`（修复前）、`plans/证据/v42-soak-after-fix.jsonl`（修复后）

## 结论先行

**`/health` 每被访问一次（准确说：每建立一条新 TCP 连接），服务端泄漏约 3 个 Windows 内核句柄，且永不释放。**
1 小时 soak 泄漏 **+1308**；按此速率跑 24 小时约 **+31k**。
根因是**缓存层用 thread-local 持有 SQLite 连接**，而 HTTP handler 是"每连接一个线程"。

---

## 第 1 步：soak 先说结论（360 个采样，1 小时）

```
samples: 360   solve_count: 0
light_endpoint_errors: 0     ✅
always_ready: 0              ✅  （整整 1 小时没 degraded 过）
rss: 1534.7 MB → 427.6 MB   （回落，非泄漏）
handles: 2787 → 4095        ❌ +1308，且**线程数全程不变**
```

两条通过、一条失败 → 失败项是**真问题**（句柄增长），不是尺子问题。
> 顺带修了尺子的一个 bug：原判据用 `abs(rss_growth)`，把"RSS 掉了 1107MB"也判成失败。
> 已改为"只看增长"（内存回落不是缺陷）。

## 第 2 步：归因到具体端点

对同一进程（python.exe）分别打不同端点，各 100/20 次：

| 操作 | 句柄增量 |
|---|---|
| `+100 × GET /health` | **+300**（python.exe；camoufox 子进程 0） |
| `+20 × GET /detect` | 0 |
| `+20 × POST /solve route=auto`（skip 分支） | 0 |

→ 泄漏在 `/health` 的处理路径上，且是**我们自己的进程**。

## 第 3 步：是"每连接"还是"每请求"？

| 实验 | 句柄增量 |
|---|---|
| A) **1 条连接** × 100 次请求（keep-alive 复用） | +7（≈0） |
| B) **100 条连接** × 1 次请求 | **+296** |
| C) 100 次 404（同样新建连接） | +0 |

→ **按连接计数**，不是按请求。复用连接不泄漏。
（C 为 0 是因为 404 路径不碰缓存层 —— 这恰好是下一条线索。）

## 第 4 步：对照实验 —— 是不是 stdlib 的锅？

用**最小 `ThreadingHTTPServer`**（不含本仓库任何代码）打 100 条新连接：

```
基线 handles=174 → +100 连接后 handles=190（+16，其中 +2 是线程）
```

→ **stdlib 不泄漏**。泄漏在我们的代码里。

## 第 5 步：根因

`/health` 每次都会读 `cached_domains` → `Solver._store_len()` → `len(store)` →
`SqliteStore.__len__()` → `self._conn()`。而 `_conn()` 当年是这么写的：

```python
self._local = threading.local()          # 每线程一条连接
def _conn(self):
    conn = getattr(self._local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(...)      # ← 每个新 handler 线程建一条
        ...
        self._local.conn = conn
    return conn
```

HTTP handler 线程的生命周期 = **一条 TCP 连接**。所以：
**每条新连接 → 新线程 → 新 SQLite 连接**。而 WAL 模式下一条连接至少占 3 个文件句柄
（`cache.sqlite3` / `-wal` / `-shm`），线程退出后连接对象未被及时 `close()`，
句柄就留在进程里 → **正好每连接 +3**。

这也解释了"线程数不变但句柄涨"：线程退出了，句柄没跟着退。

## 第 6 步：修法

`SqliteStore` 改为**全进程共享一条连接**（`check_same_thread=False` + 一把 `RLock`），
并加 `_conn_creations` 计数便于回归断言。

为什么可以共享：这一层只做毫秒级小读写，一把 RLock 串行化完全够；
跨进程并发仍由 WAL + `busy_timeout` 处理（那是另一层问题，不受影响）。
另外顺手把 `/health` 的 `cached_domains` 从"扫全表"变成 `len(store)` 走 `COUNT(*)`（同一条连接）。

## 第 7 步：修复后实测

同一台机器、同一个压测手法：

| 操作 | 修复前 | 修复后 |
|---|---|---|
| +100 条新连接 `/health` | +300 | **+0** |
| +100 条新连接 `/metrics` | — | **+0** |
| 15 分钟 soak | （1h） +1308 | 见 `plans/证据/v42-soak-after-fix.jsonl.summary.json` |

回归保护：`tests/test_cache_sqlite.py::test_sqlite_store_uses_single_shared_connection`
断言"16 个线程各做 5 次读写后，`_conn_creations == 1`"。

## 附带修掉的一个测试抖动（诚实记录）

`test_concurrent_first_boot_shares_one_salt` 在有重负载（同时跑 soak）时偶发失败：
6 个子进程抢建 `salt.bin` 的等待窗口原本是 3s，高负载下子进程"启动+import+抢文件"可能更久。
**把窗口放宽到 10s（上限，不是常态；正常路径第一条就拿到）**，断言本身没动。
这不是"放宽断言让测试变绿"—— 断言仍然是"全部子进程成功且拿到同一份盐"。
