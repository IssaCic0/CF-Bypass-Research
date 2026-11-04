## Summary

我按你给的 9 条逐条对照了 diff（仅基于本次提供的补丁内容与测试文件，未运行任何命令、未打开仓库其它文件）。核心两项（N1 脱敏、N2 断言语义）这次是**真修**，实现与测试都改到了关键性质上，不是表面改动：

- N1 从"就地正则改写整篇"改成"JSON 结构化脱敏 + JS 零改写 + 命中即拒绝"，且新用例断言了落盘内容 `json.loads` 成功。
- N2 把 `assertions_total` 统一为"跨变体判定次数 = pass+fail+skip"，消费者（`/metrics`）三个 state 独立取数，不再有 `pass = total − skip` 那种推算，也就不会再在 ≥2 变体时把 fail 清零；`test_metrics_uses_real_matrix_counts_with_multiple_variants` 确实用 `assertions_matrix()` 的真实产出喂了指标。

但仍有 1 条 P1 待验证风险（脱敏闸门可能拒收**所有真实样本**）、2 条契约与实现不一致（本次"契约同步"自己没对齐）、2 条测试缺口（恰恰是第 2 轮点名的混合场景/空 entries 场景），以及 B1 的 `last_diff_at` 有生产者但取错文件。所以本轮仍是 **Request Changes**。

---

## 逐条核验

### 1. N1 脱敏不再破坏文件 —— **FIXED**

依据：
- `scripts/corpus.py::redact_json_text` 走 `json.loads` → 递归只改**值**（键名不动）→ `json.dumps(..., indent=2)`；解析失败返回 `None`，调用方退化为原样保留。
- `cmd_add` 只对 `*.json` 调 `redact_json_text`；JS/文本**一字不改**（原 `raw` 直接落盘）。
- 入库闸门 `scan_secrets(dest)` 命中即 `shutil.rmtree(dest)` + 返回错误，不存在"提示一下继续"。
- `test_corpus_add_redacts_json_without_breaking_structure` 断言了：明文不在落盘内容里、`json.loads(stored)` 成功、`parsed["host"]` 仍为 `example.com`、`cmd_redact_check(dest) == 0`。这正是第 2 轮要求的"断言落盘内容可解析"。
- `test_corpus_add_refuses_plaintext_in_js` 覆盖了"JS 有明文 → 拒收且不留 manifest"。

### 2. N2 assertions_total 语义 —— **FIXED**

依据：
- 生产者 `research.py::assertions_matrix` 的摘要写 `assertions_defined = len(all_names)`（9）与 `assertions_total = pass + fail + skip`（跨变体求和），两个口径**分开命名**，注释也写明了混用会导致"≥2 变体时 sum != total → 被清零"。
- 消费者 `solver/metrics.py` 里 `pass/fail/skip` 各自 `int(research.get(...) or 0)`，**没有任何**由 total 反推的算式；不一致时不会输出 0，也不需要输出 NaN。
- `test_metrics_uses_real_matrix_counts_with_multiple_variants`：造 2 个真实变体 → `cmd_add(real=True)` → `assertions_matrix()` → 读真实 `corpus/assertions.json` → 断言 `assertions_total == pass+fail+skip`、`report["totals"]["fail"] >= 1`、指标文本含 `state="fail"` 的真实值且不含 `state="fail"} 0`。符合"不得再用手写 fixture"的要求。

小瑕疵（不构成未修复）：`assertions_total == sum(pass,fail,skip)` 这条断言在生产者里是定义式，属自证；真正的回归保障来自指标文本那两条断言 —— 可以接受，但建议再加一条"total != 9"的显式断言把 2 变体场景钉死。

### 3. B4 剩余（state_b == "skip" 不产"需新增补丁"） —— **PARTIAL**

依据：
- 代码已正确：`build_diff_report` 里
  - `if state_a == state_b and state_a in ("pass","skip"): continue`
  - `if state_b == "skip" or state_a == "skip": suggestions.append("…不可判定…不产生补丁建议…"); continue`
  在到达 `_patches_for_assertion`/"需新增补丁"分支之前就 return，`state_b == "skip"` 不可能产出补丁建议。✓
- **缺第 2 轮明确点名的用例**：`tests/test_diff_report.py` 6 个用例里，只有"A、B 都没有 stage2"（`stage2=False` 双份）的用例，**没有**"A 有 stage2 / B 无 stage2 → 建议动作不含'新增补丁'"的混合场景用例。`make_snapshot(..., stage2=...)` 已经支持，加一行即可，但没加。
- 附带发现：`fail → fail`（两侧都缺 stage2 标记但都有 stage2 程序）时，`transition` 显示"无变化"，却仍走到"PATCHES.json 无对应补丁登记，需新增补丁"分支 —— 与"不可判定不产补丁"的收敛方向不一致。

### 4. B1 剩余（entries 空时 note 非空；last_diff_at 生产者） —— **PARTIAL**

依据：
- note：`Solver.research_status` 里 `if not rows: notes.append("corpus/INDEX.json 存在但 entries 为空：尚未归档任何会话快照")` ✓；文件缺失、损坏两条路径也有 note ✓。
- `last_diff_at`：**有生产者** —— `assertions_matrix` 里 glob `EVIDENCE_DIR.glob("research-diff-*.md")` 并写入 `corpus/assertions.json`，`cmd_diff` 的落盘路径与之匹配 ✓；服务侧 `research_status` 也读取并透出 ✓。
- 但存在**语义 bug**：`diff_files = sorted(...)` 后取 `diff_files[-1].stat().st_mtime`，取的是**文件名字典序最大**的那份的 mtime，不是"最近生成"的那份。字段注释写的是"最近一份报告的生成时间"，实际会给出陈旧时间。应改为 `max(..., key=lambda p: p.stat().st_mtime)`。
- 测试缺口：`test_health_research_defensive_when_corpus_missing` 覆盖的是"目录不存在"，**没有**覆盖"`INDEX.json` 存在但 `entries: []`"这条（也就是第 2 轮点名的那条）。

### 5. run_wrapped（communicate / stderr / encoding / stage） —— **PARTIAL**

依据：
- `communicate(timeout=max_sec)` 读管道 ✓；超时后 `kill()` + 再 `communicate(timeout=10)` ✓；全程**没有** `proc.stderr.read()` 二次读 ✓；`text=True, encoding="utf-8", errors="replace"` ✓。历史"写满 64KB 管道被误判超时"的缺陷已消除。
- stage：`"stage": infer_stage(out_dir) if out_dir else "unknown"` —— 方向对了（不再拿仓库 corpus 猜），但 `main` 里 `run_wrapped(args.cmd, args.max_mb, args.max_sec)` **从不传 `out_dir`**，也没有对应的 CLI 参数（如 `--out-dir`）。因此文档化的 `python scripts/research.py run …` 路径上 stage 恒为 `"unknown"`，"阶段与命令相关"实际没有落地，只是个永远走空的分支。
- 次要：`_peak_rss_mb(job, max_mb)` 的 `max_mb` 参数未使用；峰值内存是在 `TerminateJobObject(job, 0)` **之后**查询（顺序可疑，建议先查再 terminate）；`Popen` 之后再创建 Job 并 Assign，存在"子进程先分配后被纳入限额"的窗口。

### 6. run.py stop 端口精确匹配 —— **FIXED**

依据：`pattern = re.compile(rf":{re.escape(str(port))}\s")`，`:8878` 不会命中 `:88780`（后面是数字不是空白）✓；只对 `LISTENING` 行匹配，避免了把 ESTABLISHED 远端端口算进来 ✓。`test_runpy_stop_matches_port_exactly` 用 `0.0.0.0:88780` + `127.0.0.1:8878` 的 netstat 输出断言只杀 PID 2222 ✓。

### 7. N5 /health 与 /metrics 不持锁做文件 I/O —— **FIXED**

依据：
- `Solver.health`：`with self._lock:` 块在 `body["prewarm"]` 后结束，`body["research"] = self.research_status()` 在**锁外**执行，并有注释说明"持锁读盘会连带阻塞 `_account()` 记账" ✓。
- `Solver.metrics_text`：锁内只构造 `kwargs` 快照，`kwargs["research_fn"] = self.research_status` 与 `render_metrics_text(**kwargs)` 都在**锁外** ✓（顺带 `host_stats_fn` 的 DB 读也移出了锁，是正确方向）✓。

### 8. 契约 docs/openapi.json 补 X-Request-Id 与 request_id —— **PARTIAL**

依据：
- 已做：`info.version` → `6.0.0`；`Error` 与 `SolveResponse` 增加 `request_id`；各 path 的 200/4xx/5xx 增加 `X-Request-Id` header 描述；新增 `/console` path（与 `do_GET` 的 `if path == "/console"` 对应，`validate-openapi.mjs` 的反向核对能过）✓。
- **问题 A（本次新造）**：`"401": {"$ref": "#/components/responses/Unauthorized", "headers": {...}}` 这种 `$ref` 旁边挂 `headers` 的写法，在 **OpenAPI 3.0.3** 里是无效的 —— `$ref` 会整体替换该对象，兄弟键被忽略。也就是说全篇 401/429 的 `X-Request-Id` 声明是**死文档**，只有 200/400/413 那种内联 response 的声明才生效。要么把 header 提到 `components.responses.Unauthorized/Busy` 里，要么把这些响应展开内联。
- **问题 B（契约与实现不一致）**：`/metrics` 200 与 `/console` 200 都声明了 `X-Request-Id`，但实现里 `do_GET` 的 `/metrics` 分支和 `_send_console_page` 都是 `send_response` 直写、**没有** `send_header("X-Request-Id", …)`（只有走 `_send()` 的 JSON 出口才带）。这正是"契约同步"这一条自己没对齐。
- **问题 C（范围外但相关）**：`_send()` 现在给**所有** JSON 响应注入 `request_id`（`/detect`、`/status`、`/logs`、`/stats`、`/createTask`、`/getTaskResult`），这些响应 schema 未声明该字段 —— 属超发，严格客户端/校验器可能报 unexpected property。
- **未核验**：`docs/API.md`（`git status` 显示 M，但本次 diff 未提供内容）与 `docs/facts.json` 的同步情况 —— 待验证。

### 9. B5 strength caveat 传播到差分报告 —— **FIXED（但未锁死，且只到了报告）**

依据：
- `build_diff_report` 第 4 段硬编码了 `> 判据强度：**literal-substring**（字面量子串存在性），不是 AST 结构校验。` 以及 `pass` 只表示标记仍出现、`skip` 表示不可判定 ✓。
- 缺：无任何测试断言该字符串存在（`tests/test_diff_report.py` 未覆盖）；`corpus/assertions.json` 摘要**没有**写 `strength`，因此 `/health.research` 与 `/metrics` 的消费方看不到判据强度，只有差分报告带；文档侧（`docs/API.md`）无法从本 diff 核验。

---

## 新引入的问题

按严重度：

1. **P1（待验证，但直接威胁 T-66 的用途）—— 入库闸门可能拒收所有真实样本。** `scan_secrets` 的 `token_assignment` 正则 `(?i)['\"]?(api[_-]?key|token|clientkey|secret)['\"]?\s*[:=]\s*['\"][^'\"]{8,}` 会命中反混淆后的 CF 脚本里几乎必然存在的 `token = "…"` / `"token":"…"` 形态。与"JS 不改写 + 命中即拒绝"叠加后，真样本大概率进不了库（而合成样本测试的 `var token = 5;` 恰好不带引号，所以测试全绿，掩盖了这一点）。必须用一份真实 `*.deobfuscated.js` 跑一次 `cmd_add` 才能定性；若命中，需要把闸门按"字段/赋值位点"收窄（例如只判 `cf_clearance`/`Cookie`/`Authorization`/`clientKey`，或对 JS 用行级白名单豁免研究对象本身的 token 字面量）。
2. **P2 —— OpenAPI `$ref` 旁的 `headers` 在 3.0.3 下被忽略**（见第 8 条问题 A）：全篇 401/429 的 `X-Request-Id` 是死声明。
3. **P2 —— `/metrics`、`/console` 的 200 响应声明了 `X-Request-Id` 但实现不发**（见第 8 条问题 B）。契约与代码在这次交付内部自相矛盾。
4. **P2 —— `last_diff_at` 取"文件名字典序最后一份"的 mtime，而非最新 mtime**，字段语义（"最近一份差分报告"）与实现不符（见第 4 条）。
5. **P2 —— `run_wrapped` 的 `out_dir` 在 CLI 路径永不传入**，`stage` 恒为 `"unknown"`，"阶段与命令相关"未落地（见第 5 条）。
6. **P2（测试缺口）** —— B4 混合场景（A 有 stage2 / B 无 stage2）用例缺失；B1 "`entries: []` 时 note 非空"用例缺失。两处都是第 2 轮明确点名的验收用例。
7. **P3 —— `redact_text` 的复合顺序让 clearance 长度信息丢失**：`redact_text = url_query(cookie(clearance(...)))`，`redact_clearance` 产出的 `cf_clearance=[len:9]` 紧接着被 `redact_cookie` 的 `([A-Za-z0-9_-]+)=…` 覆盖成 `cf_clearance=[REDACTED]`。于是"cf_clearance 值 → 只保留长度"的硬规则在 JSON 路径上不成立，而 `test_corpus_redact_rules` 只测了 `redact_clearance` 单函数、没测 `redact_text` 的组合，所以测试绿。`scan_secrets` 里 `re.sub(r"\[len:\d+\]","",…)` 那段兜底也反证了作者原本预期 `[len:N]` 会留在文件里。
8. **P3 —— `/health` 与 `/metrics` 每次请求都读两个磁盘文件**（`research_status` 无缓存）。锁是放开了，但把"每请求 2 次文件 I/O"变成了常态；Prometheus 15s 抓一次无妨，被高频轮询时是新的 I/O 放大点。建议加短 TTL（如 5s）内存缓存。
9. **P3 —— `run.py` 文档与实现不一致**：模块 docstring / `cmd_help` 打印的子命令是 `loadtest`、`serve`，而 `COMMANDS` 里是 `bench`、`start`，照着帮助敲会得到 `invalid choice`。
10. **P3 —— `run.py::cmd_go` 第二行的行尾注释**（`# 注：run() cwd 由 kw 覆盖为 gocfsolve`）位于 `return` 语句尾部、描述的行为并不存在，属误导性注释。
11. **P3（待验证）—— `request_id` 可能进不了 `/logs`**：`AUDIT_FIELDS` 已加 `request_id`，但 `EventLog` 的字段白名单（`solver/eventlog.py`）不在本次 diff 内；若白名单未同步，`_log` 注入的 `request_id` 会被当 `dropped_fields` 丢掉，`/logs` 里仍然对不上号。
12. **P3 —— `fail → fail` 仍建议"需新增补丁"**（见第 3 条附带发现），与不可判定/无变化的收敛原则不一致。

---

## Verdict

**Request Changes**

---

## 放行条件（按优先级，第 4 轮）

1. **必查**：用一份真实 `*.deobfuscated.js`（或一份真实 `capture.json` + 真实脚本）跑 `cmd_add`，证明"命中即拒绝"不会把真样本全拒。若会拒，收窄 `scan_secrets` 的 `token_assignment` 判据（只认 `clientKey`/`APIKey`/`Authorization`/`cf_clearance`/`Cookie` 等真凭据位点，并对研究对象自身的 `token="…"` 字面量放行），并把结论写成用例：**真实形态的 deobf 脚本 + 一个真凭据 → 命中；真实形态但无凭据 → 入库成功**。
2. 修 OpenAPI：`$ref` 旁挂的 `headers` 全部上提到 `components.responses.Unauthorized/Busy`（或展开内联）；并让 `/metrics` 与 `/console` 的 200 真的发出 `X-Request-Id`（或从契约里删掉）。附 `node scripts/validate-openapi.mjs` 的实际输出。
3. 补第 2 轮点名的两个用例：**（a）A 有 stage2 / B 无 stage2 → 建议动作不含"新增补丁"**；**（b）`INDEX.json` 存在但 `entries: []` → `research.note` 非空**。
4. 修 `last_diff_at`：改成按 `st_mtime` 取最大；并加一条用例（两份 diff 报告，名字序与 mtime 序相反）。
5. 修 `run_wrapped` 的 stage：给 `research.py run` 加 `--out-dir`（或从命令的 `--snapshot-dir` 之类的既有参数推导），把 `out_dir` 真传进去；至少一条用例断言 `stage` 不再恒为 `"unknown"`。
6. 修 `redact_text` 的组合顺序（先 cookie 再 clearance，或让 `redact_cookie` 跳过已经带 `[len:N]`/`[REDACTED]` 的值），并加一条 `redact_text("cf_clearance=abc-123_X")` 的用例锁死"长度可见"。
7. 顺手（P3，可与上面合并）：`docs/API.md` 补 `/console`、`request_id`/`X-Request-Id`、`research` 段与 `strength: literal-substring`，并贴出改后与代码的一致性核对；`run.py` docstring 与 `COMMANDS` 对齐；`_peak_rss_mb` 在 terminate 之前取峰值。

---

## 待验证项（我未核、也建议你自己走一遍）

- `docs/API.md`、`docs/facts.json`、`CHANGELOG.md` 的实际同步内容（本 diff 未包含）。
- `run.ps1` 的 `ValidateSet` 与 `run.py COMMANDS` 是否真相等（`test_runpy_targets_match_runps1` 会兜住，但我没有运行）。
- `solver/eventlog.py::ALLOWED_FIELDS` 是否含 `request_id`。
- `tools/cfsolve-patches/PATCHES.json` 中 `counter → newcf-computed-key-20261103` 的映射是否真如报告所依据。
- 我本轮仅阅读你贴出的 diff 与两个测试文件，**未执行任何命令、未运行测试**；所有"FIXED"均为静态确认（读代码），不是运行验证。
