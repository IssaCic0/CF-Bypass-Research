# 独立代码复审 — v6.2.0 第 2 轮（复验修复点 + 相邻回归）

> 同一通道（`claude -p` 独立进程），范围进一步收窄到"上一轮 3 条修复 + 它们的邻居"。
> 复审自述仍是**静态确认**（本会话 Python 执行被权限层拦截），每条附复现命令。

## 结论：R1/R2/R3 三条修复**均成立**；新抓 2 条 P2 + 1 条 P3

| # | 级别 | 缺陷 | 我的处置 |
|---|---|---|---|
| R4 | P2 | `detect.summarize()` 的 `protected = bool(detections)` —— 指纹命中也会让它为真 → `route=auto` 对**无盾站**照常起浏览器（16–20s + 1.2GB） | ✅ 改为 `any(is_shield(d.category) ...)` |
| R5 | P2 | 只有指纹命中时 `recommend()` 落到 fallback，返回 `unsupported_or_browser` + "检测到非 CF 防护：Web Storage Fingerprint" —— 比"什么都没命中"（`direct`）还糟，**语义倒挂** | ✅ 加 `if not shields: return direct` |
| R6 | P3 | `route._MC_PRIORITY` 是死代码（全仓仅赋值一处） | ✅ 删除 |

## R1-a：两张 ctype 表的"相反建议"——我判断为**不是缺陷**，但补了不变量

复审指出 `detect.CTYPE_ROUTE` 与 `route._CHALLENGE_PREFERS` 对 `managed` / `non-interactive`
给出相反建议（前者 `browser`、后者 `pure`），且两边各有测试把它固化。

**我的判断：这不是矛盾，是两张表回答不同的问题。**
- `CTYPE_ROUTE` 回答"这类挑战**要靠什么**才能过"（对外建议，保守）；
- `_CHALLENGE_PREFERS` 回答"后端链里**先试谁**"（顺序偏好；`pure` 只把 pydoll/flaresolverr 挪后，
  cfsolve 与 camoufox 仍在链上，失败继续回落）。

复审自己也写了："对齐方向不能凭直觉定……仓库里没有 cfsolve 对 `non-interactive`/`managed`
的通过率证据，定表前应做一次实测，否则改哪边都是猜。" 我同意，所以**不改任何一边的值**，
而是把关系写进代码注释 + 加一条守门用例
（`test_pure_preference_never_drops_the_browser`：`skip_backends("pure")` 绝不能把
camoufox 一起跳）。这条不变量一破，说明"先试纯协议"变成了"只用纯协议"，那才是真故障。

## 这两轮复审的元结论

第 1 轮抓的 R2 与第 2 轮抓的 R4/R5 是**同一类错误的三次复发**：把"命中某特征"当成
"有盾/有厂商"。每次都修了显式位置，但没修**所有**判断口径。最终收敛成一个谓词
`vendors.is_shield(category)`，三处（`recommend` 的 unsolvable / `summarize` 的 protected /
`route._provider_ids`）全部走它。这比"每处单独修"可靠。

---

## 复审原文

Warning: no stdin data received in 3s, proceeding without it. If piping from a slow command, redirect stdin explicitly: < /dev/null to skip, or wait longer.
## 结论表

| # | 核对项 | 结论 | 证据 |
|---|---|---|---|
| R1 | 键拼写改为 `non-interactive` | **修复成立**：真实产出是连字符；新增回归用例用连字符断言 `prefer=pure`，并校验 `skip_backends` 结果 | `solver/route.py:57`；`tests/test_differential_oracle.py:304-331` |
| R1-a | 两套拼写的隐患 | **无"各走一条路"**：两键值同为 `pure`，且 `cf_challenge_type()` 只可能产出小写正则命中或硬编码的 `turnstile`/`managed`，下划线键不可达。但修复把 **route 与 detect 的 ctype 表变成了相反建议**（见缺陷 1） | `solver/detect.py:232-237` vs `solver/route.py:50-60` |
| R2 | `recommend()` 取 want 改用 `pick_multicaptcha(shields)` | **修复成立**：指纹排前时返回 `multicaptcha`，与 `decide()` 一致；回归用例已覆盖该夹具 | `solver/detect.py:412-416`；`tests/test_differential_oracle.py:334-350` |
| R2-a | shields 为空、只有 Fingerprint 命中 | **未收敛，真缺陷（P2）**：返回 `unsupported_or_browser`，并把指纹当"非 CF 防护"报给调用方 | `solver/detect.py:417-422` |
| R2-b | 同一输入的 `route.decide()` | **未收敛，真缺陷（P2）**：`protected` 未按 `category` 过滤 → 同站返回 `solve` 而非 `skip` | `solver/detect.py:442` → `solver/route.py:139-146` |
| R3 | 绝对导入改 `_vendors` | **修复成立**：包方式导入用例已覆盖 | `solver/route.py:32-46`；`tests/test_differential_oracle.py:353-365` |
| R3-a | `_MAIN_CHAIN` / `_MC_PRIORITY` | `_MAIN_CHAIN` 仍在用（`:164`）；**`_MC_PRIORITY` 是死代码（P3）**：全仓仅赋值处一处引用 | `solver/route.py:42`、`:164` |
| 新问 | `is_shield()` 大小写 | **无当前可复现缺陷**：`SIGNATURES` 的 category 只有 `Anti-Bot`(14)/`ANTIBOT`(4)/`CAPTCHA`(8)/`Fingerprint`(11)，盾类三种全部精确命中；`STRONG_SIGNALS` 全是 `Anti-Bot` | `solver/vendors.py:53-58`；`solver/detect.py:44-206`、`:244-250` |

`MULTICAPTCHA_PROVIDERS` / `SOLVABLE_PROVIDER_IDS` 只被 `tests/test_differential_oracle.py:175,188,364-365` 引用，属于测试契约导出，不是死代码。

---

## 缺陷 1（P2）：只有指纹命中的站被报成"非 CF 防护"

**位置**：`solver/detect.py:417-422`（`shield_top = shields[0] if shields else top` 及其后的 `unsupported_or_browser` 返回）

**触发方式**（纯函数、无 I/O）：
`detect.summarize("https://plain.test/", 200, {}, "<script>localStorage.setItem('x',1);</script>")`

命中只有 `detect-storage-fingerprint`（`Fingerprint`，50 分）；`shields` 为空、`unsolvable` 为空；CF/AWS 分支不命中；`pick_multicaptcha(set())` 返回空；于是 `shield_top` 回落到未过滤的 `top`，返回 `route="unsupported_or_browser"`、`reason="检测到非 CF 防护：Web Storage Fingerprint（难度 Low）"`。

**为什么是缺陷**：同一函数在 `detections` 为空时返回 `direct`（`:354-359`），而指纹类命中在 `vendors.is_shield` 里明确**不是盾**（`solver/vendors.py:49-58`）。结果是"有指纹命中"比"什么都没命中"判得更糟——语义倒挂。`unsupported_or_browser` 的前提是"有盾但解不了"，此处前提为假。

**调用方影响**：`/detect` 的 `recommendation` 直接进响应（`solver/service.py:2378`），`scripts/detect.py:93` 打印它，`scripts/pick-prewarm-targets.py:55` 拿它生成"非 CF 防护：本仓库求解不覆盖"的归类原因——对普通站是错话（归类结果碰巧也是跳过，所以没暴露成故障）。

**修法**：在 CF/AWS 分支之后、最后 fallback 之前加 `if not shields: 返回 direct`（理由与空命中分支一致）；或让最后 fallback 只服务 `shields` 非空的场景。

---

## 缺陷 2（P2）：`protected` 未按盾类过滤，`route=auto` 对无盾站照常求解

**位置**：`solver/detect.py:442`（`"protected": bool(detections)`）

**触发方式**：把上面同一份 `summarize` 输出喂给 `route.decide(summ, "auto")`。`_provider_ids` 会把指纹过滤掉得到空集，`unsolvable` 为空、`pick_multicaptcha` 为空、`ctype` 为空，最终返回 `action=solve`、`reason="cloudflare-unknown"`、`prefer=any`——而不是 `no-protection`/`skip`。

**后果**：`/solve?route=auto` 走 `service.py:2369-2370`，会对一个没有盾的站照常起浏览器，正是 `solver/route.py` 开头声明要消除的"无盾站白付 16–20s 与 1.2GB 内存"。`_probe_for_route` 探测缓存命中不发出站请求，也让这条误判在缓存 TTL 内反复生效。

**为什么 R2 没覆盖到**：R2 把"按 category 过滤"只落在 `recommend`/`decide` 的**厂商集合**上，没有落到 `summarize` 的**标志位**。现有用例 `tests/test_differential_oracle.py:251-258` 只断言 `action != reject`，所以返回 `solve` 也是绿的。

**修法**：`protected = any(is_shield(d.category) for d in detections)`（无 `category` 的旧 payload 与 `route._provider_ids` 的兼容分支保持同一口径）；或在 `decide()` 里当 `ids` 为空且 `ctype` 为空时直接 `skip`。选前者，因为它修的是对外响应字段本身。

---

## R1 的收敛方式（回答"有没有更好的收敛方式"）

重复拼写本身不是隐患（同值、旧键不可达），真正的隐患是 **ctype → 后端偏好仍有两张表**，而且修复后两张表对同一页给出相反建议：

- `non-interactive`：`detect.CTYPE_ROUTE` 判 `browser`（`detect.py:235`，提示语"用浏览器引擎"），`route._CHALLENGE_PREFERS` 判 `pure`（`route.py:57`，提示语"纯协议优先（零浏览器）"）。
- `managed` 同样相反（`detect.py:232` vs `route.py:55`），且两边各有测试把它固化（`tests/test_detect.py:136` 期望 `browser`，`tests/test_route.py:56` 期望 `pure`）。`managed` 的分叉是既有的，`non-interactive` 是 R1 修复新激活的。

收敛方案二选一：

1. **推荐**：把 ctype →（人话, 偏好）做成一张表（放 `vendors.py` 或留在 `detect.CTYPE_ROUTE`），`route.decide` 直接消费并删掉 `_CHALLENGE_PREFERS`；入口统一归一化 `ctype.lower().replace("_", "-")`，不保留双拼写。
2. 次选：保留两表，但把差分 Oracle 从"能不能解"扩到"偏好映射"，对每个真实 ctype 断言 `recommend().route` 与 `decide().prefer` 同向。当前 Oracle（`test_differential_oracle.py:104-145`）只比可解性，所以抓不到这类分叉。

对齐方向不能凭直觉定：`docs/参考仓库深度调研.md:92` 把 `non-interactive` 描述成"只等页面自己放行"的浏览器行为（支持 detect 的 `browser`），而 route 的 `pure` 是第 1 轮复审的期望。仓库里没有 cfsolve 对 `non-interactive`/`managed` 的通过率证据，定表前应做一次实测，否则改哪边都是猜。

---

## 验证状态

- 全部为**静态确认**：本会话 `python`/`pytest` 与 MCP 执行均被权限层拦截（与上一轮同因），未实际运行。上述两条缺陷的触发输入都是无 I/O 的纯函数调用，可用一条命令复现：`import detect,route; s=detect.summarize("https://plain.test/",200,{},"<script>localStorage.setItem('x',1);</script>"); print(s["recommendation"]["route"]); print(route.decide(s,"auto").action)`，预期暴露 `unsupported_or_browser` + `solve`。
- R3 死代码结论由全仓 `_MC_PRIORITY` 检索得出（仅 `solver/route.py:42` 一处），不依赖运行。
