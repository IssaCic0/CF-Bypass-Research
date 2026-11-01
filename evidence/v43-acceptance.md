# v4.3 验收记录：E4/E5 演练 + 审计日志 + prefer 真跳过后端

> 2026-11-02 02:1x · 代码基线：v4.2.1 + 本次改动 · 机器：Windows 10 / AMD64

---

## 1. E4：4 worker 内存（目标 ≤ 3.5 GB）

4 个 worker（端口 8981-8984）共用同一个 `--cache-dir`（SQLite/WAL），各跑一个 Camoufox：

| worker | RSS（含浏览器子进程树） | 句柄 | 进程数 |
|---|---|---|---|
| 8981 | 863 MB | 2751 | 8 |
| 8982 | 855 MB | 2747 | 8 |
| 8983 | 853 MB | 2668 | 7 |
| 8984 | 850 MB | 2661 | 7 |
| **合计** | **3420.5 MB（3.34 GB）** | 10827 | — |

**判定：PASS**（3.34 GB ≤ 3.5 GB；单 worker 均值 855 MB）。
口算扩容：加 1 个 worker ≈ +0.86 GB，4 worker 是这台机器（41 GB）很保守的配置。

---

## 2. E5：worker 崩溃隔离与自动回归（目标：摘除 ≤20s、回归 ≤60s）

做法：4 节点池（Go `cfsolve.Pool`，`maxFailures=2`、`downDuration=30s`、主动探活 2s），
由外部脚本在 **drill t≈8s 杀掉 8984**，在 **t≈30s 重启它**，drill 每秒打印一次 `pool.Nodes()`。

| 时刻 | 8984 状态 | 说明 |
|---|---|---|
| t=0s | ok (f=0) | 四节点全健康 |
| t=8s | **f=1** | 杀进程那一秒，探活第一次失败 |
| **t=10s** | **DOWN (f=2)** | 达到 `maxFailures=2` 被摘除 ⇒ **摘除时延 ≈2s**（目标 ≤20s）|
| t=12s → t=32s | DOWN (f 涨到 12) | 进程不在，探活持续失败（符合预期）|
| t=32s | DOWN (f=**0**) | 进程重启完成，探活已能连上（失败计数清零），但仍在 30s 冷却期 |
| **t=60s** | **ok (f=0)** | 冷却结束自动回归 ⇒ **从重启到恢复 ≈28s**（目标 ≤60s）|

**判定：PASS**（摘除 2s / 回归 28s，都远优于目标）。
关键点：**摘除和回归都是自动的**，没有人工干预；期间其余 3 个节点持续健康（`f=0` 全程未变）。

### 演练脚本自身的一个坑（诚实记录）

`pooldrill` 原先在结尾固定对 `https://example.com/` 发一次 `Get`，结果是
`池内 4 个节点均失败… context deadline exceeded` —— 因为 example.com **没有盾**，
求解必然等不到 clearance 直到超时。这是**脚本设计问题，不是池的问题**，
但很容易被后来的人误读成"池坏了"。
已改为 `-final-get` 可选参数（默认不跑），并把这个理由写进代码注释。

---

## 3. T-38 审计日志（`--audit-log`）

| 项 | 实现 | 验证 |
|---|---|---|
| 格式 | JSONL，一行一个事件 | 用例 `test_audit_log_records_request_without_credentials` |
| 内容 | 字段**白名单**：`event/req_id/host/ok/cached/elapsed/clearance_len/ms/error_code/stage/backend/route/reason/peer/ts` | 用例断言文件里**不含** `cf_clearance` / `Bearer` |
| 防未来误加 | 白名单之外的字段一律丢弃（黑名单一旦漏一个就会写进凭证） | `test_audit_log_drops_unknown_fields`（故意塞 `cf_clearance`/`token`/`cookies`） |
| 失败降级 | 写盘失败只打一条 `audit-write-failed` 到 stdout，**请求照常成功** | `test_audit_log_write_failure_does_not_break_request` |
| 时序 | 审计行在响应发出**之后**写（存在毫秒级窗口） | 已在用例注释里写明，测试用 `wait_until` 等待 |

---

## 4. `prefer` 现在真的影响选路（此前只是提示字段）

| 行为 | 之前 | 现在 |
|---|---|---|
| `route=auto` 判定 `prefer=pure` | 只是响应里的一个字符串 | **跳过 pydoll / flaresolverr**，优先纯协议 |
| `prefer=browser` | 同上 | **跳过 cfsolve**，直接走浏览器 |
| 跳过记录 | — | `attempts[]` 里出现 `{"backend": "...", "skipped": true, "reason": "prefer-skip"}` |
| 安全阀 | — | 若 `skip` 会把整条链跳完，则**忽略 skip** 正常求解（不制造"没有可用后端"） |

用例：`test_skip_backends_mapping`、`test_router_skips_backends_by_prefer`、
`test_router_ignores_skip_when_it_would_skip_everything`。
一个细节：跳过是**惰性**的 —— 前面的后端成功就不会再看后面的（用例特意把被跳过的放前面才测得出来）。

---

## 5. Docker

`docker info` 仍然连不上（`dockerDesktopLinuxEngine` 未启动，本机没有可用的 Docker daemon）。
**镜像构建验证仍为未完成项**，`Dockerfile` 头部的 ⚠️ 标注保持有效。不做"应该能构建"的宣称。

---

## 6. 本轮的测试与门禁

| 项 | 结果 |
|---|---|
| Python | **293 passed**，覆盖率 **81.69%**（门槛 80%） |
| Go | 41 Test，`go vet` + `go test -race` 绿；新增 `examples/pooldrill` 可构建 |
| 真机预算 | 本轮 0 次新增（E4/E5 全部用本地服务，不打目标站）；累计仍 ≈53/60 |
