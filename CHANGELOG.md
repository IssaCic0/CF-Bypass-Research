# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [4.0.0] - 2026-11-01

主题：**路由闭环 · 治理收口 · 缓存与观测升级**。

这一版的重点不是"加功能"，而是把三处断掉的价值链接上，并把治理从"绑在某个后端实现上"
改成"绑在入口上"。施工图与验收口径见 `plans/下一步改进指南.md`、`policy.md`、`workflow_status.md`。

### 新增

- **`route=auto` 自动路由闭环**（`solver/route.py`，`POST /solve` 新参数）
  - 先发一次普通 GET 探测，再决定：无盾 → **不求解**直接告知；不支持厂商（DataDome/PerimeterX/…）→
    **直接拒绝**而不是白等一整个超时；Cloudflare `managed/jsd` → 纯协议优先；`turnstile/recaptcha` → 浏览器优先。
  - 响应新增 `routed_by`（mode/action/reason/prefer/detect）。
  - **不传 `route` 时行为与 v3 完全一致，且不会产生任何额外探测请求**（有用例锁死）。
  - 真机实测：同一 URL，`route=auto` **0.55s / 0 次求解**；不传 route 则真求解 **20s**。
- **失败原因分类**（`classify_failure` / `should_escalate`）：`no-challenge` / `profile-expired` /
  `ip-blocked` / `too-slow` / `navigation-error` / `challenge-unsolvable`，并明确"哪些原因不值得换后端"。
- **SQLite(WAL) 落盘缓存**（`solver/cache.py::SqliteStore`）：TTL 索引淘汰、单事务原子写、多进程共享，
  `build_store(backend=auto|sqlite|file|memory)` 逐级降级（FileStore 保留为逃生门）。
- **按域统计**（`record_solve` / `host_stats`）：只放计数，不放凭证，供 `/metrics` 使用。
- **统一入口** `run.ps1` / `run.bat`：`doctor|test|quick|go|findings|smoke|start|stop|bench|verify`。
- **环境自检** `scripts/doctor.py`：依赖 / 浏览器二进制 / 端口 / 缓存目录 / token / 可选后端，
  并真起一次服务打 `/health`；退出码 0=全绿 / 1=可降级 / 2=阻断。
- **离线压测** `scripts/loadtest.py`：`hot`(回放吞吐) / `cold`(准入与去重) / `share`(多进程缓存)。
- **CI**（`.github/workflows/ci.yml`）：python 覆盖率门槛 80% + go `vet`/`test -race` + findings 一致性，**全程离网**。
- **容器化**：`Dockerfile`（camoufox 二进制在 build 阶段 fetch）+ `docker-compose.yml`（3 worker 共用缓存卷）。
- **工程约束文件**：`policy.md`（门禁写死数值）、`workflow_status.md`、`test_ledger.md`、`evidence/`。

### 修复

- **SSRF 守卫在配置额外后端时被整体绕过**（安全）：守卫原先写在 `Solver.solve` 内，
  而 handler 走 `router` 分支根本不进该方法 —— 已上提到入口，所有后端共用。
- **多进程首启争抢建盐文件抛 `PermissionError`**（Windows）：4 worker 同时首启约 1/4 概率起不来。
  改为 `O_CREAT|O_EXCL` 抢占 + 轮询等对端写完整；两种平台都只认先创建成功的那份盐。
- **`cfsolve` / `pydoll` 返回 `expires_at=0`**：导致 Go 客户端每次回放都重新求解（热路径命中率归零）。
  现已返回真实 TTL，并把"所有后端 TTL>0"提升为契约级断言。
- **Go 回放遇 403/挑战页原样透传**（生产咬人项）：现自动失效该域缓存、重解一次、用新凭证重放；
  只重试一次，偷看正文后还原 body，不可重放的请求体不重试。
- **Go 池把终止性错误记成节点故障**：一次参数写错的调用就能把健康节点全部摘掉。
  现在终止性错误不 failover 也不摘节点；429 过载 failover 但不算故障；探活补 `!ready` 分支。
- **`FileStore.items()` 不过滤过期项**：`/health.cached_domains` 虚高。
- **`/detect` 无准入且重定向不复检**：现已逐跳复检 SSRF、拒绝非 http(s) 跳转、独立并发闸门。
- **后端链不共享超时预算**：最坏可跑到 375s 而客户端 240s 先超时；现在 router 按墙钟 deadline 切分。

### 变更（破坏性）

- **落盘缓存默认实现从 FileStore 改为 SQLite**(`cache.sqlite3`)。旧 `*.bin` **不做迁移** ——
  `cf_clearance` 只有 30 分钟量级，重建代价是"每域多解一次"，远小于一次性迁移代码的 bug 风险。
  需要旧行为：`build_store(..., backend="file")`。
- 删除研究产物 `node-shim/`、`go-jseng/`、`go-replay/`（不在主链路，结论保留在 `docs/findings.json` F-002）。
- `service.py` 的 `/solve` 请求校验新增 `route` 字段解析；未知取值返回 `400 invalid-request`。

### 已知限制（诚实披露）

- 多进程共享缓存只防"**先后**到达"的重复求解；4 个 worker **同时**抢同一新域仍会各解一次（无跨进程互斥，实测 12/12）。
- `route=auto` 的 `prefer=pure|browser` 目前只是提示，未实现"按偏好跳过后端"。
- `route=auto` 每次都会探测一次（约 +0.5s），按 host 缓存探测结果（T-34）尚未实现。
- Docker 镜像**未构建验证**（开发机 Docker daemon 未运行）；CI 工作流**未在 GitHub 上真跑过**。
- 本项目没有生产部署环境，"部署/热更新"只做到本地多进程启停 + 健康检查。

### 验证

| 项 | 结果 |
|---|---|
| Python 测试 | **257 passed** |
| Python 覆盖率 | **81.03%**（`--cov-fail-under=80` 通过；`service.py` 73%、`route.py` 99%） |
| Go 测试 | **41 个 Test，`go vet` + `go test -race` 全绿** |
| 结论库一致性 | `node scripts/validate-findings.mjs` → 29 条，exit 0 |
| E2E 冒烟 | `/health` ready · `/detect` → direct · SSRF 守卫拦住内网 URL |
| 回放吞吐（离线） | 2874.9 QPS，p99 5.91ms，14382 请求 0 错 |
| 冷路径准入（离线） | 16/16 accepted，每请求 1 次求解 |
| 多进程缓存 | sequential 每轮只解 1 次；burst 12/12（已知限制） |
| `route=auto` | 真机 example.com：0.55s / 0 次求解（对照：不传 route 20s / 1 次求解） |

## [3.0.0] - 2026-11-01

- 求解服务 v2：Bearer 鉴权、SSRF 三态守卫、有界准入 + inflight 闸门、worker 自愈、`/health` 三态、`/metrics`
- 落盘加密缓存（Fernet + PBKDF2 + 原子写）、代理池（一致性哈希 + 冷却）、保活续期、多进程池
- 多后端可切换：纯协议 cfsolve / Camoufox / pydoll / FlareSolverr，带 `attempts` 轨迹与终止码纪律
- 多挑战类型：`want=clearance|awswaf|turnstile|recaptcha_v3`
- 防护识别 `/detect`（33 厂商特征 + 5 条强特征）
- 174 项 Python 测试 + 17 项 Go 测试；`docs/findings.json` 结论机器化校验
