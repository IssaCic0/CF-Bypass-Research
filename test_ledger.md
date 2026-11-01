# 测试台账（test ledger）

> 一个验收项对应哪些用例、跑在哪个文件、当前是否真跑过。没跑过的不许写"通过"。

## 1. 总览

| 层 | 数量 | 命令 | 最近结果 |
|---|---|---|---|
| Python 用例 | **279** | `python -m pytest tests/ -q` | 279 passed |
| Python 覆盖率 | **81.39%** | `... --cov=solver --cov-fail-under=80` | 门槛通过 |
| Go 用例 | **41** | `cd gocfsolve && go test -race ./...` | 全部 PASS |
| 结论库校验 | 29 条 | `node scripts/validate-findings.mjs` | exit 0 |
| E2E 冒烟 | 3 个断言 | `.\run.ps1 smoke` | ✅ |
| 离线压测 | 3 模式 | `python scripts/loadtest.py` | 见 `evidence/T-40.json` |

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
| **F5/F6 治理**（SSRF/守卫不随后端变化、`/detect` 闸门） | `test_guard_hop_rejects_non_http_scheme_and_private_host`、`test_probe_blocks_redirect_to_guarded_host`、`test_detect_endpoint_returns_429_when_gate_is_full`、`test_detect_endpoint_closes_connection_on_chunked_body` | `tests/test_detect.py` | pass（并行会话实现） |
| **F5 治理不随后端变化** | `test_internal_url_rejected_before_any_backend_runs`（4 种后端组合）、`test_entry_guard_blocks_internal_url_before_any_backend` | `test_acceptance_v41.py` / `test_governance.py` | pass |
| **F9 纯协议受治理** | `test_router_backend_result_is_cached`、`..._counts_appear_in_health_and_metrics`、`..._respects_inflight_gate`、`test_router_caches_third_party_backend_result` 等 | 同上 | pass |
| **T-34 探测缓存** | `test_detect_cache_hit_skips_outbound_probe`、`..._respects_ttl`、`..._disabled_by_zero_ttl`、`..._is_bounded` | `test_governance.py` | pass |
| **F4 路由决策**（决策表 13 条） | `test_auto_*` / `test_forced_*` / `test_unknown_mode_*` / `test_direct_mode_*` | `tests/test_route.py` | pass |
| F4 路由 HTTP 行为 | `test_http_auto_skips_browser_for_unprotected_site`（solves 保持 0）、`test_http_auto_rejects_unsupported_provider_without_waiting`、`test_http_without_route_keeps_v3_behaviour`、`test_http_rejects_unknown_route_value` | 同上 | pass |
| T-33 失败原因分类 | `test_classify_failure`（7 参数）、`test_should_escalate`（5 参数） | 同上 | pass |
| **F8 预算守恒** | `test_router_shares_deadline_between_backends`、`test_router_stops_when_budget_is_exhausted`、`test_router_reports_timeout_when_budget_too_small_to_try_any_backend` | `tests/test_backends.py` | pass（并行会话实现） |

## 3. 还没被用例覆盖的验收项（诚实列出）

| 验收项 | 缺什么 |
|---|---|
| G1 真实 CF 站路由收益 | 需 §15 预算；本轮只验证了"无盾站 skip"（真机 example.com） |
| F3 CI 真跑绿一次 | 工作流文件已写，**未在 GitHub 上真跑过**（本机无法触发） |
| E4 4 worker 内存 ≤3.5GB | 未测（需 4 进程长跑采样） |
| E5 worker 崩溃隔离时延 | 未演练（需手工杀进程） |
| A3 稳态命中率 ≥95% | 需要真实站点 + 保活长跑（受 §15 预算） |
| G1 真实 CF 站路由收益 | 需 §15 预算；本轮真机只验证了「无盾站 0.55s skip」 |

## 4. 纪律

1. 上表"pass"只代表**本轮实际跑过并通过**，改代码后必须重跑对应层。
2. 新增验收项必须同时新增用例，否则在 `workflow_status.md` 里标 `TODO` 而不是"完成"。
3. 不许为了让门禁变绿而放宽断言；发现断言与实现矛盾时，**先判断谁错**（本轮 `TestPoolDo403*` 就是一次真实的口径冲突，最终以"403 不 blame 节点 + 清该域凭证"定稿）。
