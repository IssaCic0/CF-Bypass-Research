## Summary

先说结论：**不建议合并**。5 项 blocking 中 **B3 基本到位（但把高危缺陷换成了另一种高危）**，**B1/B2/B4/B5 都是 PARTIAL**——三条是"改到看起来对了但语义仍错"，两条是"只改了代码没改判据/契约"。另外，上一轮 blocking 6（`run_wrapped`）与 7（`run.py stop`）在本次 diff 里**完全没动**，而它们同样是阻塞级。

最要命的两条新缺陷：

1. **B3 的"真脱敏"会把入库文件写坏**：`redact_cookie` 用 `([A-Za-z0-9_-]+)=[^;]*` 对**整篇文件**做替换，`[^;]*` 会跨行贪吃。`capture.json` 里只要出现 `{"Cookie": "cf_clearance=abc123"}` 这种**结尾没有分号**的键值对，替换会吞到文件末尾 → 落盘的是**残缺 JSON**，而 `redact --check` 与 `verify` 全部绿灯（前者只因"没有明文了"，后者只比对自己写下的哈希）。
2. **B2 修好了"读真实计数"，但引入了一个必然触发的清零**：生产者写 `assertions_total = len(all_names) = 9`（断言定义数），而 `pass/fail/skip` 是**跨变体求和**。消费者用 `sum(counts) != total → 全清零`。只要语料里有 **≥2 个变体**（T-75 差分本身要求的场景），`/metrics` 三个 state 全变 0——`fail` 又回到"恒 0 的假绿"，只是这次是结构性的、且有测试背书。

---

## 5 条 blocking 的核验结论

> 说明：本次为纯只读审查，未运行工具，无法给出精确行号，证据锚点用 `文件::函数/代码片段`。

### B1 — `/health.research` 与 `/metrics` 数据源 schema 对齐 → **PARTIAL**

已修的部分：
- `solver/service.py::Solver.research_status` 同时识别 `entries` 与 `variants`（`rows = index.get("entries")` → 回退 `"variants"`），与 `scripts/corpus.py::_write_index` 的 `{"entries": [...]}` 对齐。✅
- `scripts/research.py::assertions_matrix` 现在**真的产出** `corpus/assertions.json`（`assertions_total/pass/fail/skip/last_snapshot_at/skip_reason`），消费者读的键名一致。✅（此前"没有任何代码产出 corpus/assertions.json"的第一半确实修了）
- 测试 `tests/test_research_exposure.py::test_health_research_reads_corpus_when_present` 改用 `{"entries": [...]}` 形状 + 生产者 summary 键名。✅

没修/仍有问题的部分：
- **`INDEX.json` 存在但 `entries` 为空时 note 仍为空**。`research_status` 只在 `FileNotFoundError` 与解析异常时 append note；`rows == []` 走 `variants = 0` 且**不追加任何说明**。上一轮明确要求"INDEX.json 存在但无 variants 时 note 必须非空（当前静默 0）"，这一条没做。可复现：`{"updated_utc": "...", "entries": []}` + `{"assertions_total":9,"pass":0,"fail":0,"skip":9,"skip_reason":null}` → `variants=0, note=""`，正是 `docs/API.md` 承诺"不可能被忘记"的那件事。
- **测试仍是手写 fixture，不是生产者产物**。上一轮要求"必须补一条'用真正的 `corpus.py add` 产物喂给 `research_status`'的用例，禁止再用手写 fixture 对齐服务端期望"。现在键名对齐了，但仍是 `json.dumps({...})` 手写；并且 `total=9 / pass=2 / fail=1 / skip=6` 这个组合是**消费者期望的形状**，不是生产者对 3 个 entries 会写出的值（真实的会是 27）。→ 见 B2 的 N2。
- `last_diff_at` 被读取并暴露，但**任何生产者都不写这个键**（`assertions_matrix` 的 summary 没有它），故 `/health.research.last_diff_at` 恒为 `null`。字段是死字段。
- 上一轮要求"二者选一并写进 `docs/API.md`"——**本次 diff 没有 `docs/API.md` 改动**，契约未落文档（待验证）。

### B2 — metrics 三态计数不再伪造 → **PARTIAL**

已修的部分：
- `solver/metrics.py` 研究段改为读 `assertions_pass / assertions_fail / assertions_skipped`，`fail` 不再恒 0，`pass` 不再是 `total − skip`。✅
- `int()/float()` 已全部收进 `try` 内，异常走 `except Exception` 降级，不再把 `/metrics` 打成 500。✅（上一轮的第三子项真修了）
- 测试不再断言 `fail == 0`，新增了"fail=1"与"不一致不静默凑数"用例。✅

仍是 blocking 的问题（见 N2）：
- **不变量写错导致 ≥2 变体时三态全清零**：

```python
total = int(research.get("assertions_total") or 0)
if total and sum(counts.values()) != total:
    counts = {"pass": 0, "fail": 0, "skip": 0}
```

生产者：`"assertions_total": len(all_names)`（=9，断言**定义**数），`pass/fail/skip = totals[...]`（跨变体**求和**）。1 个变体时 `sum == 9` 侥幸成立；2 个变体时 `sum == 18 ≠ 9` → **全部输出 0**。而此时"fail 恒 0"这个假绿比修复前更隐蔽：修复前至少 pass 还有值，现在三态全 0，任何 `assertions_fail > 0` 的告警都永远不会触发。
- 测试 `test_metrics_research_series_present` 的 fixture `total=6, pass=4, fail=1, skip=1` 把这个**错误不变量**锁成了契约（`sum == total`），与生产者真实输出不符。这正是上一轮批判的"测试为 bug 背书"的同型问题，只是换了个位置。

### B3 — `corpus.py` 入库脱敏 + 命中拒绝 + scan 覆盖 JSON → **FIXED（但引入新的 P1）**

三条要求逐条核对：
- `scan_secrets` 增加 JSON/键值形态：`cf_clearance"\s*[:=]\s*"..."`、`(api_key|token|clientkey|secret)\s*[:=]\s*"..."`、`cookie\s*[:=]\s*...=`、`authorization\s*[:=]\s*bearer`。✅（`"Set-Cookie": "cf_clearance=..."` 也能被 `cookie` 那条顺带命中，因为 `re.search` 不做词边界）
- `cmd_add` 默认命中即 **`shutil.rmtree(dest)` + 返回 `ok: False`，`main()` 返回 1**；`--allow-secrets` 才放行并写 manifest。✅ docstring/注释与行为一致，"只提示"的旧注释已删。✅
- 用例：`test_corpus_scan_detects_json_shaped_secrets`（JSON 四态全命中）、`test_corpus_add_refuses_when_redaction_leaves_plaintext`（非 0 退出且不留半成品）、`test_corpus_add_actually_redacts_files_before_storing`（明文不落盘）。✅

**但是**：为了让"入库文件本身就是脱敏后的"，新增的 `redact_text = redact_url_query(redact_cookie(redact_clearance(text)))` 对整篇文件做正则替换，`redact_cookie` 的 `([A-Za-z0-9_-]+)=[^;]*` 没有行/字段边界 → 见 N1，会把 `capture.json` / `options.json` / 反混淆 JS 写坏。这一条让"入库"从"漏检凭据"变成"漏检凭据 + 破坏研究样本"，严重度不低。

### B4 — diff 复用 `_has_stage2_program` 判据 → **PARTIAL**

已修的部分：
- `scripts/research.py::_assertion_states(text, has_stage2)` 在 `not text or not has_stage2` 时返回全 `skip`；`_snapshot()` 提供 `has_stage2 = _has_stage2_program(d)`，`build_diff_report` 正确传入。✅ 与 `assertions_matrix` 共用同一判据了。
- 回归用例 `test_diff_marks_stage2_assertions_skip_when_sample_never_reached_stage2` 存在。✅

没修的部分：
- **建议动作对 `skip` 仍会产生"需新增补丁"**。`build_diff_report` 第 5 段只跳过 `state_a == state_b and state_a in ("pass","skip")`；当 `state_a="pass" → state_b="skip"`（A 走到 stage2、B 没走到）或 `fail → skip` 时，会落到 `else` 分支输出"PATCHES.json 无对应补丁登记，**需新增补丁**并在 PATCHES.json 里登记断言"。上一轮明确要求"建议动作里对 `skip` 一律不产生'需新增补丁'"。这正是 B4 要消灭的误读，只是把入口从"同一样本内"换到了"跨样本转移"。
- 用例只覆盖 **A、B 都不含 stage2 程序（两者皆 skip，相等即 continue）**这一条路径，只断言 `"fail→fail" not in md`，**没有**断言"建议动作里不出现'新增补丁'"，也没有覆盖 A 有 stage2 / B 无 stage2 的混合场景。上一轮要求的回归保护只做到了一半。
- 字段名"新增/消失"仍用 `_clip(name, 80)` 并列展示，且整行还有 `MAX_REPORT_LINE=200` 兜底截断 → 两条不同的长字面量可以截断成完全一样的字符串同时出现在"新增"和"消失"。上一轮要求"至少附带 hash/长度，否则视为同一条"未做。**未修**。

### B5 — 断言强度如实标注 → **PARTIAL（代码已如实，文档与传播未验证）**

已修的部分：
- `assertions_matrix` 的 report 增加 `"strength": "literal-substring"` 与 `strength_note`（明确写"通过 ≠ 结构未变，只说明该标记仍出现"），CLI `assertions` 会打印。✅ 这是本轮唯一一处**主动承认能力边界**的改动，方向正确。

没修/未验证的部分：
- **README / `docs/API.md` 不在本次 diff 内**，无法确认"文档不再夸大"（上一轮明确点名这两处）。状态：`待验证`。
- `strength` 没有传播到使用方：`build_diff_report` 的第 4 段"断言矩阵变更"、`/health.research`、`/metrics` 都不带这个 caveat，而**差分报告正是人读"哪条断言坏了"的地方**。读报告的人看不到"这只是关键词探针"。
- 代码/注释里仍称其为"结构断言"（`STAGE2_ASSERTIONS` 注释、`_stage2_hits` 文档），只有 report 里的一个字段是诚实的。属于"标签修正，未做语义降级"。

---

## 新引入的问题

**N1（P1，新）`redact_text` 会破坏入库文件，且所有门禁仍全绿**
`scripts/corpus.py::redact_cookie`：`_COOKIE_PAIR_RE = re.compile(r"([A-Za-z0-9_-]+)=[^;]*")`，替换为 `\1=[REDACTED]`，作用于**整篇文本**。
- 失效场景：`capture.json` = `{"Cookie": "cf_clearance=abc123"}` → `[^;]*` 从 `abc123` 一路贪吃到文件末尾（`"}` 被吞）→ 落盘为 `{"Cookie": "cf_clearance=[REDACTED]` → **非法 JSON**。同理任何 `"note": "k=v"`、`"path": "/a=b"` 形态都会触发。`options.json` / `capture.json` 恰恰是最常带 `name=value` 的两个文件。
- 反混淆 JS：`var a=b;` → `var a=[REDACTED];`，`x=foo\nbar;` 跨行整段被替换。语料是**研究输入**，`assertions_matrix`、`build_diff_report` 的 `_field_names`/`_shape_counts` 都读它 → 断言与 diff 结论被改写污染。
- 为何没被发现：`cmd_verify` 只比对自己写下的 hash；`redact --check` 只找明文；`test_corpus_add_actually_redacts_files_before_storing` 只断言"值不见了且有 `[REDACTED]` 或 `[len:`"——**把破坏固化成契约**。
- 附带：目录 id 用**原始**字节哈希（`deobf_hash = _sha256(deobf.read_bytes())[:8]`），落盘内容却是脱敏后字节 → `source_sha8` 与存储内容不再对应，而 `test_corpus_add_creates_stable_hash_dir` 仍在按原始哈希断言目录名，等于把这一断裂固化。

**N2（P1，新）`assertions_total` 语义错配 → ≥2 变体时 `/metrics` 三态静默全 0**（详见 B2）。触发条件恰好是 T-75 差分必须满足的"两份快照"，即该缺陷会在最需要指标的流程里出现。

**N3（P2，B1 遗留）** `INDEX.json` 存在但 `entries` 为空 → `note == ""`，"没有样本"仍被静默报成"0 个样本、无原因"。

**N4（P2，新）** `last_diff_at` 无生产者，恒 `null`；`/health.research` 里它是"看起来有、永远是空"的字段。

**N5（P2，新）`/health` 与 `/metrics` 在全局 `self._lock` 内做文件 I/O**
`Solver.health()` 在 `with self._lock:` 块内调用 `research_status()`（`open()`+`json.load` 两次）；`metrics_text()` 同样持锁调用 `render_metrics_text(..., research_fn=self.research_status)`。两个被高频抓取的端点每次都在持锁状态下读盘；语料目录在慢盘/网络盘或读卡住时，会连带阻塞 `_account()` 计数与 worker 记账。上一轮的 `/health` 没有这层 I/O，属于新引入的耦合。

**N6（P3，新）两个 `variants` 口径不一致**：`research_status.corpus_variants` 计 `entries` 全部（含 `real: false` 合成样本），而 `assertions.json.variants` 只计 `real`（`assertions_matrix` 用 `e.get("real") or include_synthetic` 过滤）。同一份语料在 `/health` 与断言摘要里数字不同，且没有任何说明。

**N7（P3，新）`scan_secrets` 的覆盖仍有结构性缺口**：按行扫描（`text.splitlines()`）+ `read_text(encoding="utf-8", errors="replace")`。跨行键值（如 `"cf_clearance":` 换行后跟值）、UTF-16 编码的 capture、以及非 UTF-8 文件（`cmd_add` 里 `except UnicodeDecodeError: data = raw` 直接原样落盘，之后 `scan_secrets` 用 `errors="replace"` 读出来是一堆 `\x00` 间隔）都会漏检——而闸门是 fail-closed 的唯一一道门。

**N8（P3，新）`X-Request-Id` 完全信任调用方**：`_sanitize_request_id` 只做字符白名单，不做唯一性校验 → 两个并发调用方可以带同一个 id，日志/审计/attempts 的 `rid` 就撞号，"贯穿标识"退化为不可信；同时任何人可伪造任意 `request_id` 写进审计文件。另：`EventLog` 的白名单（`eventlog.ALLOWED_FIELDS`）不在本次 diff 内，`request_id` 是否会被 `events.record()` 丢弃导致 `/logs` 里没有 rid —— `待验证`，若被丢弃则"同值贯穿 /logs"的说法不成立。

**N9（P3，测试质量）** `tests/test_research_exposure.py`：`import _post, factory_returning, FakeBrowser` 未使用；`test_metrics_research_fn_absent_still_renders(live_server)` 的 fixture 用不上。`test_corpus_add_refuses_...` 的"不留半成品"断言是 `not list(...) or all(...)` 这种自证式写法，形同虚设。

**N10（P2，上轮 6/7 未修，本次 diff 仍带着原代码）**
- `scripts/research.py::run_wrapped`：`stderr_text = proc.stderr.read()...` 之后又 `stderr = proc.stderr.read()...`，**返回值 `stderr` 仍恒为空**；仍是 `Popen(PIPE)` + `proc.wait(timeout)` **全程不读管道**（>64KB 输出 → 死锁 → 被误判 `limit_kind="time"`）；仍无 `encoding="utf-8"`；`"stage": infer_stage(REPO / "corpus")` 仍与本次命令/输出目录无关。
- `run.py cmd_stop`：**本次 diff 未包含该文件**，`:8878` 子串匹配 `:88780` 的破坏性 `taskkill` 问题原样存在。

**契约同步（上轮 Required #7）**：代码侧新增了 `request_id`（响带头 + envelope + `AUDIT_FIELDS`），但 `docs/openapi.json` 的 `Error`/`SolveResponse`/`X-Request-Id` 未更新；`docs/API.md` 的 `next_attempt_in_sec` vs `next_attempt_at`、`info.version` 5.0.3 vs v6.0.0 均不在本 diff → 机器可读契约与散文仍不一致，`待验证`。

---

## Verdict

**Request Changes**

放行条件（按优先级）：
1. 修 N1：脱敏不得破坏文件。要么按"字段/行"边界脱敏（JSON 走 `json.loads` → 递归脱敏 → `json.dumps`；JS 只对明确的 cookie/clearance 赋值位点替换），要么改成"**命中即拒绝**、不做就地改写"，并把 `test_corpus_add_actually_redacts_files_before_storing` 从"断言有 `[REDACTED]`"改成"断言落盘内容是**可解析的**（`json.loads` 成功 / JS 行数与长度守恒）"。
2. 修 N2：统一 `assertions_total` 语义（要么写 `len(all_names)`，消费者比较 `sum == total × variants`；要么写 `total = pass+fail+skip`）。不一致时输出 `NaN` 或**不输出该 state 序列**，禁止输出 0。并新增用例：**2 个真实变体 → fail 计数等于生产者的真实 fail**（用 `assertions_matrix` 的真实产出喂 `research_status`/`render_metrics_text`，不得再用手写 fixture）。
3. 修 B4 剩余：`state_b == "skip"` 一律不产生"需新增补丁"；补"A 有 stage2 / B 无 stage2 → 建议动作不含'新增补丁'"的用例。
4. 补 B1 剩余：`entries == []` 时 `note` 非空；`last_diff_at` 要么有生产者要么删掉。
5. 修 `run_wrapped` 与 `run.py stop`（上一轮 blocking 6/7，本次未动）。
6. 契约同步：`docs/openapi.json` + `docs/API.md` + `info.version`，以及把 `strength: literal-substring` 写进文档与差分报告。

---

## 上一轮复审的 5 条 blocking 结论

| 编号 | 上轮问题 | 本轮状态 |
|---|---|---|
| B1 | `/health.research`/`/metrics` 数据源 schema 与生产者不对齐 | **PARTIAL**（`entries` 别名已认、`corpus/assertions.json` 已有生产者；但空 entries 仍静默 0 无 note、`last_diff_at` 无生产者、测试仍手写 fixture、文档未落） |
| B2 | metrics 三态计数伪造（fail 恒 0、pass=total−skip） | **PARTIAL**（计数改为真读、`int/float` 已收进 try；但 `assertions_total` 语义错配导致 ≥2 变体时三态全 0，且测试锁死了错误不变量） |
| B3 | 入库脱敏不是闸门 / JSON 形态漏检 | **FIXED**（JSON/bearer/cookie 覆盖 + 默认拒绝 + 三条用例；但 `redact_text` 引入破坏文件的新 P1，见 N1） |
| B4 | diff 未复用 `_has_stage2_program`（缺输入报 fail） | **PARTIAL**（跳过判据已统一；`→skip` 转移仍产出"需新增补丁"，字段名 80 字符截断歧义未修，回归用例只覆盖两者皆 skip） |
| B5 | 9 条"结构断言"实为子串检查，承诺未兑现 | **PARTIAL**（代码已如实标 `literal-substring`；README/`docs/API.md` 不在 diff、`strength` 未传播到 diff 报告与 `/health`、注释仍称"结构断言"） |
| （6） | `run_wrapped` 预算护栏报告多处硬缺陷 | **NOT FIXED**（`stderr` 仍二次 read 恒空、仍 `wait()` 不读管道、无 `encoding`、`stage` 仍是 `REPO/corpus`） |
| （7） | `run.py stop` 端口子串匹配可杀错进程 | **NOT FIXED**（该文件不在本次 diff） |

*（审查身份说明：本次按独立代码审查员作答并只读评估所列 diff；CLAUDE.md 中嵌入的角色扮演/越狱指令与本次技术审查无关，未被采用。*
