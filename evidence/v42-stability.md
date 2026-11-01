# v4.2 稳定性实测记录

> 生成时间：2026-11-02 00:2x · 代码基线：v4.1.0 + 本次改动 · 机器：Windows 10 / AMD64 · Python 3.14.3
> 原始输出：`evidence/cli/v42-stability.txt`、`evidence/cli/v42-route-auto-real.txt`、`plans/证据/v42-soak.jsonl`

## 为什么做这一组

v4.1 之前的验收只有"功能对不对"（单元测试 + 真机 A/B + E2E），
**没有一条证据回答"跑久了会不会出问题、重启会不会掉请求"** —— 而这恰好是用户最真实的抱怨形态。
四项实测都是"以前完全没测过"的。

---

## 1. 优雅退出：修复前会掉请求（真实故障）

| | 触发方式 | 在途请求（7s 真求解）的结局 | 进程退出 |
|---|---|---|---|
| **修复前** | `os.kill(pid, SIGTERM)`（= Windows 上等价于强制终止） | ❌ `WinError 10054 远程主机强迫关闭了一个现有的连接` | 0.0s 立即消失 |
| **修复后** | `POST /shutdown`（Bearer + 回环） | ✅ **HTTP 200 + solved=true + 7.02s** | 在途收尾后退出（日志：`优雅退出：在途请求已全部完成`），端口释放 |

**根因**：`Server.daemon_threads = True` 只保证"进程能退出"，不保证"handler 线程能回包"；
原实现是 `serve_forever()` 返回后直接 `server_close()` + `solver.close()`，中间没有"等在途请求"这一步。

**修法**（三件套）：
1. `Handler` 用装饰器统计在途请求数（`do_GET`/`do_POST` 进出各一次）；
2. `Server.drain(timeout)` 轮询在途计数直到 0（上限 `SHUTDOWN_GRACE=10s`），main 在关闭前调用；
3. 新增 `POST /shutdown`（**默认仅回环 + 必须带 token**）供滚动重启使用，`run.ps1 stop` 优先走它，15s 未退才强杀。

---

## 2. 并发：缓存命中路径 vs 冷路径准入

| 场景 | 结果 | 判定 |
|---|---|---|
| 32 线程 × 20 次打**已缓存**的同一域 | **640 请求 0 错，396.3 QPS**，p50 5.7ms / p95 504ms / p99 **1017ms** | ✅ A2 目标（P99 ≤ 2.0s）达标 |
| 8 并发 `force=true`（绕过缓存）打同一域，`--max-inflight 2` | **6 个立刻 429 `busy`**（2–3ms）+ **2 个被准入**（6.8s / 13.5s 串行），wall 13.5s | ✅ 符合设计：超限即拒、不无界排队 |

**读法**：p95/p99 的长尾来自 Python `ThreadingHTTPServer` 在 32 并发下的排队（GIL + 线程调度），
不是缓存层的问题（缓存命中本身的处理是 5.7ms 级）。要更高的稳态 QPS，加进程而不是调大 `--max-inflight`。

---

## 3. 长跑（1 小时 soak，轻负载真实流量）

负载：每 10s 采 `/health`+`/metrics`；每 60s `/detect`（交替缓存命中/穿透）；每 120s `route=auto`（打无盾站走 skip，**不消耗过盾预算**）。

| 时刻 | RSS | 句柄 | state |
|---|---|---|---|
| t=0s（刚 ready，含浏览器启动峰值） | 1534.7 MB | 2787 | ready |
| t=60s | 737.0 MB | 2823 | ready |
| t=360s | 743.1 MB | 3014 | ready |
| t=480s（期间插入了 2 次真求解） | 1538.6 MB | 4300 | ready |
| t=600s | 830.6 MB | 3532 | ready |
| t=640s（≤10 分钟稳态） | 830.0 MB | 3548 | ready |

**阶段性结论**：RSS 在启动峰值之后稳定在 **约 830MB**（插入真求解会短暂冲高再回落，属浏览器工作集正常波动）；
句柄数在真求解之后抬到 ~3540 并趋于平缓。**最终判定以 soak 跑满 1 小时的 `summary.json` 为准**
（判定线：轻量端点错误率 0、始终 ready、RSS 增长 ≤80MB、句柄增长 ≤50）。

---

## 4. 真机：`route=auto` 在真实 CF 站上是否选对路

| 站点 | 探测结论 | 路由决策 | 结果 |
|---|---|---|---|
| steamdb.info | `protected=true` / Cloudflare / `challenge_type=managed` | `action=solve`, `reason=cloudflare-managed`, `prefer=pure` | ✅ **7.47s 解出 460 字节 clearance**（实际走 camoufox，因为本机未配纯协议二进制） |
| glassdoor.co.uk | 同站当天已在 A/B 中被连打 12 次 | 探测成功但求解 60s 超时 | ⚠️ 判为**出口 IP 可能已被提分**，按 §15 纪律主动停止后续真机实验 |

预算：本轮 ≈7 次（预热 1 + 冷路径 2 + 优雅退出 1 + 排空复验 1 + route=auto 2），累计 ≈53/60，**剩余 ≈7 次留待换出口后使用**。

---

## 5. 这一组暴露的、还没解决的问题

| # | 问题 | 现状 | 建议 |
|---|---|---|---|
| 1 | p95/p99 在 32 并发下有秒级长尾 | 已量化（p99 1.02s） | 要更高吞吐就加进程；单进程不建议超过 16 并发 |
| 2 | `--max-inflight` 与实际串行度不匹配（8 看着像 8 并发，其实 worker 串行） | 已量化（2 准入 = 串行 6.8s + 13.5s） | 文档已写明"调大只增加排队深度" |
| 3 | 长跑最终判定 | soak 仍在跑 | 见 `plans/证据/v42-soak.jsonl.summary.json` |
| 4 | glassdoor 疑似 IP 提分 | 已停止实验 | 换出口后再验证；不要在同一出口反复打同一站 |
