# 测试台账（test ledger）

> 一个验收项对应哪些用例、跑在哪个文件、当前是否真跑过。没跑过的不许写"通过"。
> 数字口径：**2026-11-02 · v4.7.0 · 本机 Python 3.14.3 / Go 1.26.1 / Windows 10**

## 1. 总览

| 层 | 数量 | 命令 | 最近结果 |
|---|---|---|---|
| Python 用例 | **898** | `python -m pytest tests/ -q` | 891 passed + 7 skipped |
| Python 覆盖率 | **89%**（`solver/service.py` 84%；逐模块门禁见 `docs/coverage-policy.json`） | `... --cov=solver --cov-fail-under=80` + `node scripts/check-coverage.mjs` | 门槛通过 |
| Go 用例 | **46 个 Test** | `cd gocfsolve && go test -count=1 -race ./...` | 全部 PASS |
| Go 静态检查 | — | `cd gocfsolve && go vet ./...` | 无输出 |
| 结论库校验 | **54 条** | `node scripts/validate-findings.mjs` | exit 0 |
| E2E 冒烟 | 3 个断言 | `.\run.ps1 smoke` | ✅ |
| **E2E 真进程** | **29 项断言** | `.\run.ps1 e2e`（= `python scripts/e2e_local.py`） | **29/29**，落盘 `evidence/e2e-local.json` |
| **热更新/滚动重启演练** | **17 项断言** | `python scripts/hot_update_drill.py` | **17/17**，落盘 `evidence/hot-update.json` |
| 容器 E2E | 3 worker + 6 端点 + 滚动 | `docker compose up -d` | 见 `evidence/v47-container.md` |
| 离线压测 | 3 模式 | `python scripts/loadtest.py` | hot **5959.6 QPS** / cold 16/16 / share 3 solves |

## 2. 验收项 → 用例映射

| 验收项 | 用例 | 文件 | 状态 |
|---|---|---|---|
| **F1 失效自愈**（403 → 重解 → 成功） | `TestDoWith403TriggersRevalidate` | `gocfsolve/revalidate_test.go` | pass |
| F1 只重解一次（防风暴） | `TestDoWith403RevalidatesOnlyOnce` | 同上 | pass |
| F1 缓存失效 | `TestDoWith403InvalidatesLocalCache` | 同上 | pass |
| F1 挑战页正文识别 | `TestDoWithChallengePageBodyHeuristic` | 同上 | pass |
| F1 **正文不被截断**（探测后还原） | `TestDoWithNormalBodyIsNotTruncated` | 同上 | pass |
| F1 并发只重解一次 | `TestDoWith403ConcurrentSingleRevalidate` | 同上 | pass |
| F1 开关可关闭 | `TestRevalidateDisabledKeepsPassthrough` | 同上 | pass |
| F1 不可重放 body 不重试 | `TestRevalidateSkipsNonReplayableRequestBody` | 同上 | pass |
| **F2 单飞去重**（50 并发同冷域） | `TestGetSingleFlightColdDomain` | 同上 | pass |
| F2 不同域不互相阻塞 | `TestGetSingleFlightDifferentHostsDoNotBlock` | 同上 | pass |
| F2 失败共享不悬挂 | `TestGetSingleFlightSharesError` | 同上 | pass |
| F2 leader 取消不死锁 | `TestGetSingleFlightLeaderCancelDoesNotDeadlock` | 同上 | pass |
| **TTL 契约**（`expires_at=0` 不入缓存） | `TestZeroTTLResponseIsNotCached` | 同上 | pass |
| TTL 真实值（cfsolve） | `test_cfsolve_clearance_has_real_ttl` | `tests/test_strategies.py` | pass |
| TTL 真实值（pydoll） | `test_pydoll_clearance_has_real_ttl` | `tests/test_strategies.py` | pass |
| 池：终止性错误不摘节点 | `TestPoolTerminalErrorIsNotNodeFault` | `gocfsolve/pool_v4_test.go` | pass |
| 池：过载不记故障 | `TestPoolSaturationDoesNotMarkNodeDown` | 同上 | pass |
| 池：探活发现 ready=false | `TestPoolHealthProbeMarksNotReadyNode` | 同上 | pass |
| 池：403 不牵连节点 | `TestPoolDo403DropsStaleCredentialWithoutBlamingNode` | 同上 | pass |
| **G2 缓存契约双跑**（file/sqlite） | `test_store_*[file|sqlite]` ×12 | `tests/test_cache_sqlite.py` | pass |
| G2 跨进程可见 | `test_sqlite_cross_process_visibility` | 同上 | pass |
| G2 多进程首启共用一份盐 | `test_concurrent_first_boot_shares_one_salt` | 同上 | pass |
| G2 写锁冲突可重试 | `test_sqlite_write_survives_concurrent_lock` | 同上 | pass |
| G2 盐不被 purge 删 | `test_sqlite_purge_keeps_salt_file` | 同上 | pass |
| G2 半成品盐自愈 | `test_partial_salt_file_is_repaired` | 同上 | pass |
| 落盘无明文凭证 | `test_store_encrypts_at_rest` / `test_sqlite_host_stats_are_counts_only` | 同上 | pass |
| 坏行当 miss | `test_sqlite_corrupted_blob_is_miss_and_removed` | 同上 | pass |
| 降级链 | `test_build_store_defaults_to_sqlite_when_dir_given` / `..._file_backend_is_still_available_as_escape_hatch` / `..._unknown_backend_is_not_silently_treated_as_auto` / `test_build_store_memory_backend_explicit` | `tests/test_cache.py` | pass |
| **F5/F6 治理**（SSRF/守卫不随后端变化、`/detect` 闸门） | `test_guard_hop_rejects_non_http_scheme_and_private_host`、`test_probe_blocks_redirect_to_guarded_host`、`test_detect_endpoint_returns_429_when_gate_is_full`、`test_detect_endpoint_closes_connection_on_chunked_body` | `tests/test_detect.py` | pass |
| **F5 治理不随后端变化** | `test_internal_url_rejected_before_any_backend_runs`（4 种后端组合）、`test_entry_guard_blocks_internal_url_before_any_backend` | `test_acceptance_v41.py` / `test_governance.py` | pass |
| **F9 纯协议受治理** | `test_router_backend_result_is_cached`、`..._counts_appear_in_health_and_metrics`、`..._respects_inflight_gate`、`test_router_caches_third_party_backend_result` 等 | 同上 | pass |
| **T-34 探测缓存** | `test_detect_cache_hit_skips_outbound_probe`、`..._respects_ttl`、`..._disabled_by_zero_ttl`、`..._is_bounded` | `test_governance.py` | pass |
| **F4 路由决策**（决策表 13 条） | `test_auto_*` / `test_forced_*` / `test_unknown_mode_*` / `test_direct_mode_*` | `tests/test_route.py` | pass |
| F4 路由 HTTP 行为 | `test_http_auto_skips_browser_for_unprotected_site`（solves 保持 0）、`test_http_auto_rejects_unsupported_provider_without_waiting`、`test_http_without_route_keeps_v3_behaviour`、`test_http_rejects_unknown_route_value` | 同上 | pass |
| T-33 失败原因分类 | `test_classify_failure`（7 参数）、`test_should_escalate`（5 参数） | `tests/test_route.py` | pass |
| **F8 预算守恒** | `test_router_shares_deadline_between_backends`、`test_router_stops_when_budget_is_exhausted`、`test_router_reports_timeout_when_budget_too_small_to_try_any_backend` | `tests/test_backends.py` | pass |
| **v4.7 缓存并发（P0 串域 / WAL 重试）** | `test_concurrent_reads_and_writes_never_corrupt_rows`、`test_read_paths_actually_take_the_connection_lock`（确定性机制门）、`test_wal_switch_retries_when_database_is_locked`、`test_wal_permanent_error_does_not_retry_from_the_moon`、`test_wal_degradation_is_loud_but_usable`、`test_wal_silent_downgrade_is_still_loud_and_safe` | `tests/test_cache_sqlite.py` | pass（改前必红，见 `evidence/v47-cache-concurrency.md`） |
| **v4.7 早退路径审计完整性**（畸形/空/超大 body 必须留审计行） | `test_malformed_json_is_rejected_and_still_audited`、`test_empty_body_is_rejected_and_still_audited`、`test_oversized_body_is_rejected_and_still_audited` | `tests/test_governance.py` | pass（改前必红，见 `evidence/v47-early-exit-audit.md`） |
| **v4.7 真进程交付链路**（16 + 13 = 29 断言） | `A01..A16` + `B01..B13` | `scripts/e2e_local.py` | pass 29/29 |
| **v4.7 排空 / 滚动 / blue-green** | `A01..A06` + `B01..B07` + `C01..C04` | `scripts/hot_update_drill.py` | pass 17/17 |

## 3. 还没被用例覆盖的验收项（诚实列出）

| 验收项 | 状态 | 说明 |
|---|---|---|
| F3 CI 真跑绿一次 | ✅ 已闭环 | run `41348212433`（Go / py3.11 / py3.12 / findings 四绿） |
| E4 4 worker 内存 ≤3.5GB | ✅ 已闭环 | 实测合计 **3.34 GB**（855MB/worker，含浏览器进程树） |
| E5 worker 崩溃隔离时延 | ✅ 已闭环 | 摘除 **2s** / 回归 **28s**，其余节点全程 0 失败 |
| A3 稳态命中率 ≥95% | ⚠️ 部分 | 需真实站点 + 保活长跑（受真机预算约束）；离线侧只证明"缓存命中不重复求解" |
| G1 真实 CF 站路由收益 | ⚠️ 部分 | 真机已验证 3 站：udemy 6.9s / canva 4.7s / g2 reject 1.3s；**同站双臂 A/B 未做**（预算） |
| `solver/engines.py` 覆盖率 57% | ⚠️ 记录 | cloakbrowser 二进制专有、本机未装；只能测到"缺依赖即失败"分支 |
| 真实生产部署 | ❌ 不适用 | 本仓库无生产环境/云凭证。"部署"= 本机多进程 / compose 滚动，已用 `hot_update_drill.py` 量化 |

## 4. 纪律

1. 上表"pass"只代表**本轮实际跑过并通过**，改代码后必须重跑对应层。
2. 新增验收项必须同时新增用例，否则在 `workflow_status.md` 里标 `TODO` 而不是"完成"。
3. 不许为了让门禁变绿而放宽断言；发现断言与实现矛盾时，**先判断谁错**（本轮 `TestPoolDo403*` 就是一次真实的口径冲突，最终以"403 不 blame 节点 + 清该域凭证"定稿）。
4. **in-process 用例证明不了"真进程交付链路"**：起进程、读 `.env`、401 体、`/metrics` 的 content-type、
   `202` 之后进程是否真退出、退出码、审计文件里有没有混入凭证 —— 这些只有 `scripts/e2e_local.py` 覆盖。

## 四类验收 → 可复跑脚本（v6.2）

| 验收类型 | 脚本 | 实测 | 证据落点 |
|---|---|---|---|
| UI 截图验收 | `python run.py ui`（`scripts/verify_ui_e2e.py`） | **23/23**，5 种状态 + 按钮交互 + 逐张看片 | `plans/证据/ui-*.png` |
| 真实调用验收 | `python run.py realcalls`（`scripts/verify_real_calls.py`） | **37/37** | 控制台输出 |
| 真实 E2E 验收 | `python run.py e2e`（`scripts/e2e_local.py`） | **40/40** | `evidence/e2e-local.json` |
| 服务契约 golden | `python run.py report`（`scripts/service_report.py`） | 13 端点形状一致 | `docs/golden/service-report.golden.json` |
| 真实代码复审 | `claude -p` 独立进程 | 见 `plans/证据/independent-review-v6.2-r1.md` | — |

> 这四类在 v5.0.1 是**人工跑一次**写进 `workflow_status.md` 的 —— 证明过当时那一刻，但不可复跑。
> v6.2 起全部脚本化，`run.py` 一个命令就能重来。
