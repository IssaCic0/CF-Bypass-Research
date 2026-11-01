# Workflow Status — 施工状态（唯一事实源）

> 更新：2026-11-02 · 版本 **v4.7.0** · 基线 commit `dd35453`（`master`，已含 v4.6.0 的审查修复）
> 施工图：`plans/下一步改进指南.md`（v3.0 → v4.0） · 门禁：`policy.md`
>
> **读这份文件的正确姿势**：§2 是任务卡状态，§3 是门禁实测值，§4 是验证日志，§7 是**还没闭环的东西**。
> §7 里没关掉的项，任何地方都不许写成"已完成"。

---

## 1. 任务契约

| 项 | 内容 |
|---|---|
| 目标 | 把 v4.0 指南里的 P0/P1 全部落地并**真实验证**：治理收口、客户端自愈、缓存升级、观测、工程化 |
| 用户授权 | 全部阶段自主执行（含提交/推送/tag/发布）；`git push --force` 永久禁止 |
| 验收标准 | `policy.md` §1 的 7 项门禁 + 指南 §5 的 SLO 表逐项有证据 |
| 不在范围 | 生产部署（本仓库无生产环境/无云凭证）；真实站点压测（受 §15 预算约束） |
| 协作说明 | 本仓库同期有**另一个并行会话**在改 Python 侧（T-28/T-29/T-30/T-31/T-35/T-38），两方按文件分工，见 §6 |

---

## 2. 任务图与状态

### W1 止血（P0/P1）

| ID | 内容 | 负责 | 状态 | 证据 |
|---|---|---|---|---|
| T-28 | 守卫/准入/指标上提到入口（所有后端共用） | 并行会话声称完成 → **v4.1 实测不成立，本会话真修** | **DONE（2026-11-01 23:xx 复验）** | 入口守卫：`service.py` do_POST 内 `_check_target`（选后端之前）；用例 `tests/test_acceptance_v41.py::test_internal_url_rejected_before_any_backend_runs`（4 种后端组合） |
| T-29 | 后端治理收口（共享 store/inflight/计数） | 并行会话声称完成 → **v4.1 实测不成立，本会话真修** | **DONE（复验）** | `BackendRouter(governance=)` + `_governed_solve`；`tests/test_governance.py`（12 例）+ `test_acceptance_v41.py` 组 B |
| T-30 | cfsolve/pydoll 真实 TTL 契约 | 并行会话 | **DONE** | `test_cfsolve_clearance_has_real_ttl` / `test_pydoll_clearance_has_real_ttl` |
| T-31 | `/detect` 逐跳守卫 + 独立准入 + 非 http(s) 拒绝 + chunked 拒绝 | 并行会话 | **DONE** | `test_probe_blocks_redirect_to_guarded_host`、`test_detect_endpoint_returns_429_when_gate_is_full` |
| T-35 | 超时预算分配（router 共享 Deadline） | 并行会话 | **DONE** | `test_router_shares_deadline_between_backends`、`test_router_stops_when_budget_is_exhausted` |
| **T-10** | Go 回放 403 自愈（失效→重解一次→重放；含 body 还原、并发收敛、跨域并发上限） | 本会话 | **DONE** | `gocfsolve/revalidate_test.go`（13 例）· `evidence/T-10.json` |
| **T-09** | Go per-host 单飞 | 本会话 | **DONE** | `revalidate_test.go` 4 例 · `evidence/T-09.json` |
| **T-07b** | Go 池：终止/过载错误分类 + 探活 `!ready` + 403 清凭证 | 本会话（并行会话补 403 语义） | **DONE** | `pool_v4_test.go`（5 例）· `evidence/T-07b.json` |

### W2 新能力

| ID | 内容 | 负责 | 状态 |
|---|---|---|---|
| **T-32** | `route=auto` 自动路由闭环（探测→决策→求解/skip/reject） | 本会话 | **DONE** | `solver/route.py`、`tests/test_route.py`（30 例）· `evidence/T-32.json` |
| **T-33** | 失败原因分类（`classify_failure` + `should_escalate`） | 本会话 | **DONE（决策层）** | `solver/route.py`；升级阶梯的**执行**仍由既有 router 按顺序完成 |
| **T-43** | **优雅退出（`/shutdown` + 在途排空 + 滚动重启脚本）** | 本会话 | **DONE** | 实测：修复前在途请求 `WinError 10054`，修复后 **200 + solved**；`evidence/v42-stability.md` §1 |
| **T-45** | **句柄泄漏：缓存层 thread-local SQLite 连接 → 共享连接** | 本会话 | **DONE** | 1h soak 泄漏 +1308/线程数不变；归因到"每条新 TCP 连接 +3 句柄"；修复后 +100 连接 **+0**。见 `evidence/v42-handle-leak.md` |
| **T-44** | 失败响应回传 `routed_by` | 本会话 | **DONE** | 真机 60s 未解出时 `routed_by` 全空 → 已修，用例 `test_routed_by_survives_failure` |
| T-34 | `/detect` 结果按 host 短 TTL 缓存 | 本会话 | **DONE** | `--detect-cache-ttl`（默认 300s，0=关）；`fresh=1` 跳过；`tests/test_governance.py` 4 例 |
| **T-38b** | 代理/出口绑定透传到 Go 回放侧 | 本会话补文档+验证 | **DONE** | `egress_test.go` 4 用例（含『请求确实经过代理』与『未登记出口 fail closed』）；端到端配方（服务 --proxy → egress_id → WithEgressProxy）写进 `docs/部署-容器.md` §4.5 |

### W3 缓存与观测

| ID | 内容 | 负责 | 状态 | 证据 |
|---|---|---|---|---|
| **T-36** | 缓存层 SQLite(WAL) + FileStore 降级 | 本会话 | **DONE** | `solver/cache.py::SqliteStore`、`tests/test_cache_sqlite.py`（17 例）· `evidence/T-36.json` |
| T-37 | 指标升级（后端维度/按域统计/口径修正） | 本会话 | **DONE** | `/metrics` 新增 `cfsolve_backend_total{backend,result}`、`cfsolve_host_solves_total`（top-20 截断）；`solves_total` 口径改为"含失败" |
| **T-38** | 审计日志（`--audit-log`，JSONL + 字段白名单） | 本会话 | **DONE** | 3 用例（白名单不含凭证 / 白名单外字段丢弃 / 写失败不打断请求）· `evidence/v43-acceptance.md` §3 |

### W4 工程化

| ID | 内容 | 状态 | 证据 |
|---|---|---|---|
| **T-39** | CI 三 job（python 覆盖率门槛 / go vet+race / findings），全程离线 | **DONE** | `.github/workflows/ci.yml`、`requirements-dev.txt` · `evidence/T-39.json` |
| **T-40** | `scripts/loadtest.py`（hot/cold/share）+ `scripts/doctor.py` | **DONE** | 实跑数据见 §4 · `evidence/T-40.json` |
| **T-41** | 统一入口 + Dockerfile/compose | 本会话 | **DONE** | 构建成功 1.61GB；容器 **5s 就绪**（修复前 110-120s）；`/health`/`/detect`/SSRF/401/`route=auto`/`/shutdown` 六项全过；优雅退出 `exit=0` · `evidence/v44-container.md` |
| **C3** | 删除研究产物 + 修 findings 证据链 | **DONE** | `node-shim/`、`go-jseng/`、`go-replay/` 已 `git rm`；`validate-findings.mjs` exit 0 |
| C1/C2 | 文档纠偏 / `docs/项目规格.md` | 本会话 | **DONE** | `docs/项目规格.md`（单一事实源）+ `docs/部署-容器.md` + `docs/运维-压测与自检.md` |
| **T-42** | 发版 v4.0.0（tag + CHANGELOG + release） | **DONE** | 见 `git tag v4.0.0`、`CHANGELOG.md` |

---

## 3. 验收标准表（v4.7 门禁，全部为本轮实跑）

| 门禁 | 目标 | 实测 | 结论 |
|---|---|---|---|
| Python 全量测试 | 0 failed | **304 passed**（35.2s） | ✅ |
| Python 覆盖率 | ≥80%（service.py ≥70%） | **82.06%**（service.py 76%） | ✅ |
| Go 测试 | 0 failed 且 `-race` | **41 个 Test 全过，`-count=1 -race` 绿**（8.3s） | ✅ |
| Go 静态检查 | `go vet` 无输出 | 无输出 | ✅ |
| Python 语法 | compileall 退出 0 | 0 | ✅ |
| 结论库 | `validate-findings.mjs` exit 0 | **38 条**（32/5/1）exit 0 | ✅ |
| 冒烟（E2E） | `/health` ready + `/detect` + SSRF 守卫 | 三项全绿（`run.ps1 smoke`） | ✅ |
| **真进程 E2E** | 全绿 | **29/29**（`run.ps1 e2e`）· `evidence/e2e-local.json` | ✅ |
| **热更新/滚动演练** | 全绿 | **17/17** · `evidence/hot-update.json` | ✅ |
| **容器 E2E（3 worker）** | 就绪 + 6 端点 + 滚动 + 优雅退出 | 3s 就绪 / 6 项全过 / 150-0 失败 / 202+exit 0 · `evidence/v47-container.md` | ✅ |
| 回放吞吐（E2） | ≥50 QPS | **5615.6 QPS**（p99 2.92ms，28088 请求 0 错） | ✅ |
| 冷路径准入（E1/F2） | 不被拒、每请求 1 次求解 | 16/16 accepted，solves_per_request **1.0** | ✅ |
| 多进程去重（G2/A3） | 先后 + 同时到达都只解 1 次 | sequential **3/3 轮**；burst **3 次**（1 次/轮） | ✅ |
| 路由收益（G1 机制） | 无盾站不启浏览器 | `route=auto`：`no_challenge=true`、`solves` 计数不动、探测缓存命中后 `elapsed=0.0` | ✅ |

---

## 4. 验证日志（全部为本轮真实执行）

| # | 命令 | 结果 |
|---|---|---|
| V1 | `python -m pytest tests/ -q --cov=solver --cov-fail-under=80` | **227 passed / 80.47% / exit 0** |
| V2 | `cd gocfsolve && go vet ./... && go test -race ./...` | `ok`（41 Test） |
| V3 | `python -m compileall -q solver scripts tests` | exit 0 |
| V4 | `node scripts/validate-findings.mjs` | 29 条，exit 0 |
| V5 | `python scripts/doctor.py --port 8907` | 服务 **24.3s** 后 ready，`cache=sqlite:...`，exit 1（仅 2 项 WARN：token 未生成、cfsolve 未配置） |
| V6 | `.\run.ps1 smoke -Port 8911 -NoAuth` | `/health` ready · `/detect` → `direct` · SSRF 守卫拦住内网 URL |
| V7 | `python scripts/loadtest.py --mode hot --workers 8 --duration 5` | **2874.9 QPS**，p50 2.64 / p90 4.18 / p99 5.91 ms，0 错 |
| V8 | `python scripts/loadtest.py --mode cold --workers 16 --solve-delay 0.05` | 16/16 accepted，`solver_calls=16`，`solves_per_request=1.0` |
| V9 | `python scripts/loadtest.py --mode share --processes 4 --iterations 3` | sequential **3 solves / 9 hits / 0 fail**（1 次/轮）；burst 12 solves（见 §7 已知限制） |
| V10 | `.\run.ps1 verify` | 全部通过 ✅ |
| V11 | 端到端服务（真实 camoufox） | 起服务 → `ready`，`cache_backend=sqlite:...`（证明 SQLite 缓存真的接上了生产路径） |
| V12 | **`route=auto` 真机** | `POST /solve {url:https://example.com/, route:auto}` → `no_challenge:true, elapsed=0.55s`，`solves` 计数保持 0 |
| V13 | 对照：同 URL 不传 route | 真去求解，20s 后 `unsolved`，`solves=1` ⇒ 路由收益 **0.55s vs 20s**（同一 URL、同一服务） |
| V14 | **GitHub Actions 真跑（首轮）** | **红**（这正是它存在的意义）：python(3.12) job 抓到 2 个本机发现不了的缺陷（N-06 prewarm 覆盖 `Thread._stop`、N-07 salt.bin 被 glob 命中） |
| V15 | **GitHub Actions 真跑（修复后）** | **全绿**：`go` ✓ · `python (3.11)` ✓ · `python (3.12)` ✓ · `findings 一致性` ✓（run 41317345648） |
| V16 | **同站双臂 persona A/B（真机）** | 固定 9/9 求解 8/9 回放、均 **6.0s**；随机 8/9、8/9、均 **11.8s**；glassdoor **快 2.02×**。失败 2 次（未触发停止条件）。证据 `plans/证据/v41-persona-ab.json` |
| V17 | **真实 E2E：Go→服务→glassdoor.co.uk→回放** | `status=200 长度=400000 挑战页=false 耗时=1.93s`，二次调用 `cached=true` |
| V18 | **治理修复后的实测** | 内网 URL → 后端 0 次调用；纯协议第 2 次 `cached:true`；`/metrics` 出现 camoufox 与 host 维度 |
| V19 | **并发（缓存命中）** | 32×20 = 640 请求 0 错，**396.3 QPS**，p99 1017ms（A2 目标 ≤2s） |
| V20 | **冷路径准入** | 8 并发 / `--max-inflight 2`：6×429 busy（2-3ms）+ 2 准入（6.8s / 13.5s 串行） |
| V21 | **优雅退出** | 修复前 `WinError 10054`；修复后 `/shutdown` → 在途 7.02s 真求解拿到 **200 + solved** |
| V22 | **route=auto 真实 CF 站** | steamdb.info：`cloudflare/managed` → `action=solve,prefer=pure` → **7.47s / 460 字节** ✅；glassdoor 60s 未解（疑似 IP 提分，已按纪律停） |
| V23 | **长跑 soak（1h，修复前）** | 360 采样：轻量端点 **0 错**、**整整 1 小时 ready**、RSS 1534→427MB（回落）；**句柄 2787→4095（+1308）** → 判 FAIL，抓到真缺陷 T-45 |
| V24 | **句柄泄漏归因** | 100×`/health` → +300；100 连接×1 请求 → +296；1 连接×100 请求 → +7；最小 stdlib 服务 +100 连接 → +16 ⇒ 泄漏在我们代码：`SqliteStore` thread-local 连接（WAL 每连接 3 个文件句柄） |
| V25 | **修复后复测** | 同一手法：+100 `/health` → **+0**；+100 `/metrics` → **+0** |
| V26 | **修复后 soak（15 分钟）** | **passed: true** — 0 错 / 全程 ready / RSS 回落 396.7MB / **句柄 2788→2746（净 -42）** |
| V27 | **E4：4 worker 内存** | 合计 **3.34 GB**（855MB/worker，含浏览器进程树）≤ 3.5GB ✅ |
| V28 | **E5：崩溃隔离演练（真实杀进程）** | 摘除 **2s**（maxFailures=2 + 探活 2s）、回归 **28s**（受 30s downDuration 支配），其余节点全程 f=0 ✅ |
| V29 | **跨进程去重** | `loadtest share` burst：**3 轮 3 次求解**（修复前 12 次） |
| V30 | **route=auto 真机 3 站** | udemy 6.9s/459B、canva 4.7s/330B（solve）；**g2 → reject 1.3s**（DataDome，不再白等 45s） |
| V31 | **容器化真跑** | build 1.61GB；容器 **5s 就绪**（修复前 110-120s）；六项端点全过；`/shutdown` → 202(peer=172.17.0.1) → `exit=0` |
| V32 | **容器专属缺陷 N-11/N-12** | N-11 fetch 落 root HOME→每容器重下载（110s→5s）；N-12 `/shutdown` 仅回环在端口映射下失效（加 `--allow-remote-shutdown`+回归用例） |
| V33 | **compose 三 worker** | 20s 3/3 healthy；cache/ua_fingerprint 一致；内存 2.26GB；跨容器 try_claim 1真2假 |
| V34 | **镜像瘦身** | 4.63GB → **3.27GB**（SLIM_FONTS=1），容器 10s 就绪 + /detect 200（必须验活，BuildKit 缓存曾给出无浏览器镜像） |
| V35 | **真机预算 60/60 用尽** | patreon skip 1.2s；epicgames solve 8.7s/462B；epicgames 回放 403（脚本 TLS/头部不一致，未判为服务缺陷） |
| V36 | **service.py 拆分第一步** | 2452 → 2310 行；拆出 probe.py(135)/auth.py(77)，原样再导出，297 测试全绿 |
| V37 | **独立审查第 1 轮** | `claude -p` 独立进程 → **Request Changes**：P1 `_account` 双计、P1 claims 泄漏 3 条路径、P2 `/shutdown` 拒绝无响应、P2 cfsolve expires 矛盾、P2 锁注释不符 |
| V38 | **修复 + 回归用例** | 4 处修复全部带"改前必红"用例（300→302 测试，覆盖率 81.98%） |
| V39 | **独立审查第 2 轮复验** | **Approve**（4 FIXED + 1 PARTIAL + 4 P3）；PARTIAL 与 2 条 P3 也已修 |
| V40 | **回放一致性（Go 客户端，同 IP）** | epicgames：带凭证 **301/challenge_page=false**，不带凭证 **403** ⇒ 凭证有效、CF 已过；此前脚本 403 是客户端头部/域名不匹配，**不是 TLS 指纹、也不是 IP 提分** |

---

## 4.5 修正记录（撤回"假 Done"，必须留痕）

| 时间 | 撤回项 | 原依据 | 实测反证 |
|---|---|---|---|
| 2026-11-01 22:4x | T-28 完成 | 并行会话 commit message「守卫上提到入口」 | `_check_target` 全仓仍只有 2 个调用点；配 `--cfsolve-bin` 后假二进制实收内网 URL |
| 2026-11-01 22:4x | T-29 完成 | 同上 | `BackendRouter.__init__(backends)` 不接治理对象；实测 `solves=0`、第二次 `cached:false` |
| 2026-11-01 22:4x | T-37 部分完成 | 代码里存在 `host_stats` | 无任何调用点（死代码） |

**根因**：把并行会话的提交信息当成事实，且验收用例 F5（"任意后端组合下内网 URL 一律被拒"）
从未被写出来 —— 没有红灯，就没被发现。**修法**：红灯先行 + 只信实测。

## 5. 本轮新发现并修复的缺陷（进 ICM 与 findings）

| # | 缺陷 | 触发条件 | 证据等级 |
|---|---|---|---|
| **N-01** | **`/solve` 在配置额外后端时绕过 SSRF 守卫**（守卫写在 `Solver.solve` 内，handler 走 router 分支不进该方法） | 配 `--cfsolve-bin`/`--pydoll`/`--flaresolverr` 任一 | **已验证**（假 cfsolve 二进制实收 `--page http://127.0.0.1:8903/health`） |
| **N-02** | cfsolve/pydoll 返回 `expires_at=0` ⇒ Go 侧每次回放都重新求解 | 启用这两个后端 | 静态确认 → 已修（并行会话） |
| **N-03** | **多进程首启争抢建盐文件时 `os.replace` 抛 `PermissionError`（WinError 5）**，4 worker 同启约 1/4 概率起不来 | 空缓存目录 + 多进程同时启动 | **已验证**（loadtest burst 阶段抓到）→ 已修为 `O_CREAT\|O_EXCL` 抢占 |
| **N-04** | `FileStore.items()` 不过滤过期项 ⇒ `/health.cached_domains` 虚高 | 缓存里有过期项 | 已验证（新用例抓到）→ 已修 |
| **N-05** | 多进程**同时**抢同一新域会各解一次（无跨进程互斥） | 4 worker 同时收到新域 | **已验证**（burst 12/12）→ **未修**，列为已知限制（见 §7） |
| **N-06** | `Prewarmer` 用 `self._stop = threading.Event()` **覆盖了 `Thread._stop` 私有方法**，3.12 上线程收尾抛 `TypeError: 'Event' object is not callable` | 保活线程退出 | **已验证（CI, Linux/py3.12 抓到；本机 3.14 复现不出）** → 已改名 `_stop_event` |
| **N-07** | `test_filestore_corrupt_file_*` 用 `glob("*.bin")` 取文件，Linux 上先拿到 `salt.bin` ⇒ 用例实际在弄坏盐而不是缓存条目 | 非 NTFS 目录序 | **已验证（CI, Linux 抓到）** → 改用排除盐的 `cache_files()` 助手 |

---

## 6. 文件归属（并行会话协作，防止互相覆盖）

| 范围 | 归属 |
|---|---|
| `solver/service.py`、`backends.py`、`errors.py`、`cfsolve_backend.py`、`pydoll_backend.py`、`tests/test_service.py|test_backends.py|test_detect.py|test_strategies.py`、`docs/API.md` | 并行会话 `cf-bypass-research-72` |
| `gocfsolve/cfsolve.go`（单飞/自愈/并发上限）、`revalidate_test.go`、`solver/cache.py`、`tests/test_cache.py`、`tests/test_cache_sqlite.py`、`scripts/doctor.py`、`scripts/loadtest.py`、`.github/`、`Dockerfile`、`docker-compose.yml`、`run.ps1`、`run.bat`、`policy.md`、`workflow_status.md`、`test_ledger.md`、`evidence/`、`plans/`、README | 本会话 |
| `gocfsolve/pool.go`、`pool_v4_test.go`、`egress_test.go` | 两边都碰过，以当前工作区为准（已跑绿） |

---

## 7. 剩余风险与阻塞（诚实披露）

| # | 项 | 状态 | 原因 / 下一步 |
|---|---|---|---|
| R1 | `route=auto` 的 `prefer` 只是提示 | 部分闭环 | `prefer=pure/browser` 未实现"按偏好跳过某个后端"（后端链顺序仍由配置决定）；要真正跳过需要 `BackendRouter` 支持 skip 集合 |
| R2 | **跨进程 thundering herd 未修** | 已知限制 | 4 worker 同时抢新域 = 4 次求解（实测 12/12）。修法：`O_EXCL` 锁文件当选主 / SQLite 里加 `claims` 表。**代价 **> 收益时不该急修**（稳态 ≈0.08 QPS，撞车概率低） |
| **R3** | ~~Docker 镜像未构建验证~~ | **已闭环（v4.7）** | 本机 Docker 29.9.2 真构建 `cf-bypass-solver:4.7.0` = 3.54GB，exit 0；3 worker compose 3s 就绪、6 端点全过、滚动 150 次请求 0 失败、`/shutdown` 202+exit 0。见 `evidence/v47-container.md` |
| **R4** | ~~无生产部署环境~~ | **如实降级为"本机部署语义"** | 仓库确实没有服务器清单/云凭证，所以"上线"= 本机多进程 / compose 滚动。**这条不再是"未验证"，而是"它不是生产部署"**：`scripts/hot_update_drill.py` 把 policy §6 的三条语义（排空 / 逐个摘 / 新进程先行）做成了 17 项断言，可重复跑 |
| R5 | 真机预算已用 ≈46/60 | 部分完成 | 剩余 ~14 次留给"真实 CF 站上的 route=auto 收益"；persona A/B 与 E2E 已完成（V16/V17） |
| R6 | `docs/项目规格.md`（C2）未建 | 未闭环 | 成本 3h，收益是"跨会话不用重新考古"；优先级低于 T-32 |
| R7 | `service.py` 仍 1846 行（>800 规范） | 记录 | 已降级为 P3（拆分风险 > 收益）；本次未动 |
| R8 | `engines.py` 覆盖率 57% | 记录 | cloakbrowser 二进制专有、本机未装，只能测到"缺依赖即失败"分支 |
| **R12** | ~~跨进程同时抢同一新域各解一次~~（v4.4 已修） | 已闭环 | `claims` 抢占；burst 12→3（1 次/轮） |
| R9 | ~~句柄长期增长~~（v4.2.1 已修） | 已闭环 | 见 T-45：缓存层改共享连接后不再随连接增长；仍建议生产按周看 `num_handles` 曲线 |
| R9b | **glassdoor.co.uk 疑似出口 IP 已提分** | 已停止实验 | 同站当天被连打 12 次后 60s 解不出；换出口再验证，不要在同一出口反复打同一站 |
| R10 | 缓存命中路径在 32 并发下 p99 ≈1s | 已量化 | 单进程建议 ≤16 并发；要更高稳态吞吐请加进程 |
| R11 | geoip 已否决（有证据） | 决策完成 | camoufox `geoip=True` 会 `download_mmdb()` 下载外部数据库（`geolocation.py`），按指南 §8 否决 |

---

## 8. 下一步（v4.7 之后，按"收益 / 成本"排序）

> 上面 v4.0 时代的五条已经全部落地（T-34 探测缓存、T-37 指标接线、C2 项目规格、T-42 发版、
> findings 回填 F-030…F-038）。下面是**真正还没做**的：

| # | 事项 | 为什么现在不做 | 怎么做才算闭环 |
|---|---|---|---|
| N1 | **A3 稳态命中率 ≥95%** | 需要真实站点 + 保活长跑，受 §15 真机预算约束 | 起 4 worker + `--prewarm`，跑 24h，采样 `cache_hits / (hits+solves)` |
| N2 | **G1 同站双臂 A/B**（route=auto 的收益） | 需要同一站点两组对照，预算已用大半 | 固定站点 + 固定 persona，A 组带 route、B 组不带，各 10 次 |
| N3 | `solver/engines.py` 覆盖率 57% | cloakbrowser 二进制专有、本机未装 | 只测"缺依赖 → NOT_CONFIGURED + 安装提示"这条；已覆盖，剩余分支无法在本机跑 |
| N4 | `service.py` 仍 2300+ 行（>800 规范） | 拆分风险 > 收益，历史上分过两次（probe/auth） | 按"请求解析 / 鉴权 / 路由 / 后端执行"四块继续下沉，每步保持 304 测试全绿 |
| N5 | 超大 body 返回 `400` 而非 `413` | 文档未承诺 413，改了就是**扩大契约面** | 若要改：先定契约（`docs/API.md`）+ 改 `STATUS_BY_CODE` + 加用例，再改行为 |

**不要做的事**：把 `route=auto` 默认打开（向后兼容第一优先级）；换掉 camoufox；引入 Redis/PG。

---

## 9. v4.7.0 本轮做了什么（2026-11-02）

这一轮**没有加新功能**，只做三件事：把"没验证过的"变成**可复现的断言**、修掉两个只有真跑才会暴露的缺陷、清掉文档里的过期结论。

### 9.1 修掉的两个真缺陷

| # | 缺陷 | 怎么发现的 | 修法 | 证据 |
|---|---|---|---|---|
| **N-14** | `do_POST` 的三条早退路径（空 body / 畸形 JSON / 超大 body）在 `url` 赋值之前抛异常，而 `except` 无条件读 `url` → `UnboundLocalError` **打穿 `_log`**：响应仍是 400（所以状态码用例全绿），但**审计里一条都没有**，且每个畸形请求打一整段 traceback | 新写的**真进程 E2E** 在「服务日志不许出现 Traceback」上变红 | try 之前绑定 `url = ""`；两条「改前必红」用例 | `evidence/v47-early-exit-audit.md` |
| **N-13** | 镜像承诺"运行期不联网下载浏览器"，但 camoufox 的 uBlock Origin **不在** `camoufox fetch` 产物里，是**首次启动**才下载（18MB）。离线环境浏览器永远起不来；slim 分支还有一句 `rm -rf addons/*/` 反向删掉它 | 容器 `docker logs` 里看到 `Downloading addon (UBO) 0%→100%` | build 阶段 headless 拉起一次浏览器预热插件（`PREWARM_ADDONS=1`）；删掉那句 rm | `evidence/v47-container.md` §2 |

### 9.2 新增的三个可重复验收件

| 件 | 覆盖什么（in-process 用例覆盖不到的） | 断言数 |
|---|---|---|
| `scripts/e2e_local.py` + `run.ps1 e2e` | **真进程交付链路**：起两个真服务（严格/宽松）、读 `.env` 的 token、401 响应体、`/metrics` content-type、`/health` 免鉴权**不泄露**内部字段、`route=auto` 不启浏览器、真跑一次浏览器拿结构化失败、**审计里没有 token**、`/shutdown` 202 + 退出码 0 | **29** |
| `scripts/hot_update_drill.py` | policy §6 的部署语义：**排空**（在途 6.07s 请求在 `/shutdown` 后仍拿到 200）、**滚动期另一个 worker 零失败**、**blue-green**（新进程先就绪才摘旧的） | **17** |
| 容器验收（compose 3 worker） | 3s 就绪 / 6 端点 / 滚动 150 次请求 0 失败 / `/shutdown` 202+exit 0 / 交叉 token 一致 | 见 `evidence/v47-container.md` |

`run.ps1 verify` 现在把 e2e 也串进去了（提交前必跑 = 测试 + 语法 + Go + findings + **E2E**）。

### 9.3 文档纠偏（这一轮清掉的"过期结论"）

| 位置 | 原来写的 | 真实情况 |
|---|---|---|
| `README.md` ×7 | `.un.ps1 doctor`（`\r` 被写成了真的 CR 字节，命令根本敲不出来） | `.\run.ps1 doctor` |
| `README.md` | "4 个 worker 同时抢新域会各解一次（没有跨进程互斥）" | v4.4 起 `claims` 抢占已修，burst 12 → **3**（1 次/轮） |
| `README.md` | 「跑实验」教人 `cd node-shim` / `go run go-jseng` / `go run go-replay` | 这三个目录已在 D-3 决策里 `git rm` |
| `Dockerfile` / `docker-compose.yml` | 「⚠️ 未在本机验证（Docker daemon 未运行）」 | 已在本机实测（§9.2） |
| `docker-compose.yml` | `image: cf-bypass-solver:4.0.0` | 对齐 `4.7.0` |
| `test_ledger.md` §1 | 279 用例 / 81.39% | **304** / **82.06%** |
| `test_ledger.md` §3 | F3 CI / E4 内存 / E5 崩溃隔离"未测" | 三项均已闭环（run 41348212433 / 3.34GB / 摘除 2s 回归 28s） |
| `workflow_status.md` §8 | 五条"下一步"其实都已完成 | 换成真正未做的 N1–N5 |
| `docs/00-索引.md` | findings 29 条 | **38 条**（补 F-030…F-038：N-01…N-07 + N-13 + N-14） |

### 9.4 findings 回填（§8 老版第 4 条的遗留）

`docs/findings.json` 29 → **38 条**，把历史上只在 `workflow_status.md` 口口相传的 7 个缺陷
（N-01 SSRF 绕过 / N-02 TTL 自相矛盾 / N-03 盐并发 / N-04 过期项虚高 / N-05 跨进程重复求解 /
N-06 覆盖 `Thread._stop` / N-07 假绿用例）正式登记，并加上本轮两条。

### 9.5 边界（仍然没做的，别读成"已闭环"）

- 没有生产环境：本轮的"部署"是 compose + 本机多进程滚动，**不是**线上灰度/回滚；
- A3 稳态命中率、G1 同站双臂 A/B 仍受真机预算约束（§8 N1/N2）；
- 容器侧未压测"浏览器并发求解"的稳态吞吐（离线压测只覆盖回放与准入）。
