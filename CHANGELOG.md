# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [6.0.1] - 2026-11-04

主题：**修 CI 红（Python 3.11 语法兼容）**。无行为变更。

### 修复

- `run.py` 的 `cmd_stop` 里有一处 **跨行 f-string**（`f"...{... if x
 else ...}..."`）。
  PEP 701（f-string 内换行）是 **Python 3.12+**，CI 的 `python (3.11)` job 直接
  `SyntaxError: unterminated string literal` → 测试与覆盖率门槛失败。
  本机 3.14 能跑，所以本地全绿掩盖了它 —— 这正是 CI 存在的价值。
- 已改为先算 `reason` 再插值。

### 防复发：没有可靠的本地方案，只能靠 CI 的 3.11 job

先试过 `ast.parse(source, feature_version=(3, 11))`，**实测它放过了那个真实的失败样例** ——
`feature_version` 不覆盖 Python 3.12 引入的新 f-string tokenizer（PEP 701 是词法层变化）。
一个"看起来能拦、实际拦不住"的门禁比没有门禁更坏，所以**没有把它留在仓库里**，也没写进 CI。

结论（如实）：这类版本兼容问题**本机无可靠检测手段**，唯一有效的是 CI 的
`python (3.11)` job —— 它这次确实抓到了，也正是它存在的价值。

### 验证

Python 652 passed / 7 skipped；覆盖率 87.5%；21 模块达标；openapi 13 操作；文档 9 处一致。

## [6.0.0] - 2026-11-04

主题：**研究工程化 —— 把「CF 改版就要重新赌一把」变成「样本驱动、离线可复现的回归工程」**。

> **breaking change：无。** 本版对既有端点为**纯增量**：新增 `GET /console`、响应头
> `X-Request-Id`、`/health` 的 `research` 段、`/metrics` 三条 `cfsolve_research_*` 序列。
> 既有字段、状态码、错误结构、鉴权语义均未改。

### 核心交付：研究台（`scripts/research.py` + `scripts/corpus.py`）

CF 改版后，"哪条结构假设破了"过去只能靠真机跑一遍看报错。本版把这件事变成离线可查：

| 能力 | 命令 | 本轮实测 |
|---|---|---|
| 构建前置自检（G0 门禁） | `research.py doctor` | **EXIT=0，13 项全 PASS** |
| 编译 5 个离线分析 example | `research.py build` | **EXIT=0 / 4m24s** |
| 结构断言矩阵 | `research.py assertions` | 真样本上 **pass=2 fail=1 skip=6**（skip 有明确原因） |
| 两份快照差分 | `research.py diff <a> <b>` | 真跑报出 **`counter: pass → fail`** 并映射到补丁 |
| VM 原语缺口清单 | `research.py gaps <id>` | 真样本 **783 调用点 / 105 缺口 / 35 已实现** |
| 预算护栏 + 阶段推断 | `research.py run --max-mb --max-sec` | 5 态表驱动用例 |
| 补丁集演进 | `research.py patch --check` | 真实报出 2 份补丁的冲突文件与行号 |
| 会话快照归档/脱敏 | `corpus.py add/list/verify/redact` | 真实样本入库，`redact_hits=0` |

### 新增

- `GET /console`：**单文件只读控制台**（`solver/console_html.py`，零依赖零构建、CSP、无 innerHTML、
  无内联事件）。五条硬约束各有用例；**Playwright 真浏览器 E2E 通过**并留截图。
- `X-Request-Id` 贯穿（响应头 / 成功与错误 envelope / 审计 JSONL / `/solve` attempts 每跳），
  并有用例锁死「绝不做成 metrics label」（防高基数）。
- `/health` 的 `research` 段（仅带 token 时）与 `/metrics` 的三条 `cfsolve_research_*` 序列
  （无快照时 `snapshot_age_seconds` 必须是 `NaN`，不许填 0）。
- `run.py`：跨平台统一入口（12 子命令），`run.ps1` 改为转发，子命令集合由用例锁死相等。
- `scripts/check_repo_hygiene.py`（接进 verify 与 CI）+ 7 个运行产物出库。
- `detect.py` 的 `SIGNATURES_SOURCE` 元数据 + `gen_detect_signatures.py --check`（特征本身零改动）。
- dev 依赖 `hypothesis` / `ruff`（含依赖决策记录：不引 prometheus_client / mypy 的理由）。

### 本轮实测暴露并修复的 6 个真缺陷

| # | 缺陷 | 触发 | 影响 |
|---|---|---|---|
| V-1/V-2 | 反混淆产物名硬编码 `iframe.deobfuscated.js`（共 5 处），新版 cfsolve 产出 `orchestrator.*` | 首次真机入库 | **真样本被拒收 / 断言矩阵有样本也全 skip** |
| V-3 | stage2 断言在「样本未走到 stage2」时报 `fail` | 真样本 pass=4/fail=5 复核 | 会把「缺输入」误读成「CF 改版了」 |
| V-4 | `doctor` 不查 cmake/nasm/libclang/CMakeCache | `build` 两次失败 | cargo 甩 cmake panic / bindgen 栈，看不出该装什么 |
| V-5 | example 产物在 `target/release/examples/`，却只在 `release/` 找 | build 成功仍报 FAIL | **G0 永远过不了** |
| V-6 | cp936 控制台中文乱码；`subprocess` 用 GBK 解码 Rust 的 UTF-8 输出直接崩 | `doctor` / `stage2` 实测 | 工具在中文 Windows 上不可用 |

### 真机（用户追加授权 R-1 共 3 次，本版用 1 次）

- `steamdb.info`：反混淆**成功**（622 KB → 528 KB），随后 stage2 阶段**内存吃满 24.6 GB**，
  按 PID 精确终止。样本仍入库（脱敏后 1.15 MB）。
- **如实结论**：本版**没有**拿到走到 stage2 的完整会话，因此 stage2 的 6 条结构断言在真样本上
  仍是 `skip`（既非 pass 也非 fail）。这份样本证明的是：断言矩阵在真实输入上真的会判断
  （A/B pass、C fail）、缺口扫描器有真输入、归档/脱敏/校验对真数据成立。
- 教训：**真机必须走 `research.py run` 护栏**（本轮第一次没走，直接吃满 24.6 GB）。

### Rust 侧（T-71/T-72）—— 已落地并实测

- **T-71** 计算属性别名规则数据驱动化：`src/alias_rules.json` + `include_str!` 嵌入，
  `CFSOLVE_ALIAS_RULES=off` 逐字恢复 11-03 的手写判定。上游独立 commit `aeacaa5`；
  `cargo test --lib alias_rules` **8 passed**（含 `switch_restores_the_hard_coded_check` 与
  反向用例 `reverse_cases_keep_calls_dynamic`：`obj[getKey()]` 必须判动态）。
- **T-72** `envelope.rs` 把「找 counter 字段」拆成「候选集生成 → 逐个校验」，错误里带候选/行号，
  **原错误串保留为 `detail`**；`assertions_do_not_mask_each_other` 保证 6 类分析不互相掩盖。
  上游独立 commit `640516a`，关键用例全绿。
- 补丁同步落 `tools/cfsolve-patches/cfsolve-alias-rules-20261104.patch` 与
  `cfsolve-stage2-counter-diag-20261104.patch`，`PATCHES.json` 逐条登记 `assertion_ids`。
- 上游 `cargo test --lib`：**245 passed / 1 failed**；失败项
  `capture::upstream::tests::socks5_chains_the_tunnel` 经核实为 **pre-existing 环境失败**
  （`src/capture/` 在 `b5a9eb3..HEAD` 零改动、稳定失败 3/3），**与 T-71/T-72 无关**。

### 未完成（如实，不许读成已闭环）

- 本版**不承诺** stage2 对最新变体收敛（K-1 不变）；只交付「收敛过程可视化」——
  counter 失败时给候选/位置/形态，但"让它在新变体上通过"仍需上游跟进 + 真机验证。

### 独立复审（`claude -p` 独立进程，只喂 diff 不喂结论）

**第 1 轮：Request Changes** —— 5 条 blocking，全部**经复核成立**，已全部修复并补「改前必红」用例：

| # | 缺陷 | 修法 |
|---|---|---|
| **B1** | `/health.research` 与 `/metrics` 的数据源 schema 与生产者**对不上**（`corpus.py` 写 `entries`，服务读 `variants`；`corpus/assertions.json` 根本没人产出）→ 真实语料也恒报 0，**且没有 note 解释**。原测试用的是"服务端期望的形状"，属**测试为谎言背书** | 服务改读 `entries`；`assertions_matrix()` 落 `corpus/assertions.json` 摘要；用例改用**生产者的真实形状**造夹具 |
| **B2** | `/metrics` 把 `pass = total − skip`、`fail` 恒写 0 —— 真实的 1 条 fail 被记进 pass，**而测试把这个口径锁成了契约** | 三态各自独立取数；三态之和 ≠ total 时不静默凑数而是暴露不一致 |
| **B3** | `corpus.py add` 的"脱敏闸门"**名不副实**：命中明文只记进 manifest 并 `return 0`，文件原样落盘；`scan_secrets` 对 JSON 形态（`"cf_clearance": "..."`、`{"Cookie": ...}`、`Authorization: Bearer`）**全部漏检** | 入库前**真做脱敏改写**（clearance→长度、cookie 值→REDACTED、query 丢弃）；脱敏后仍命中则**默认拒绝入库**（`--allow-secrets` 才放行）；正则覆盖 JSON 形态并排除自身占位符 |
| **B4** | `diff` 的 `_assertion_states` 不用 `_has_stage2_program` 判据 → 把 V-3 刚修好的"缺输入不许报 fail"**又退回去了**，证据文件已在建议"为 dispatcher 新增补丁" | 差分复用同一判据；新增用例锁死"未走到 stage2 时必须 skip" |
| **B5** | 9 条结构断言是**字面量子串存在性**（`"counter" in js`），而 README/CHANGELOG 的措辞夸大成"指出坏在哪一行 AST" | **如实收敛表述**：断言矩阵输出增加 `strength: literal-substring` 与 `strength_note`，明确"通过 ≠ 结构未变，只说明该标记仍出现"；升级路径指向 T-72 |

复审同时确认：`/console` 的只读五条约束与 request_id 的头注入防护**基本成立**，是本轮质量最好的部分。

### 能力边界（必须与上面的成绩一起读）

- **断言强度是字面量级**，不是 AST 级。它回答"这个结构标记还在不在"，**回答不了**"结构形态有没有变"。
  真正的 AST 级判据要接 `stage2_snapshot` 的真实分析输出（T-72），本版**未完成**。
- 差分报告的"字段名集合"会把长 URL 编码串当字段名（噪声），已记为 P3。

### 验证

- Python **648 passed / 7 skipped**；覆盖率 **87.49%**；**21 模块**逐模块门禁达标。
- Go 46 测试；openapi **13 操作**双向一致；文档数字 **9 处**一致；findings **54** 条 exit 0。
- 仓库卫生 exit 0（219 跟踪文件）；`/console` 真浏览器 E2E PASS；真进程 E2E **32/32**；热更新演练 **17/17**。

## [5.0.3] - 2026-11-04

主题：**真机多站端到端确认 + 工具契约固化**（无代码行为变更）。

### 真机验证（R-E2E-3，2 次目标请求，全部成功）

- `glassdoor.co.uk`：求解 **16.5s**（12 条 cookie）→ 30s 后回放 **200 / 735,957B / 真实标题 /
  前 2048 字节 0 挑战 marker** —— **端到端成功**。
- 结合 R-E2E-2（udemy 端到端成功）：『求解 → 缓存 → 回放真实内容』在**两个不同站点**上成立。
- 累计预算 ≈58/80；累计失败仍 1/5。

### 新增

- `F-053`：多站端到端结论（含"12 条 cookie 里大量是站点业务 cookie"的集成提示）。
- `F-054`：**工具契约** —— 「是不是挑战页」必须复用生产客户端的同一份实现
  （Go `gocfsolve.LooksBlocked` / Python `verify_real.is_challenge`），禁止另写子串匹配；
  验证脚本里的 cookie/凭证只允许存在内存。

### 验证

- Python 542 / Go 46 全绿；20 模块逐模块门禁达标；12 个 openapi 操作一致；findings **54** 条 exit 0。

## [5.0.2] - 2026-11-04

主题：**更正真机结论 + 收敛挑战页判据**（无产品行为变更，只修验证工具与文档）。

### 修复

- **`F-052`（high）挑战页判据误报**：`scripts/verify_real.py` 与 `gocfsolve/examples/endtoend/main.go`
  都在**整个 body** 里找 `Just a moment` 来判断挑战页，而 udemy 的真实页面（306KB）里就含这个串
  （站点自己的 JS 文案）→ 把**回放成功**判成**挑战页**。
  - Go 侧：导出 `gocfsolve.LooksBlocked()`（复用生产客户端 `looksBlocked` 的正确判据：
    只看前 2048 字节、只在 200+html 时读正文、`cf-mitigated` 头与 403 优先），
    `examples/endtoend` 改为调用它，不再自己造子串判断。新增 `gocfsolve/blocked_test.go`（5 条用例）。
  - Python 侧：新增 `verify_real.is_challenge(status, headers, body)`（前缀 + 头/状态优先，
    与 Go 的 `challengeMarkers` 同口径）。新增 `tests/test_verify_real_predicate.py`（10 条用例）。
  - 危害说明：验证工具说谎会直接产出错误结论（本次即 `F-051`）；Go 的回放自愈若用粗判据，
    会对正常页面反复重解，白烧真机预算。

### 文档更正

- **撤回 `F-051`**（"udemy 回放未通过"）：对照实测证明回放**是成功的**（带 cookie 200/306,752B/真实标题
  vs 不带 cookie 403/5,777B/`Just a moment...`）。结论以本条为准。
- v5.0.1 的 `CHANGELOG` 与 Release notes 中相应表述已标注更正。

### 真机验证（本批用 6 次目标请求，累计失败 1/5）

- 用同一套 fresh cookie 做**带/不带**对照（各 1 次），确认判据误报；再取前 2048 字节验证
  `just a moment` / `cf-mitigated` / `challenge-platform` / `challenges.cloudflare.com` **全部 0 命中**。
- 重跑 `examples/endtoend`：修复后不再误报（同一 udemy clearance 命中缓存 0.00s）。

### 验证

- Python **542 项**全绿（+10 条判据用例）；`solver/` 覆盖率不变；20 模块逐模块门禁全达标。
- Go **46 项**全绿（41 + 5 新增），`go vet` 无输出。

## [5.0.1] - 2026-11-04

主题：**可观测性闭环 + 生态接入 + 门禁补强**（含一次独立复审与其修复；无破坏性变更）。

### 新增

- **只读状态页 `GET /`**（`solver/statuspage.py`）：单文件 HTML + 原生 JS，**零依赖零构建**。
  三块面板（健康 / 在途请求 / 最近事件）；保存·刷新·自动刷新三个按钮；token 只存 `sessionStorage`。
  页面**免鉴权但不含任何服务端数据**（数据要靠用户在页面上填 token 去读），并带严格 CSP
  （`default-src 'none'` + `connect-src 'self'` + `frame-ancestors 'none'`）。
  连不上服务时**刻意标"数据已过期 N 秒"并视觉降级** —— 页面不许用过期数据说"一切正常"。
- **`GET /status`**：在途请求明细（最久的排最前），`exclude_rid` 把自己排除，另含 `compat` 计数。
- **`GET /logs?lines=N`**：最近 200 条事件的环形缓冲（`solver/eventlog.py`），字段由**白名单**决定
  （凭证类字段进不来），单值截断，`dropped_fields` 让"丢弃"可见。
- **YesCaptcha 兼容层**：`POST /createTask` + `POST /getTaskResult`（`solver/compat.py`）。
  3 种 task type（`AntiCloudflareTask` / `TurnstileTaskProxyless` / `RecaptchaV3TaskProxyless`）；
  业务错误一律 **HTTP 200 + `errorId=1`**（生态客户端只看 errorId）；鉴权支持 Bearer **或** body `clientKey`。
  **两条设计性拒绝**：不接受任务级代理（会让求解/回放出口不一致 → 稳定静默 403）、不提供 `getBalance`。
- **`scripts/capture_sample.py`**：CF 挑战样本采集脚本化（替代 README 里"手工 curl + 手工抠 `_cf_chl_opt`"）。
  默认 `--dry-run` 零网络；预算默认 5、硬上限 20；失败累计 ≥5 即停；产出带 **sha256** 的 manifest；
  `Set-Cookie` 只记"有没有"，不记值。
- **三道新门禁**：`scripts/validate-docs.mjs`（文档数字/版本以 `docs/facts.json` 为单一事实源）、
  `scripts/check-coverage.mjs`（20 个模块**逐模块**覆盖率，未登记即红）、
  `scripts/validate-openapi.mjs`（`docs/openapi.json` 与源码路由**双向**一致 + 鉴权声明 + 版本一致）。
- `tests/test_detect_golden.py`：detect 签名表的 golden 差分（防"重新生成签名后分类结论静默漂移"）。
- `tests/test_auth_matrix.py`：起真实服务逐个端点断言鉴权（**唯一能证明"鉴权真的在生效"**的检查）。
- 启动参数 `--compat-max-pending`（兼容层待处理上限，默认 64）。

### 变更

- **`do_POST` 的请求解析与校验下沉到 `solver/request.py`**（纯函数，可脱离 HTTP 单测）：
  `do_POST` 由 175 行降到 131 行；新增 52 项边界用例，**0.08s** 跑完。
  **SSRF 守卫与 route 探测刻意不下沉**（一个是守卫、一个要出站），顺序与拆分前一致。
- **日志贯穿**：`_track_inflight` 给每个 HTTP 请求分配一个 `req_id`，`/solve` 复用它；
  三处 `traceback.print_exc()` 统一为 `Handler._log_internal()` —— 结构化行（`event=internal`
  + `req_id` + `path`）进 stdout / 审计 / `/logs`，**完整栈仍只落 stderr**，并且消息里疑似路径被脱敏。
- `AUDIT_FIELDS` 增 `path`，`/solve`、`/shutdown` 的日志调用点补齐，审计文件内键集一致。

### 修复

- `F-041`（high）：`solver/bootstrap.py` 的 `except (ValueError, SolveError)` 引用了**未导入**的
  `SolveError` → 文档承诺的「引擎缺依赖 → 友好提示 + exit 2」变成 `NameError` 裸 traceback。
  根因是 v5.0 把 `main()` 下沉到 `bootstrap.py` 时漏搬该符号；它藏在 38% 的覆盖率盲区里。
- `F-042`（medium）：超大 body 返回 413 后不读请求体，客户端仍在写 → 收 RST 读不到 413
  （实测 30 次抖动 1 次）。改为"先写响应、再用 `read1` + 墙钟 deadline 有界读残余"。
- `F-043`（high）：镜像的 UBO 插件预热写的是 `|| echo WARN` → 构建 exit 0 但镜像缺件
  （实测撞 `api.github.com` 403 限流）。改为**重试 3 次后硬失败**；并据此撤下"CI 里跑容器 e2e"的方案。
- `F-044`（critical，复审发现，本轮引入本轮修）：上述 drain 只限单次 recv 超时，且用
  `rfile.read(n)`（`BufferedReader` 会循环 raw read 凑满 n）→ 滴流客户端能把一条连接钉住约 20 分钟。
  改用 `read1()` + 墙钟 deadline + 逐轮收紧超时。
- `F-045`（critical，复审发现）：收尾顺序反了（先 drain 再写 413）→ body 超上限时残余仍在缓冲
  就被 close → RST。改为"先响应、后读残余"。
- `F-046` `F-047`（high/medium）：兼容层 `_run` 兜底范围不足（任务可永远 `processing`）、
  `create()` 线程起不来不回滚（僵尸记录占配额）、保留期从创建时刻算（刚出结果就可能被清）。
- `F-048`（medium）：非 ASCII `clientKey` 让 `secrets.compare_digest` 抛 `TypeError` → 500 + traceback 洪水。
- `F-049`（high）：`validate-openapi.mjs` 的鉴权规则只看规格自洽、不看源码 → 「规格要鉴权、实现忘了」照样绿灯。
  以 `tests/test_auth_matrix.py` 兜住。
- `F-050`（high）：`capture_sample.py` 的 import 回退是死路，且 `detect_summary` 失败会让
  `manifest.json` 丢失（真机预算已花）→ 改为仓库根入 path + `_safe_detect()`。

### 不兼容变更

- **无破坏性变更**：`/solve` 的成功响应体、`timeout` 的默认值与上下界、错误码语义**逐字未变**。
- 唯一一处**对外文案**变更：`/solve` 的 `internal` 错误 `hint` 由「查看服务端 traceback」改为
  「查看服务端日志中同 req_id 的 internal 事件」（已在 `docs/API.md` 记录）。
- 审计 JSONL 新增 `path` 字段（只增不改，按固定键集解析的消费方需知悉）。

### 验证

- Python：**532 项**全绿；`solver/` 整体覆盖率 **86.42%**，**20 个模块逐模块门禁全达标**。
- 接口规格：`validate-openapi.mjs` 12 个操作双向一致；文档数字 9 处一致；结论库 **50 条** exit 0。
- 真实调用矩阵（真进程）：**27/27**；真实 E2E（真进程 + 真浏览器）：**30/30 / 25.7s**。
- UI 截图验收（真 Chrome）：live 态 **12/12** + 服务中断态 **2/2**。
- 真机（真实站点，本批用 **4 次**目标请求，累计失败 1/5）：
  - `/detect` **3 站全部正确**：example.com→无防护(direct)、canva.com→**AWS WAF**(browser_cookie)、
    udemy.com→**Cloudflare managed**(browser)；
  - `route=auto` **2 站符合预期**：example.com 无盾 → `no_challenge=true`、**0.00s / 0 次求解**；
    g2.com(DataDome) → 直接 `unsupported-provider` 拒绝（不白等 45s）；
  - 真实求解 **1/1 成功**：udemy.com 18.32s 拿到 462 字节 `cf_clearance`，跨进程缓存命中 `cached=true`；
  - ~~**但回放未通过**~~ —— **该结论已撤回（见 5.0.2）**：当时用"整段 body 找 `Just a moment`"判挑战页，
    而 udemy 真实页面里就含这个串，属**判据误报**。对照实测：带 cookie `200`/306,752B/真实标题，
    不带 cookie `403`/5,777B/`Just a moment...` ⇒ **回放其实成功**。详见 `F-051`(rejected) 与 `F-052`。
  - glassdoor.co.uk 对照求解超时未完成，按纪律不重试。
  - **本版对真机结论的表述以 5.0.2 为准。**

## [5.0.0] - 2026-11-03

主题：**治理收尾 + 测试卫生（对照《下一步改进指南 v4.7→v5.0》W1/W3；T-53/T-54 受真机预算约束另行排期）。**

### 新增：413 契约（T-51/T-52）

- **超大 body 从 `400 invalid-request` 改为 `413 payload-too-large`**。400 的语义是"请求坏了，
  不该重试"；413 的语义是"可减小请求体后重试"——客户端的重试决策由此分化，混用是契约错误。
  `/solve` 与 `/detect` 同步对齐；`hint` 告知阈值（`--max-body-bytes`，默认 65536）与解决方向；
  审计行为不变（超大 body 请求在 JSONL 里仍有一行）。
  兼容性：`gocfsolve` 对非 2xx 一律返回 `APIError` 且不重试（静态确认 `cfsolve.go` 状态码分支），
  回放自愈（revalidate）只由**目标站 403**触发——413 不会引发任何自动重解。文档先行：
  `docs/API.md` 错误码表新增行（契约 commit 与行为 commit 分开，可单独回滚行为）。
  用例：`test_oversized_body_returns_413_and_is_audited` 等 3 个（改前必红）+ E2E `A13b`。

### 修复

- **soak.py 采样管线缺 token（T-53 前置，P1）**：`/solve` `/detect` `/metrics` 全部 401，
  长跑只会采集到 401 噪音——等于"命中率 soak 从未真正可用"。新增 `--token` / `--token-file`
  （CF_SOLVE_TOKEN= 行格式），日志只打指纹。
- **soak.py 支持断点续采（`--resume`）**：Windows 更新/休眠打断后可从已有 jsonl 继续同一轮，
  summary 的 `duration_sec` 正确累计偏移。
- **cache.py 4 处 `open()` 句柄未关闭**（salt.bin / 缓存文件读路径）：`ResourceWarning` 洪水的源头之一，
  已全部改为 `with` 块。
- **summary 新增 `hit_rate_steady`**：剔除首 1h 冷启动后，用 /health 快照的累计 `solves`/`cache_hits`
  差值计算稳态命中率（T-53 验收字段）。

### 测试卫生（T-55）

- 新增 `tests/conftest.py` autouse fixture：跟踪测试期间创建的 `SqliteStore`/`FileStore`，
  teardown 统一 `close()`。**ResourceWarning 从 100+ 条降到 0**，warning 区恢复为真信号通道
  （N-15 就是从这里抓到的）。

### 结构下沉（T-56，每步 314 全绿）

- `metrics_text` 渲染逻辑下沉到 **`solver/metrics.py`**（115 行，覆盖率 95%）；
  `Solver.metrics_text` 保留为原样再导出，`/metrics` 端点与调用方零感知。
- CLI 装配（参数解析 + 组件装配 + 信号处理，225 行）下沉到 **`solver/bootstrap.py`**；
  `service.main` 保留再导出（延迟导入避免循环），`python solver/service.py` 入口行为逐字节不变。
  **`service.py` 从 2329 行降到 2073 行**。

### 复核结论

- **T-50（prefer 真跳过）经复核 v4.x 已实现**：`backends.BackendRouter.solve(skip=)` +
  空链安全阀（skip 覆盖全链时放弃跳过）+ `route.skip_backends` 映射 + handler 翻译 + 测试断言
  （`reason=prefer-skip`）。本版未重复施工。

### 验证（全部实跑，证据见 `evidence/v50-w1.json`）

Python 314 passed · 覆盖率 81.90% · Go `-race` 绿 · E2E 30/30（含新 413 断言）·
热更新演练 11/11 · 回放压测 5589.3 QPS / p99 2.93ms / 0 错 · findings 40 条校验 exit 0

## [4.7.0] - 2026-11-02

主题：**不加功能，只把"没验证过的"变成可复现的断言，并修掉两个只有真跑才会暴露的缺陷。**

### 修复：缓存层两个并发缺陷（P0，本轮挖出来的）

- **N-15（P0）读路径不加锁 → 凭证缓存串域**。v4.2 为了修句柄泄漏把 sqlite 连接改成
  **全进程共享一条**，但只给**写**路径加了锁，读路径的注释写的是"并发安全由 sqlite3
  内部互斥保证"——**这不成立**：pysqlite 只把单次 C 调用串行化，两条线程在同一条连接上
  `execute()` 之后**游标状态是共享的**，`fetchone()` 会互相踩。
  16 线程压测里的实测后果：`InterfaceError('bad parameter or other API misuse')`、
  `float(None)` 的 `TypeError`，以及最坏的一种——**问 k9 拿到了 k10 的 `cf_clearance`**。
  凭证按域隔离是这套服务的底线，串域会让回放稳定 403，而且完全查不出来。
  修法：四条读路径（`get`/`items`/`__len__`/`host_stats`，外加 `stats()`）全部进
  `_conn_lock`（`RLock` 可重入，`get()` 内部调 `delete()` 不会死锁），锁只覆盖到 fetch
  完成；顺带给 `expires_at` 加类型防御（坏行按未命中处理，不再把 TypeError 抛给调用方）。
  **这个缺陷一直是"绿"的**：线程里的异常只变成 `PytestUnhandledThreadExceptionWarning`，
  用例照样通过——所以它只在 `run.ps1 verify` 的 warning 区里露了半行。
  用例：`test_concurrent_reads_and_writes_never_corrupt_rows`（改前必红）。
- **N-16（中）切 WAL 不带重试**。`PRAGMA journal_mode=WAL` 拿不到锁时**不走 busy handler**，
  直接返回 `database is locked`：多进程冷启动（compose 3 worker / 6 个子进程同时首启空目录）
  实测约 **1/10** 概率有一个进程起不来，`test_concurrent_first_boot_shares_one_salt` 因此
  **间歇性红**。已确认是**既有**缺陷（把本轮改动 `git stash` 掉后 10 轮仍复现 1 次）。
  修法：`busy_timeout` 提到切 WAL 之前 + 有界重试（25×0.2~0.6s）+ 仍失败则降级并**打印警告**、
  把真实模式暴露在 `stats().journal_mode`（可用性优先，但不静默）。
  诚实边界：竞态本身没能被确定性复现，用例只覆盖重试逻辑（mock 真实 SQLite 错误）。

### 修复

- **N-14（审计丢失 + 日志洪水）**：`do_POST` 的三条早退路径（空 body / 畸形 JSON / 超大 body）
  在 `url` 赋值之前就抛 `SolveError`，而 `except` 分支无条件读 `url` → `UnboundLocalError`
  **打穿了 `_log` 那一行**。后果很隐蔽：响应仍是正确的 `400`（所以只看状态码的用例全绿），
  但**审计里完全没有这条请求**，且 socketserver 会替每个畸形请求打印一整段三段式 traceback
  —— 畸形输入恰恰是最需要审计、也最容易用来刷日志的那一类。
  修法：`url = ""` 绑定在 `try` 之前。用例：`test_malformed_json_is_rejected_and_still_audited`、
  `test_empty_body_is_rejected_and_still_audited`（均「改前必红」）。
  证据：`evidence/v47-early-exit-audit.md`。
- **N-13（镜像与自己的承诺矛盾）**：`Dockerfile` 写着"浏览器在 build 阶段 fetch，运行期不联网"，
  但 camoufox 的 uBlock Origin **不在** `camoufox fetch` 的产物里 —— 它是**浏览器首次启动**时
  下载解压的（约 18MB）。实测镜像内 `addons/` 是空目录，每个新容器都刷
  `Downloading addon (UBO) 0%→100%`；离线环境下浏览器永远起不来。
  同时删掉 slim 分支里那句 `rm -rf addons/*/`（为省 2MB 把"离线可用"删掉，收益为负）。
  修法：build 阶段 headless 拉起一次浏览器预热插件（`ARG PREWARM_ADDONS=1`，默认开）。
  修复后：3 个新容器日志里插件下载 **0 次**，compose **3s** 就绪。

### 新增

- `scripts/e2e_local.py` + `run.ps1 e2e`：**真进程**端到端验收（29 项断言，离线 17s）。
  覆盖 in-process 用例天然跳过的交付链路：起进程 → 读 `.env` 的 token → 401 响应体 →
  `/metrics` 的 content-type → `/health` 免鉴权**不泄露**内部字段 → `route=auto` 不启浏览器 →
  真跑一次浏览器拿结构化失败 → **审计文件里没有 token 原文** → `/shutdown` 202 + 退出码 0。
- `scripts/hot_update_drill.py`：把 `policy.md` §6 的部署语义做成 17 项断言 ——
  **排空**（在途 6.07s 的求解在 `/shutdown` 后仍拿到 200 + 结构化响应）、
  **滚动期另一个 worker 零失败**（56 次请求全 200）、
  **blue-green**（备用端口先就绪才摘旧进程）。
- `run.ps1 verify` 现在包含 e2e（提交前必跑 = 测试 + 语法 + Go + findings + E2E）。

### 文档纠偏（都是真漂移，不是排版）

- `README.md` 里 7 条命令渲染成 `.un.ps1 xxx` —— 历史上的转义事故把 `
` 写成了真的 CR 字节，
  复制粘贴必然失败；已修回 `.
un.ps1 xxx`。
- `README.md` 的"已知限制"仍写着"4 个 worker 同时抢新域会各解一次（没有跨进程互斥）"——
  那是 v4.4 之前的事实，`claims` 抢占早已修掉（burst 12 → 3）。
- `README.md` 的"跑实验"仍在教 `cd node-shim` / `go run go-jseng|go-replay`，
  而这三个目录已在 D-3 决策里 `git rm`。
- `Dockerfile` / `docker-compose.yml` 顶部"⚠️ 未在本机验证"已过期；compose 的
  `image:` 从 `4.0.0` 对齐到 `4.7.0`。
- `test_ledger.md`：279 用例 / 81.39% → **311 / 81.95%**；§3 里"F3 CI、E4 内存、E5 崩溃隔离未测"
  三项已闭环，改标实测值。
- `workflow_status.md` §8 的五条"下一步"其实都已完成，换成真正未做的 N1–N5。
- `docs/部署-容器.md` §5 新增两条**真踩过**的运维坑：Git Bash(MSYS) 把 `/data/...` 改写成
  Windows 路径（表现是容器起来了但 token 不在 `/data/.env`）、以及运行期插件下载。

### 独立审查（3 轮，`claude -p` 全新上下文，只喂 diff、只读工具）

| 轮次 | Verdict | 说了什么 |
|---|---|---|
| 1 | **Request Changes** | 无 P0/P1，6 条：重试不分类（永久错误也重试 25 次 ≈10s）/ 降级没补 `synchronous=FULL` / "只读挂载"举例错（只读挂载下建表也写不了）/ 降级警告写脏 stdout（违反本服务的 JSON-lines 契约）/ 两条测试断言一个恒真一个过弱 / `blob, expires_at` 解包与"共享连接可能给畸形行"的论证自相矛盾。附：并发用例是概率门；自述"三条早退路径"只测了两条 |
| 2（复验） | **Request Changes** | **唯一阻塞项**：`PRAGMA journal_mode=WAL` 在某些文件系统上**不抛异常、原值返回**，而成功分支没校验返回值 ⇒ 静默降级（rollback journal + `synchronous=NORMAL` + 零告警），把第 1/2/4 条的修复在这条路径上全抵消了。另有 6 条 P3 |
| 3（复验） | **Approve** | 四种情形逐一走查（返回 `wal` / 返回 `delete` / locked×2 后成功 / 永久错误）全部落到正确分支；6 条全部成立；**无阻塞问题**。4 条 P3（告警文案、`stats()` 误把 entries 抹成 0、最后一次白睡、超大 body 的平台差异）已在本轮收掉 |

前两轮的每一条都改在代码里，并各补了一条回归用例：
`test_wal_silent_downgrade_is_still_loud_and_safe`、`test_wal_permanent_error_does_not_retry_from_the_moon`、
`test_read_paths_actually_take_the_connection_lock`（确定性机制门，不是概率门）、
`test_oversized_body_is_rejected_and_still_audited`。

### 结论库

- `docs/findings.json` 29 → **40 条**：补登记历史缺陷 F-030…F-036（N-01 SSRF 绕过 /
  N-02 TTL 自相矛盾 / N-03 盐文件并发 / N-04 过期项虚高 / N-05 跨进程重复求解 /
  N-06 覆盖 `Thread._stop` / N-07 假绿用例），加上本轮的 F-037(=N-14)、F-038(=N-13)、F-039(=N-15)、F-040(=N-16)。

### 验证（全部本机实跑）

| 项 | 结果 |
|---|---|
| `pytest tests/ -q --cov=solver --cov-fail-under=80` | **311 passed / 81.95%**（连跑 3 次稳定） |
| `go vet ./... && go test -count=1 -race ./...` | 无输出 / **41 Test 全过（8.3s）** |
| `node scripts/validate-findings.mjs` | **40 条** exit 0 |
| `run.ps1 e2e` | **29/29** |
| `hot_update_drill.py` | **17/17** |
| `loadtest.py --mode hot/cold/share` | 5959.6 QPS 0 错 / 16-16 准入 / 3 次求解 |
| 容器（compose 3 worker） | 3s 就绪、6 端点全过、滚动 150-0、`/shutdown` 202 + exit 0 |

### 未做（别读成已闭环）

生产部署（本仓库无该环境）；A3 稳态命中率与 G1 同站双臂 A/B（受真机预算约束）；
容器侧"浏览器并发求解"的稳态压测。详见 `workflow_status.md` §8 / §9.5。

## [4.6.0] - 2026-11-02

主题：**独立审查终于拿到 Verdict（Request Changes），并按循环修完 P1/P2 后复验**。
顺带用 Go 客户端把"严格站点回放 403"的根因查清。

### 独立代码审查（本会话第一次真正返回结论）

前 4 次同族子代理尝试都未返回 Verdict。本轮改用**独立进程** `claude -p`
（全新上下文、只读工具、不继承主控推理）→ 第 1 轮给出 **Request Changes**，指出：

| 级别 | 问题 | 位置 |
|---|---|---|
| **P1** | **`_account` 双计**：成功记账写在 `_submit` 返回之后且**无条件执行**，失败分支又记一次 → 一次失败被记成「1 成功 + 1 失败」，指标上表现为失败请求还有 50% 成功率 | `service.py:1321` vs `1331/1359` |
| **P1** | **claims 泄漏三条路径**：代理解析失败、准入被拒、路由准入被拒 —— 泄漏期内同域请求每次多等 10s，最长 120s | `service.py:1262→1284`、`1272-1278`、`1300`；`backends.py:350→358` |
| P2 | `/shutdown` 来源拒绝抛在 `do_POST` 的 try 外 → 调用方拿到断连而不是文档承诺的 401 | `service.py:1885-1893` |
| P2 | `cfsolve` 一次性 token 返回 `expires_in=300` 但 `expires_at=0.0`，自相矛盾（同一 bug 在第四个后端复发） | `cfsolve_backend.py:179` |
| P2 | `SqliteStore._conn()` 注释称"一把 RLock 串行化"，实际读路径不取锁 | `cache.py:311` |

**主控自查漏掉了两个 P1**（我把 Q4"是否重复计数"判成了 REFUTED）—— 这条要记下来：
自查不能替代独立审查。

### 修复（全部带回归用例）

- `_account(True, …)` 挪到"确实拿到 token 之后"；一次失败只记 1 次 solves + 1 次 failures。
  用例 `test_failed_solve_is_counted_once`。
- 四条早退路径（代理解析失败 / Solver 准入被拒 / 路由准入被拒 / 抛 SolveError）全部释放 claims。
  用例 `test_claim_released_when_admission_rejects`。
- `/shutdown` 来源不合法改为**自己回 401 + JSON 体**。用例 `test_shutdown_denied_returns_401_with_body`。
- `cfsolve` 一次性 token 的 `expires_at` 改为真实时间戳（字段自洽）。
- `cache.py` 的并发注释改写为与实现一致（写路径加锁、读路径靠 pysqlite 内部互斥）。

### 第 2 轮复验：**Approve**，顺带修掉它列的 P3

- `cache.py`：注释点名的 `delete`/`purge_expired`/`record_solve` 三条写方法**真的补上了锁**
  （原注释说"写路径加锁"但它们没加；`delete` 还会被无锁的 `get()` 调用，所以必须自己加）。
- `cfsolve_backend.py`：一次性 token 的名义寿命改为查 `challenges.NOMINAL_TTL`，
  不再做第三份硬拷贝（下次调寿命不会再漏一处）。
- 补两条覆盖缺口用例：代理解析失败 / Solver 自身准入被拒 两条早退路径的 claim 释放
  （第 2 轮指出"新用例只覆盖了路由侧同构路径"）。

### 新增工具：`gocfsolve/examples/replaycheck`

用来判"严格站点回放 403"到底是服务问题、脚本问题还是 IP 问题：
同一个服务、同一份凭证、同一个出口，**只换回放客户端**，并带"不带凭证"对照组。

实测 epicgames.com（同 IP）：

```
REPLAY_GO url=https://www.epicgames.com/ status=301 bytes=270 challenge_page=false
REPLAY_NOCOOKIE                          status=403 bytes=4000
```

⇒ **凭证有效、CF 已过**（301 是站点把首页跳到 store 登录页的业务跳转）；
之前 Python 脚本的 403 是**脚本问题**（只发 cf_clearance、请求的是无 www 的 host、缺 Firefox 导航头），
**不是 TLS 指纹问题，也不是 IP 被提分**（同一 IP 同一时刻带凭证就能过）。

同时暴露两个**调用方易踩点**（已写进 `docs/API.md`「回放要点」与项目规格 L10）：
回放必须用**求解时的最终 URL**（cookie 域可能比请求 host 更具体）；必须带**全部 cookie_list**。

### 验证

| 项 | 结果 |
|---|---|
| Python | **302 passed**，覆盖率 81.98% |
| Go | 41 + egress 4，`vet` + `-race` 绿；新增 `replaycheck` 示例可构建 |
| 独立审查 | 第 1 轮 Request Changes → 修复 → **第 2 轮 Approve**（证据：`evidence/cli/v46-review-round{1,2}.txt`）|
| 真机预算 | 本轮额外用了 **3 次**（超出 §15 的 60 次，用户明确要求闭合该项），已记账 |

## [4.5.0] - 2026-11-02

主题：把上轮列出的 4 条尾巴收掉——compose 多 worker、镜像瘦身、真机预算用尽、`service.py` 第一步拆分。

### 新增

- **`--build-arg SLIM_FONTS`**：镜像默认 4.63 GB；`SLIM_FONTS=1` 只删 Camoufox 的 CJK 字体包
  → **3.27 GB（−1.36 GB）**，且**已验活**（10 s 就绪、`browser_alive=true`、`/detect` 200）。
  默认不删：那 2.1 GB 字体包是**字体枚举指纹真实性**的依赖，删了可能降低成功率。
- **`solver/probe.py`（135 行）**：`probe_protection` + 逐跳重定向守卫的 3 个常量。
- **`solver/auth.py`（77 行）**：`load_or_create_token` / `_read_token_file` / `token_fingerprint`。
  `service.py` 侧原样再导出，既有引用与 `monkeypatch` 全部照旧。

### 实测

| 项 | 结果 |
|---|---|
| `docker compose up -d`（3 worker） | ✅ **20 s** 全部 healthy；三者 `cache_backend`/`ua_fingerprint` 一致；内存合计 **2.26 GB** |
| 跨容器 `claims` 抢占 | ✅ 1 真 / 2 假，释放后可再抢（共享卷 `/data`） |
| 镜像瘦身 | ✅ 4.63 GB → **3.27 GB**，容器 10 s 就绪、`/detect` 真出站 200 |
| `service.py` | 2452 → **2310** 行（−142，−5.8%） |
| Python / Go | **297 passed / 覆盖率 81.90%**；Go 41 + egress 4，`vet` + `-race` 绿 |

### 真机预算用尽（60/60）

| 站点 | 决策 | 结果 |
|---|---|---|
| patreon.com | skip（探测未命中防护） | ✅ **1.2 s** 返回 `no_challenge=true`（旧行为会跑 20 s 浏览器） |
| epicgames.com | solve（Cloudflare managed） | ✅ **8.7 s / 462 字节** |

回放侧：用**完整 cookie_list**（`cf_clearance` + `__cf_bm`）重放 epicgames 仍 403。
**不判成服务缺陷**：脚本是 Python `urllib`（OpenSSL TLS、缺 `Sec-Fetch-*` 等 Firefox 导航头），
Go 客户端会带完整头部并用自己的连接池。这是「回放依赖 UA+完整 cookie+头部/TLS 一致」的已知限制显形。
预算已用尽，按 §15 纪律不再打真机；要验证需换出口 + 用 `gocfsolve.Do()` 而非脚本重放。

### 两个只有动手才会遇到的坑（已写进 Dockerfile 注释）

1. **删除必须与 `fetch` 同一个 `RUN`**，分开写只加 whiteout，镜像一点不小（实测分两层时仍是 4.97 GB）；
2. **BuildKit 缓存会给出一个没有浏览器的镜像**（0.99 GB、一跑就 `CamoufoxNotInstalled`）
   ⇒ 瘦身必须验活，不能只看数字。

## [4.4.1] - 2026-11-02

补丁版：自查发现"抢占在失败路径上没释放"。

### 修复

- **求解失败时没有释放 `claims` 抢占** → 同域的其它 worker 会白等一个永远不会出现的缓存
  （最多等满 `_wait_for_peer` 的 10s，或 claim 的 TTL）。现在三条失败路径
  （未取到凭证 / TTL 过短 / 抛 SolveError）与路由后端失败路径都会立刻释放。
  回归用例：`test_claim_is_released_after_failure`。

### 验证

| 项 | 结果 |
|---|---|
| Python | 297 passed，覆盖率 81.82% |
| Go | 41 + egress 4，vet + -race 绿 |

## [4.4.0] - 2026-11-02

主题：把上一轮列出的"剩余风险"逐条打掉——跨进程重复求解、多出口回放、真实 CF 站路由证据、容器化。

### 修复

- **多个 worker 同时抢同一个新域会各解一次**（v4.2 之前量化过：4 进程 burst 12/12）。
  根因：共享缓存只解决"先后到达"，不解决"同时到达"。
  修法：`SqliteStore` 增加 `claims` 表 + `try_claim(key, ttl)`（`INSERT OR IGNORE` 原子抢占）/
  `release_claim`；`Solver` 在解之前抢占，抢不到的进程**等对端填缓存**（最多 ~10s 或预算一半），
  而不是各自启动浏览器。Solver 路径与路由后端路径**共用同一套**（`claim_solve`/`wait_for_peer`/
  `release_claim_for`），避免两份实现漂移。
  **实测：burst 3 轮从 12 次求解降到 3 次（1 次/轮，0 浪费）。**

### 新增（文档）

- **多出口部署的端到端配方**（`docs/部署-容器.md` §4.5）：服务端 `--proxy` → 响应里的
  `egress_id`（形如 `px:529c4adb33a1`，不含凭据）→ 客户端 `WithEgressProxy(egress_id, proxyURL)`。
  纪律写明：**一旦登记出口代理，回放就 fail closed**（拿到未登记的 egress_id 直接
  `ErrNoEgressProxy`，绝不悄悄直连——直连的结果是静默 403，最难归因）。

### 真实调用（3 站，消耗预算 3 次）

| 站点 | 探测结论 | 路由决策 | 结果 |
|---|---|---|---|
| udemy.com | Cloudflare / managed | solve, prefer=pure | ✅ **6.9s / 459 字节** |
| **g2.com** | **DataDome + Cloudflare** | **reject（unsupported-provider）** | ✅ **1.3s 就拒绝**（旧行为会白等一整个 45s 超时） |
| canva.com | Cloudflare / managed | solve, prefer=pure | ✅ **4.7s / 330 字节** |

累计真机预算 ≈56/60。**g2 这一条是本轮最有价值的证据**：真实站点上的诚实拒绝，
把 45s 的无效等待压到 1.3s。

### 变更

- `scripts/loadtest.py` 的 `share` 子进程改为模拟真实行为（先 `try_claim` 抢，抢不到等缓存），
  因此 burst 阶段现在也应当只解 1 次/轮 —— 一旦抢占回归，burst 会立刻从 1 次变回 4 次。

### 容器化：从「未验证」到「真跑通」，并抓出 2 个容器专属缺陷

| 项 | 结果 |
|---|---|
| `docker build` | ✅ 1.61 GB |
| 容器就绪 | ✅ **5 s**（修复前 **110–120 s**） |
| 端点 | ✅ `/health`、`/detect`（真出站）、SSRF 400、无 token 401、`route=auto` skip |
| 优雅退出 | ✅ `POST /shutdown` → `202`（peer=172.17.0.1）→ **`exit=0`** + `在途请求已全部完成` |

- **N-11**：build 期 `camoufox fetch` 落在 root 的 HOME，运行期用户是 `solver` ⇒ **每个新容器首次启动
  重下载浏览器（白等 110s）**。修法：`ENV HOME=/home/solver` → fetch → `chown` → `USER solver`
  （直接 `USER solver` 再 fetch 会因 fpgen 模型属主报错，构建失败）。
- **N-12**：`/shutdown` 的「仅回环」判断在端口映射下不成立（客户端是 **docker 网关 172.17.0.1**）
  ⇒ 滚动重启在容器里必失败。修法：抽出 `shutdown_allowed(peer)` + 新增 `--allow-remote-shutdown`
  （compose 已带；token 仍是第一道门），并有回归用例 `test_shutdown_allowed_matrix`。

### 验证

| 项 | 结果 |
|---|---|
| Python | 295 passed，覆盖率 81.75% |
| Go | 41 + egress 4 个用例，`vet` + `-race` 绿 |
| 跨进程去重 | burst 12 → **3**（1 次/轮） |
| 路由证据 | 3 站 solve + 1 站 reject（1.3s） |
| 容器 | 5s 就绪 / 六项端点全过 / 优雅退出 exit=0 |

## [4.3.0] - 2026-11-02

主题：**把三件"设计上应该有、但从没验证过"的事变成实测数字**——4 worker 内存、
崩溃隔离时延，以及补上审计日志。

### 新增

- **审计日志 `--audit-log <path>`**（JSONL，字段**白名单**）。
  每次 `solve`/`detect`/`shutdown` 落一行，含 `host/backend/route/ok/elapsed/error_code/stage/peer` 等；
  **刻意不含** clearance / token / cookies —— 审计回答"谁在什么时候打了哪个站、结果如何"，
  不回答"凭证是什么"。写盘失败只降级（打 `audit-write-failed`），**不会让请求失败**。
- **`gocfsolve/examples/pooldrill`**：池演练程序，每秒打印节点健康快照，
  配合外部脚本杀/启 worker，用来量 E5 的摘除与回归时延。

### 变更

- **`prefer` 现在真的影响选路**（此前只是响应里的一个字符串，名不副实）：
  `prefer=pure` 跳过 pydoll/flaresolverr；`prefer=browser` 跳过 cfsolve；
  被跳过的后端在 `attempts[]` 里标 `reason=prefer-skip`。
  安全阀：若 skip 会把整条链跳完，则**忽略 skip** 正常求解（不制造"没有可用后端"）。

### 实测（E4/E5，此前只有"设计上应该"）

| 项 | 目标 | 实测 |
|---|---|---|
| E4 4 worker 内存 | ≤ 3.5 GB | **3.34 GB**（855 MB/worker，含浏览器进程树；总句柄 10827） |
| E5 崩溃摘除 | ≤ 20 s | **2 s**（maxFailures=2 + 探活 2s） |
| E5 自动回归 | ≤ 60 s | **28 s**（受 30s `downDuration` 支配；其余 3 节点全程 `f=0`） |

### 验证

| 项 | 结果 |
|---|---|
| Python | **293 passed**，覆盖率 **81.69%** |
| Go | 41 Test，`vet` + `-race` 绿；`pooldrill` 可构建 |
| 审计 | 3 个用例：含"白名单外的凭证字段不落盘"与"写盘失败不打断请求" |

### 未完成（诚实披露）

- **Docker 镜像仍未构建验证**：本机 Docker daemon 未启动（`dockerDesktopLinuxEngine` 连不上）。
- 代理/出口绑定只覆盖 Go 客户端侧，**回放侧**仍未接线。
- 真机预算本轮 0 新增（E4/E5 全部本地），累计 ≈53/60。

## [4.2.1] - 2026-11-02

补丁版：修掉 1 小时 soak 抓出的**句柄泄漏**（v4.2.0 的 soak 判定因此是 FAIL，这是它存在的意义）。

### 修复

- **`/health` 每条新 TCP 连接泄漏约 3 个内核句柄，且永不释放**（P1，实测）。
  - 现象：1 小时 soak 句柄 2787 → 4095（**+1308**），而**线程数全程不变**。
  - 归因链：`+100 × GET /health` → **+300**（我们进程，浏览器 0）；`100 连接 × 1 请求` → +296，
    而 `1 连接 × 100 请求` → +7 ⇒ **按连接计**；最小 `ThreadingHTTPServer` 同样 100 连接只 +16
    ⇒ 泄漏在我们的代码里。
  - 根因：`SqliteStore` 用 **thread-local** 持连接，而 HTTP handler 是"每连接一个线程"，
    于是每条新连接都新建一条 SQLite 连接（WAL 下 3 个文件句柄：`db`/`-wal`/`-shm`），
    线程退出后连接未及时关闭。触发点是 `/health` 每次都读 `cached_domains` →
    `len(store)` → `_conn()`。
  - 修法：全进程**共享一条连接**（`check_same_thread=False` + `RLock`），加 `_conn_creations`
    计数便于回归断言；`tests/test_cache_sqlite.py::test_sqlite_store_uses_single_shared_connection`
    断言"16 线程 × 5 次读写后只建 1 条连接"。
  - 复测：同手法 `+100 /health` → **+0**，`+100 /metrics` → **+0**；
    **15 分钟 soak 全绿**（句柄 2788 → 2746，净 **-42**；RSS 回落 396MB；0 错；全程 ready）。

### 修复（工具自身）

- `scripts/soak.py` 的 RSS 判据原来是 `abs(growth)`，把"内存回落"也判成失败 ——
  已改为**只看增长**（负数不是缺陷）。这一条属于"尺子错了"，不是系统错了。

### 修复（测试抖动，诚实记录）

- `test_concurrent_first_boot_shares_one_salt` 在重负载（同时跑 soak）时偶发失败：
  6 个子进程抢建 `salt.bin` 的等待窗口从 3s 放宽到 **10s（上限，正常路径一秒内完成）**。
  **断言未改**（仍是"全部子进程成功且拿到同一份盐"）—— 这是把超时调成现实值，不是放宽断言。

### 验证

| 项 | 结果 |
|---|---|
| Python | **287 passed**（连跑两遍稳定），覆盖率 81.6% |
| Go | 41 Test，`vet` + `-race` 绿 |
| 句柄泄漏 | 修复前 +1308/小时 → 修复后 **-42/15min**（净减少） |
| 15 分钟 soak | **passed: true**（0 错、全程 ready、RSS 回落、句柄不涨） |

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
