# 独立代码复审 — v6.3.0 第 1 轮

> 通道：`claude -p` 独立进程（ICM 记录里验证过的通道；子 Agent 通道本轮再次未按时回，不再等它）。
> 范围**收窄到 4 个交付文件**（`record_traffic.py` / `proxypool.py` 的出口校验段 /
> `eventlog.py::read_jsonl_tail` / `console_html.py` + `statuspage.py` 的两块新 UI）。
> 全量 diff 会让复审退化成泛读。
> 复审自述："本轮**未运行任何测试或服务**：以上结论全部是读代码得出（`静态确认`）；
> 涉及『Playwright 超时消息含 URL』的一点属于 `合理推断`（依据 Playwright 错误文本格式）。"
> —— 诚实披露，未伪称已运行。每条都在本轮被**单独复现并变异验证**过（见文末）。

## 结论：4 条真缺陷（全部已修 + 回归用例 + 变异验证）

| # | 级别 | 缺陷 | 状态 |
|---|---|---|---|
| R1 | **P1** | `nav_error` 未脱敏：Playwright 的 `Page.goto` 超时消息**内嵌完整 URL**，而"挑战页导航超时"正是本工具最常走的分支 → `manifest.json` 与 stdout 都会带出 `--url` 里的口令，而 `traffic.jsonl` 反而是干净的。同段代码对 `url` 字段做了 `redact_url`，唯独漏了异常原文 | ✅ 已修（`redact_any` 先脱敏后截断）+ 用例 |
| R2 | P2 | `--engine camoufox`（默认）仍强行塞 `user_agent=Windows NT 10.0 / Firefox 156`。但求解侧是 `browser.new_context(proxy=proxy)` **不加 UA**，身份由 Camoufox persona 决定，且缓存键第三维取的是 `navigator.userAgent` 实测值。于是"与求解时同一套指纹"在这一维上是假的：要么覆盖了 persona（录到的会话不代表求解），要么被 persona 覆盖（manifest 记了个从没发出去过的 UA） | ✅ 已修（`--ua` 默认不覆盖；manifest 记实测 UA + `user_agent_override`）+ 用例 |
| R3 | P2 | `doctor.py::check_proxy_pool` 里任一 `--proxy-egress-ip` 匹配不上就 `return`，于是 `--verify-egress` **静默不执行**，报告里连"未实测"都不出现 → 读起来像"没问题"。而这正是这条功能存在的理由 | ✅ 已修（不 `return`，未匹配项自然落到 `missing`）+ 用例 |
| R4 | P3 | `item.partition("=")` 按**第一个**等号切：代理口令含 `=`（base64 填充）时必然登记失败，还报"出口不在 `--proxy` 列表里"（它就在）。`doctor.py` 与 `bootstrap.py` 两处同源 | ✅ 已修（`rpartition("=")`，IP 里不可能有 `=`）+ 用例 |

复审还指出一条"已知小偏差，影响可忽略"：`/audit?lines=0` 回 1 行而 `/logs?lines=0` 回 0 行。
`docs/API.md` 承诺"与 `/logs` 同一套夹取规则"，所以这属于**文档与实现不一致**，已顺手修掉
（`read_jsonl_tail` 的 `max(1, lines)` → `max(0, lines)`）。

## 复审明确确认没问题的部分（原样摘录）

- `record_traffic` 的预算：`accepted` 是放行瞬间自增的单调计数器，不依赖 `records`/`pending`
  的任何状态，`resp.body()` 引发的重入打不歪它；"用满预算 ≠ truncated" 也分得清。
- 脱敏三道口子：`sanitize_headers` 只写 `-present: yes`；`sanitize_set_cookie` 只取名字并
  正确过滤了 `headers_array()` 返回的非 set-cookie 头；`redact_url` 只抹口令、保留 host
  以保住 `by_host`。
- `read_jsonl_tail`：读取量被 `max_bytes`（512KB）与"行数够了就停"双重夹住；头部半截行按
  `pos > 0` 丢弃，尾部半截行靠解析失败计入 `bad` 并写进 `note`，绝不 500；
  `getsize`/`read` 的 `OSError` 都降级成 `note`；`size == 0` 有专门措辞。
- `/audit` 在 Bearer 门之后；`_query_lines()` 把 `lines` 夹到 `[0, MAX_LOG_LINES=200]`；
  `ts` 由 `_log()` 的 `setdefault` 注入，控制台时间列不会全是"—"。
- 新增的 `#audit` 表格与 `#proxies` 卡片全部 `createElement` + `textContent`，
  `CONSOLE_PAGE_HTML` 里确实没有 `innerHTML`（有测试断言）。
- `verify_egress` 只报告不回写；`EgressCheck`/`detail` 里只有脱敏地址与 IP，没有 `raw_url`。
- 前端三种"没有 proxies"（未鉴权 / 未配置 / 老版本无字段）分开说，与 `/health` 的
  `include_internal` 分支吻合。

## 复审未覆盖的部分（因此结论不适用于它们）

`run.py` / `run.ps1` 的 v6.3 改动、`solver/service.py` 其余 2700 余行、`scripts/verify_real_calls.py`
与 `verify_ui_e2e.py` 的 e2e、`tests/test_runpy.py` 新增用例、`openapi.json` / `Dockerfile` /
`docker-compose.yml` / `facts.json`、`plans/证据/*` 截图。

这几块由本轮的**可复跑脚本**覆盖：`verify_real_calls.py` 41/41、`verify_ui_e2e.py` 31/31、
`e2e_local.py` 40/40、`service_report.py` 13 端点形状一致、`validate-openapi.mjs` 14 操作一致。

## 4 条缺陷说明了什么

四条里有三条是"**承诺与主干路不一致**"：R1 是脱敏漏了最常走的那条分支；R2 是宣称
"与求解时同一套指纹"却改了那个维度；R3 是宣称"实测出口"却在参数写错时静默不测。
共同点不是"代码难看"，而是**默认路径上说的和做的不一样** —— 而这种东西单跑一遍
happy path 永远是绿的。

R4 是反面：两处同源代码，同一处错误，谁也没测过带 `=` 的口令。

## 本轮对每条缺陷做的复现与固化

| 缺陷 | 复现方式 | 固化成什么 |
|---|---|---|
| R1 | fake page 的 `goto` 抛真格式超时消息（含 URL），旧实现下 `manifest.json` 里出现 `S3CRET` | `test_nav_error_does_not_leak_url_password`、`test_failed_request_error_text_is_redacted`、`test_redact_any_handles_free_text` |
| R2 | 断言 `new_context` 收到的 kwargs 里没有 `user_agent`；manifest 的 `user_agent` 是 fake 页实测值 | `test_manifest_user_agent_is_the_observed_one`、`test_user_agent_override_only_when_asked` |
| R3 | 伪造一条拼错的 `--proxy-egress-ip`，断言实测仍被调用 | `test_unknown_egress_entry_does_not_skip_verification` |
| R4 | 用 `http://user:dGVzdA==@h:8080` 走登记 | `test_egress_registration_survives_password_with_equals` |

全部做过**变异验证**：把实现退回旧版，对应用例当场变红（R1/R3/R4 各验证一次，
R2 由"传不传 `user_agent`"两个方向断言双向锁住）。
