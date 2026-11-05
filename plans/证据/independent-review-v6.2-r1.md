# 独立代码复审 — v6.2.0 第 1 轮

> 通道：`claude -p` 独立进程（ICM 记录里验证过的通道；子 Agent 通道三次失败，不再使用）。
> 本轮把范围**收窄到 3 个文件**（vendors.py / route.py / backends.py）—— 全量 diff 10156 行
> 会让复审退化成泛读；收窄后它给出了可执行的结论。
> 复审自述："本会话的权限层拒绝了一切 Python 执行，因此下面的结论是**静态确认**级别，
> 每条都附了可直接复跑的复现命令。"（诚实披露，未伪称已运行。）

## 结论：2 条真缺陷（均已修复）+ 1 条备注（已修复）

| # | 级别 | 缺陷 | 状态 |
|---|---|---|---|
| R1 | P2 | `_CHALLENGE_PREFERS` 的键写成 `non_interactive`（下划线），而 `detect` 真实产出是 `non-interactive`（连字符）→ **永不命中** → `prefer` 静默降级成 `any` → pydoll/flaresolverr 不被跳过 → cfsolve 失败后白起一次 Chrome | ✅ 已修 + 回归用例 |
| R2 | P2 | `recommend()` 取 want 仍用**未过滤的 top**（`detections[0]`）：一个 50 分的 Fingerprint 排在 50 分的 hCaptcha 前面时，返回 `unsupported_or_browser`，而 `decide()` 同输入返回 `multicaptcha` —— **两个公共决策函数再次分叉** | ✅ 已修（改用 `vendors.pick_multicaptcha`）+ 回归用例 |
| R3 | 备注 | `route.py` 的 `from vendors import ...` 是绝对导入，包方式导入会 `ModuleNotFoundError`（当前入口都是脚本式所以未炸） | ✅ 已修 + 回归用例 |

## 复审明确排除的怀疑（5 条，均为"不成立"）

1. `SHIELD_CATEGORIES` 是否漏类别 → 不漏：全仓 producer 只有 `Anti-Bot`/`ANTIBOT`/`CAPTCHA`/`Fingerprint` 四种。
2. `pick_multicaptcha` 返回 `("","")` 的调用方处理 → 唯一调用点用 `if _mc_id` 挡住了。
3. 无 `category` 的旧 payload 会不会被恶意构造利用 → 入参只来自服务端探测，客户端塞不回来。
4. `unsupported` 先于 multicaptcha 会不会"全都能解仍被拒" → 不会，判据是 `id ∉ SOLVABLE`，与"有几个能解"无关。
5. `_session_lock` 死锁 / `_inflight` 漏加 → 都不成立（`_v1()` 不取锁；自增在 try 之前）。

## 这两条缺陷说明了什么

R1 是**表与实现拼写不一致**的静默降级 —— 不报错、不红测试，只是让优化悄悄失效，
而 `tests/test_route.py` 里那条用例的**标题**提到了 `non_interactive`、**函数体**却只断言了 `jsd`。
R2 更值得记：它正是本轮新增的"语义差分 Oracle"声称要根治的那类分叉，
而 Oracle **没抓到它** —— 因为 `_detections()` 夹具把 confidence 恒设为 100，
永远构造不出"Fingerprint 排在 top"的输入。

**教训：差分 Oracle 的价值取决于夹具的构造能力。** 夹具只喂"典型输入"时，
它只能证明"典型输入下一致"，不能证明"任何输入下一致"。已补的两条用例
（`test_recommend_uses_shields_not_raw_top` / `test_every_ctype_is_spelled_like_detect_produces`）
就是把这个缺口补上。

---

## 复审原文

Warning: no stdin data received in 3s, proceeding without it. If piping from a slow command, redirect stdin explicitly: < /dev/null to skip, or wait longer.
## 结果

**结论：2 条 P2 真缺陷（均在 `route.py` / `vendors.py` 契约边界上），1 条待确认隐患；你列的其余 5 个怀疑点核对后不成立。**

先说明执行环境：本会话的权限层拒绝了一切 Python 执行（不是代码问题，是批准策略），因此下面的结论是**静态确认**级别，每条都附了可直接复跑的复现命令。实际尝试与真实输出：

```
$ python -m pytest tests/test_backends.py tests/test_differential_oracle.py -q
→ This command requires approval
$ python -c "import solver.route"
→ This command requires approval
$ pytest --version
→ This command requires approval
```

`rg` / `Read` 等只读工具可用，`python`/`pytest`/`where.exe` 一律被拦。

---

# 缺陷 1（P2）：`route.py` 里 `non_interactive` 这个键永远不命中，`prefer` 静默降级成 `any`

**位置**：`solver/route.py:55`（表）、`:175-176`（查表）

```python
175  ctype = str(detect.get("cloudflare_challenge_type") or "").lower()
176  prefer = _CHALLENGE_PREFERS.get(ctype, "any")
```

**触发输入**：任意真实 CF 挑战页，`_cf_chl_opt` 里 `cType:'non-interactive'`（连字符），经 `/solve?route=auto` 走 `detect.summarize` → `_probe_for_route` → `decide()`。

**错误行为**：`cf_challenge_type()` 的输出来源是 `detect.py:240` 的正则 `[A-Za-z_-]+`，产物是 `non-interactive`（连字符，`detect.py:235` 的 `CTYPE_ROUTE` 用的是同一拼写，`docs/参考仓库深度调研.md:87` 也是）。`_CHALLENGE_PREFERS` 的键写成 `non_interactive`（下划线），查不到 → `prefer="any"` → `skip_backends("any")` 返回空集 → pydoll / flaresolverr 不会被跳过。表里写明的意图（`jsd`/`non-interactive` 走纯协议）在这条路径上从不生效：cfsolve 失败后会白起一次 pydoll（整只 Chrome，~16–20s 且吃内存），而不是直接落到 camouflaged。

**为什么一直没人发现**：`tests/test_route.py:68` 的用例标题写着「jsd / non_interactive 都是"算出来就行"」，但函数体只断言了 `jsd`（`:69`）。这个键从来没被真实拼写过。

**复现（请执行）**：
```powershell
python -c "import sys;sys.path.insert(0,'solver');import detect,route;print(detect.cf_challenge_type('<script>window._cf_chl_opt={cType:\'non-interactive\'};</script>'));d={'protected':True,'providers':[{'id':'detect-cloudflare','name':'Cloudflare','category':'Anti-Bot'}],'cloudflare_challenge_type':'non-interactive'};dec=route.decide(d,'auto');print(dec.prefer, route.skip_backends(dec.prefer))"
```
期望 `non-interactive  pure frozenset({'pydoll', 'flaresolverr'})`；实际应为 `non-interactive  any frozenset()`。

---

# 缺陷 2（P2）：`recommend()` 仍用"未过滤的 top"取 want，`vendors.py` 的单一事实源在检测侧没接上

**位置**：`solver/vendors.py:74-82`（`pick_multicaptcha`，只有 `route.decide` 在用）/ `solver/detect.py:362`、`:406`

**触发输入**：一个页面正文里同时出现「某万盾厂商的 content 级特征」和「某个 Fingerprint 特征，且名字排序更靠前」。两个置信度都是 50 时按 `(-confidence, name)` 排序，`CSS Fingerprint` / `Battery API Fingerprint` / `Web Storage Fingerprint` 会排在 `hCaptcha`、`FunCaptcha`、`PerimeterX` 之前。`getComputedStyle` / `sessionStorage` 在任何现代站点正文里都常见，而万盾厂商只要正文里提到短 token（自托管 hCaptcha、内联 arkose 片段）就只拿到 50。

**错误行为**：`recommend()` 拿 `detections[0]`（含 Fingerprint）去查 `MULTICAPTCHA_PROVIDERS`，查不到 → 返回

```
route="unsupported_or_browser", reason="检测到非 CF 防护：CSS Fingerprint（难度 Low）…"
```

而同一份输入给 `route.decide()`（`_provider_ids` 已按 category 过滤）会返回 `action=solve, prefer=multicaptcha, want=hcaptcha`。两个公共决策函数对同一输入一个说"解不了"、一个说"能解"——正是 `vendors.py` 开头声称已经根治、并写了差分 Oracle 去守的那类分叉。附带后果：`/detect` 的 reason 把「CSS 指纹」当成盾报给调用方，是明显的错话。

**为什么差分 Oracle 没抓到**：`tests/test_differential_oracle.py:66-74` 的 `_detections()` 只构造厂商 id 且 `confidence` 恒为 100，永远构造不出「Fingerprint 排在 top」的输入；`test_all_fingerprint_signatures_are_excluded_from_shield_check`（`:270`）只验证"Fingerprint 不在可解集合里"，没验证 `recommend` 的 `top` 取值来源。

**复现（请执行）**：
```powershell
python -c "import sys;sys.path.insert(0,'solver');import detect,route;b='<script>getComputedStyle(document.body)</script><div class=\'h-captcha\' data-sitekey=\'x\'></div>';s=detect.summarize('https://x.test/',403,{},b);print([(p['id'],p['confidence']) for p in s['providers']]);print(s['recommendation']['route']);print(route.decide(s,'auto').prefer)"
```
期望两条都表示"能解"（`multicaptcha` / `multicaptcha`）；实际应为 `unsupported_or_browser` / `multicaptcha`。

修法在 `vendors.py` 侧：`recommend()` 的 want 查找应改用 `pick_multicaptcha(names)`（或先对 shields 过滤再取 top），让两个实现共用同一个入口。

---

# 你列的怀疑点，逐条核对结论

### 1. `vendors.py` — `SHIELD_CATEGORIES` 是否漏了真实类别？

**没有漏。** 全仓 producer 只有两处：`SIGNATURES`（生成物）与 `STRONG_SIGNALS`。`rg -n "category" solver/detect.py` 的全部取值就是 `Anti-Bot`(14) / `ANTIBOT`(4) / `CAPTCHA`(8) / `Fingerprint`(11)，`STRONG_SIGNALS` 用的是 `Anti-Bot`。没有第五种取值，`{"Anti-Bot","ANTIBOT","CAPTCHA"}` 覆盖了全部盾类。生成器 `scripts/gen_detect_signatures.py:79` 是 `data.get("category")` 直传上游值，但当前落地文件里没有其它值——这属于未来风险，不是本轮缺陷。

### 2. `pick_multicaptcha` 返回 `("","")` 时调用方是否都处理了？

**处理了。** 全仓唯一调用点是 `solver/route.py:159-160`：

```python
_mc_id, _mc_want = _pick_multicaptcha(ids)
if _mc_id and not ids & _MAIN_CHAIN:
```

空元组被 `if _mc_id` 挡掉。没有第二个调用方（`rg pick_multicaptcha` 只命中 `vendors.py` 定义、`route.py` 导入、以及 `tests/test_differential_oracle.py:202`）。**不是缺陷。**

### 3. 没有 category 的旧 payload 走"当成盾"，会不会被恶意构造的 payload 利用？

**不能。** `decide()` 的 `detect` 入参只有唯一来源 `service.py:2369` → `_probe_for_route()` → `probe_protection()`（`solver/probe.py:132` → `detect.summarize()`），结构永远带 `category`。`/solve` 的请求体只解析 `url/route/timeout/...`（`solver/request.py:161`），不存在"客户端把 detect 结果塞回来"的字段。缓存投毒需要写本地 cache 目录，已在授权边界之外。

顺带说明：`category=None → 当盾` 这条兼容分支本身也不是"重新引入 v6.2 那个 P0"，因为旧缓存条目同样是 `Detection.as_dict()` 产出的，`category` 字段从 v6.0 起就在。**不成立。**

### 4. `unsupported` 先于 multicaptcha，会不会出现"全部能解但仍被拒"？

**不会。** `unsolvable_from(ids)` 判的是 `id ∉ SOLVABLE_PROVIDERS`；`SOLVABLE = 主链 2 + 嫁接 6`，正好覆盖 `detect.py` 中所有真有求解器的 id（`retcaptcha_x`/`hcaptcha`/`arkose`/`datadome`/`perimeterx`/`akamai` 均在 `multicaptcha.CAPTCHA_TYPES` 里，有 `test_both_tables_cover_the_same_vendor_ids` 守）。只有当集合里出现 SOLVABLE 之外的 id 时才会 reject，与"有几个能解"无关。顺序正确。**不成立。**

### 5. `FlareSolverrBackend` 死锁可能？

**不存在互相等待。** `_session_lock` 是普通 `threading.Lock`，只有三处获取：`_ensure_session:228`、`close:255`、`solve:284/295`。`_v1()` 从不取锁，因此任何持锁线程都不会再去拿同一把锁——没有递归获取，也就没有自死锁。真实情况是**互斥等待**：`_ensure_session` 持锁做 `sessions.create` 网络 I/O，`close()` 会阻塞到这一步返回为止，上界是 urlopen 的 `timeout`（`solve` 传入的调用方 timeout，最大到 clamp 上限）。这是有界的，且注释已声明"串行备胎"，不构成缺陷。**不成立。**

### 6. `solve()` 在 finally 减 `_inflight`，`_ensure_session` 抛异常会不会漏加？

**不会。** 代码顺序是 `_ensure_session(timeout)`（283）→ 取锁判 `_closing`（284-289）→ `_inflight += 1`（290）→ `try:`（292）→ `finally` 减（295）。`_ensure_session` 和 `_closing` 的 `raise` 都发生在自增之前，try/finally 不会被执行；不存在只减不加，也不存在漏加后继续跑。计数器不会为负。**不成立。**

---

# 备注（不计入缺陷）

`solver/route.py:37` 的 `from vendors import (...)` 是无条件的绝对导入，而上面 32-35 行的 try/except 正是为了支持"包导入"。以包方式导入时 `from . import vendors` 成功、随后 `from vendors import` 抛 `ModuleNotFoundError`。当前所有受支持入口（`python solver/service.py`、`docker CMD`、tests 的 `sys.path.insert(solver)`）都是脚本/顶层导入，**未受影响**；`scripts/capture_sample.py` 那种 `from solver.detect import` 的写法一旦用在 route 上就会炸。改成与 `detect.py:29-31` 相同的 `except` 内 `import vendors` 即可。

# 验证状态

- 缺陷 1、2：**静态确认**（读码 + 拼写/排序/置信度逐项核对 + 相关测试缺口定位），执行复现被权限层拦截，命令已附上。
- 缺陷 1 的"表与实现拼写不一致"另有旁证：`rg "non_interactive|non-interactive"` 全仓仅 `route.py:55` 是下划线，其余（`detect.py:235`、`docs/参考仓库深度调研.md:87`、`scripts/pick-prewarm-targets.py:12`）全是连字符。
- 未跑 `tests/test_backends.py`、`tests/test_differential_oracle.py`，因此不能声称现有用例是否仍绿；按上面的分析，缺陷 2 的用例缺口意味着这两个文件跑了也不会变红。
