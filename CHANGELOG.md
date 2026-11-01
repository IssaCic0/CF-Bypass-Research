# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [4.2.0] - 2026-11-02

主题：**稳定性收口**——把"优雅退出会掉请求"这个真实故障补掉，并把文档漂移一次清完。

### 新增

- **`POST /shutdown` 优雅退出端点**（Bearer 鉴权 + **默认只允许回环地址**）。
  先停收新连接 → **等在途请求收尾（≤10s）** → 再关资源。
  实测（7.02s 的真求解在途中触发）：在途请求**拿到 200 + solved=true**，
  端口随后释放，日志 `优雅退出：在途请求已全部完成`。
- **`Server.drain()`**：按在途请求计数排空；`Handler` 用装饰器统计在途请求（`do_GET/do_POST`）。
- **`scripts/soak.py`**：长跑稳定性（RSS/句柄/ready/探测耗时曲线 + 四条判定阈值），
  默认**不做真求解**（不烧过盾预算）。
- **`run.ps1 stop` 改为优雅停止**：先 `POST /shutdown`，15s 未退才强杀。

### 修复

- **优雅退出会砍掉在途请求**（P1，实测）：Windows 上 `Stop-Process` / `kill -TERM`
  等价于强制终止，正在求解的调用方拿到 `WinError 10054 连接被强制关闭`。
  根因是"停收连接后立刻退出"，没有等 handler 线程收尾（`daemon_threads=True` 只保证能退出，
  不保证能回包）。现在有 `/shutdown` + `drain()` + 滚动重启脚本三件套。
- **`route=auto` 失败响应缺 `routed_by`**（P2，实测）：真实 CF 站 60s 未解出时，
  响应里 `routed_by` 全空，排查时无法区分"探测判错"与"解不出来"。现在成功/失败/异常三条路径都回传。

### 文档（一次清完 5 处漂移）

- `docs/API.md`：补 `/detect` 的 `fresh` 参数与 `cached` 字段、`--detect-cache-ttl`、
  `/shutdown` 契约、新增指标（`cfsolve_backend_total{backend,result}`、`cfsolve_host_*`）。
- `docs/00-索引.md`：能力清单补 `route=auto` 与"治理不随后端变化"。
- `SKILL.md`：补 `route=auto`、治理必须绑入口/路由的坑、geoip 否决结论。
- `docs/项目规格.md`：新增 `POST /shutdown`、`--allow-remote-shutdown`、
  **入口守卫 400 vs 502 的语义对照表**（实测踩过：拿不存在的域名测"后端是否被调用"，差点误判）。
- `plans/下一步改进指南.md`：回填已完成/未开始卡的状态，避免下次把已完成当 TODO。
- `docs/部署-容器.md`：热更新改用 `/shutdown`，说明为什么不能 `taskkill /F`。

### 决策

- **geoip（T-14）否决**：`camoufox` 的 `geoip=True` 会调用 `download_mmdb()` **下载 GeoIP 数据库**
  （`geolocation.py`，落到 `user_cache_dir("camoufox")/geoip/mmdb/`）。这是运行期外部依赖 + 隐私面，
  按指南 §8 的规则直接否决，不做 A/B、保持 `geoip=False`。要重开必须：①离线 MMDB 供给方式 ②同站双臂实测收益。

### 稳定性实测（本机）

| 场景 | 结果 |
|---|---|
| 缓存命中并发（32 线程 × 20） | **640 请求 0 错**，396.3 QPS，p50 5.7ms / p99 **1017ms**（A2 目标 ≤2s ✅） |
| 冷路径 8 并发（`--max-inflight 2`） | **6 个立刻 429 busy、2 个准入**（一个 6.8s、一个 13.5s 串行），行为符合设计：满了即拒、不无界排队 |
| 优雅退出（在途 7.02s 真求解） | 修复前 `WinError 10054`；修复后 **200 + solved=true** |
| 长跑（1 小时 soak） | 见 `plans/证据/v42-soak.jsonl`（RSS 稳定在 ~830MB，句柄在 t≈10min 后趋于平台） |

### 真实调用（真机，预算 ≈53/60）

- ✅ `route=auto` 打 **steamdb.info**：探测 `cloudflare/managed` → `action=solve, prefer=pure` →
  **7.47s 解出 460 字节 clearance**。这是"自动路由在真实 CF 站上选对了路"的第一份证据。
- ⚠️ `route=auto` 打 **glassdoor.co.uk**：60s 未解出（该站当天在 A/B 里已被连打 12 次）。
  按 §15 的 IP 信誉纪律**主动停止**后续真机实验，剩余 ≈7 次预算留待换出口后再用。

## [4.1.0] - 2026-11-01

主题：**把治理从"某个后端实现"上提到"入口 + 路由层"**。
v4.0.0 的提交信息声称做了这件事，但实测没做（三条 P0/P1 仍在），本版是真修。

### 修复（实测复现 → 修 → 实测验证）

- **P0 SSRF：配了任一额外后端时 `/solve` 的守卫被整体绕过**。守卫原先只在
  `Solver.solve` 里，而配了 cfsolve/pydoll/flaresolverr 时 handler 走 router 分支根本不进该方法。
  已实测复现（假 cfsolve 二进制收到 `--page http://127.0.0.1:.../health`）。
  现在守卫在**选后端之前**执行一次，任何后端组合都拦得住；修复后同一实验里
  该二进制**一次都没被启动**。
- **P1 纯协议/pydoll/兜底后端"三不沾"**（不缓存 / 不准入 / 不进指标）。`BackendRouter`
  新增 `governance=` 通道，由路由层统一施加缓存查询与写入、inflight 准入、按后端与按域计数。
  实测：修复前第二次请求 `cached:false` 且 `/metrics` 全 0；修复后第二次 `cached:true`、
  `solves=1 cache_hits=1`、`cfsolve_backend_total{backend="cfsolve",result="ok"} 1`。
- **P1 `host_stats` 是死代码**：现在在每次后端调用后记录，并在 `/metrics` 暴露
  top-20 的 `cfsolve_host_solves_total` / `cfsolve_host_failures_total`（截断防基数爆炸）。

### 新增

- **T-34 探测结果按域短 TTL 缓存**（默认 300s，`--detect-cache-ttl 0` 关闭；`fresh=1` 跳过）。
  摊掉 `route=auto` 每次多出来的 ≈0.5s 出站。
- **`cfsolve_backend_total{backend,result}` 指标**：能看出"哪个后端在退步"。
- **`docs/项目规格.md`**（单一事实源：定位/拓扑/契约/配置全表/凭证模型/SLO/已知限制/决策索引）。
- **`docs/部署-容器.md`**、**`docs/运维-压测与自检.md`**。
- **`scripts/ab_persona.py`**：同站双臂 persona A/B（`scripts/verify_real.py` 是站点间对照，
  归因力弱；本脚本让同一个站被两个臂各打一遍）。**强制 `force=true`**，否则第 2 次起命中
  缓存，测到的就不是 persona 差异。
- **`tests/test_governance.py`**（12 例）与 **`tests/test_acceptance_v41.py`**（10 例验收）。

### 变更

- `cfsolve_solves_total` 的口径修正为"真实发起的求解次数（含失败）"，与 HELP 文本一致。
- `BackendRouter(backends, governance=...)`：不传时退化为纯路由（兼容旧用法）。
- 盐文件创建的重试窗口从"20 轮 ≈0.5s"放宽到"墙钟 3s"，修复高负载下 6 进程并发首启偶发失败。

### 验证

| 项 | 结果 |
|---|---|
| Python 测试 | **279 passed** |
| 覆盖率 | **81.39%**（门槛 80%） |
| Go 测试 | 41 个 Test，`go vet` + `-race` 全绿 |
| 治理实测 | 内网 URL 被入口拦住（后端 0 次调用）；纯协议第二次 `cached:true`；`/metrics` 与真实调用一致 |
| 真机 A/B | 见 `plans/证据/v41-persona-ab.json`（同站双臂，预算内） |

## [4.0.1] - 2026-11-01

补丁版：修掉 v4.0.0 首次真跑 CI 时暴露的 2 个跨平台缺陷（本机 Windows/py3.14 复现不出）。
v4.0.0 的 tag 保持不动（不改写已发布引用），发布方式为 patch 递增。

### 修复

- **`Prewarmer` 覆盖了 `threading.Thread._stop` 私有方法**：`self._stop = threading.Event()`
  顶替掉线程收尾时会被调用的 `Thread._stop()`，在 Python 3.12 上抛
  `TypeError: 'Event' object is not callable`，导致保活线程永远 join 不掉。
  改为 `_stop_event`（私有属性，无对外影响）。
- **`test_filestore_corrupt_file_*` 弄坏了错的对象**：用 `glob("*.bin")` 取"要弄坏的文件"，
  而 `salt.bin` 也匹配；Linux 目录序先返回 `salt.bin`，于是用例实际在破坏盐而非缓存条目。
  改用已有的 `cache_files()` 助手（显式排除盐）。

### 验证

- GitHub Actions **全绿**：`go`（vet + test -race）/ `python 3.11` / `python 3.12` / `findings 一致性`
  （run `41317345648`，conclusion=success）
- 本机：257 项 Python 测试 + 覆盖率 81.03%；41 项 Go 测试；findings exit 0

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
- Docker 镜像**未构建验证**（开发机 Docker daemon 未运行）；CI 已真跑：首轮抓到 2 个跨平台缺陷（见下），修复后全绿。
- 本项目没有生产部署环境，"部署/热更新"只做到本地多进程启停 + 健康检查。

### 验证

| 项 | 结果 |
|---|---|
| Python 测试 | **257 passed** |
| Python 覆盖率 | **81.03%**（`--cov-fail-under=80` 通过；`service.py` 73%、`route.py` 99%） |
| Go 测试 | **41 个 Test，`go vet` + `go test -race` 全绿** |
| 结论库一致性 | `node scripts/validate-findings.mjs` → 29 条，exit 0 |
| E2E 冒烟 | `/health` ready · `/detect` → direct · SSRF 守卫拦住内网 URL |
| CI（GitHub Actions） | **全绿**：python 3.11/3.12 + go(vet/test -race) + findings |
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
