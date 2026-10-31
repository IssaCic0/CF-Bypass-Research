---
name: cf-bypass-methods
description: 当需要选择/新增/评估"过 Cloudflare 盾"的方法时使用——四种已实测方法（Camoufox / pydoll / cfsolve 纯协议 / CloakBrowser·patchright）的速度与成功率、硬约束、新增一个方法的判定标准与步骤、A/B 实测协议。
---

# Cloudflare 过盾方法选型与扩展

## 什么时候用这个 skill

- 要给某个抓取/Gateway 场景**选一个过盾方案**
- 要**新增**一种求解方法（新引擎 / 新后端）
- 要**评估/对比**现有方法（跑 A/B）
- 用户问"纯算行不行""为什么这么慢""能不能不加浏览器"

## 一、先看实测数据（2026-11-01，同出口 IP，3 站，每格一次）

| 排名 | 方法 | 类型 | 成功率 | 平均成功耗时 | 一句话 |
|---|---|---|---|---|---|
| 1 | **Camoufox** | 浏览器（Firefox，C++ 层） | **3/3** | **18.7s** | 默认首选：最快最稳 |
| 2 | pydoll | 浏览器（Chromium，CDP 直连） | 2/3 | 74.5s | 用系统 Chrome，慢 4 倍，可作第二引擎 |
| 3 | **cfsolve** | **纯协议**（Rust CLI，无浏览器） | ✅ **可用（站点相关）** | **18.43s**（同站最快） | 已构建+打补丁跑通；只对"会弹挑战且无国家跳转"的站有效 |
| — | CloakBrowser | 浏览器（Chromium，87 处 C++ 补丁） | 未测 | — | 二进制专有，最新版需订阅 |
| — | patchright | 浏览器（Chromium fork） | 未测 | — | 历史同类实测 0/4 |

完整数据与逐站点明细：`docs/AB实测-三种过盾方法.md`。

**同站对比（glassdoor.co.uk）**：cfsolve **18.43s** < Camoufox 19.56s < pydoll 失败(25.72s)。
纯协议不但能过，还是最快、且不起浏览器进程。

⚠️ **"不弹挑战" ≠ "失败"**：steamdb/udemy/quora 对 Safari-iOS 指纹直接放行，cfsolve 会报
`page has no .cf-turnstile container` —— 那说明**没有可解的挑战**，不是方案不行。
选站时要先确认目标站真的会弹挑战（用 Camoufox 访问看是否 403 + `cf-mitigated`）。

## 二、四条硬约束（任何方法都逃不掉，先检查这个再调参）

1. **同出口 IP**：求解与回放必须同一出口 → 缓存键要含出口标识，代理要用一致性哈希粘性
2. **同 UA**：回放必须用求解时那个 UA（`user_agent` 字段），否则静默 403
3. **时效**：约 30 分钟（以 cookie 自身 `expires` 为准），到期前续期
4. **按域隔离**：一个 clearance 只对一个 zone 有效

> 90% 的"突然过不了了"都是这四条之一被破坏，不是算法问题。

## 三、"纯算"这个词要拆开说

| 说法 | 含义 | 结论 |
|---|---|---|
| 纯算（离线推 token） | 不接触浏览器、纯算法推导 | ❌ 已证伪（挑战 VM 每轮重混淆 + 绑运行时） |
| 纯 JS 引擎 | 自己起 goja/jsdom 跑脚本 | ❌ 语法层能跑，环境不自洽 → 拿不到凭证 |
| **纯协议（录制回放）** | 用**录制的真实浏览器快照**回答指纹质询 + 离线算挑战 | ✅ 可行（cfsolve），但 profile 是消耗品 |

差别在"环境从哪来"：前两者**现场编造**，第三种**重放**真实环境。

## 四、要新增一种方法：先判断它是 engine 还是 backend

```
新方法能不能"返回一个 Playwright 兼容的 browser 对象"？
├─ 能（Playwright/CloakBrowser/patchright 这类） → 加 engine（solver/engines.py）
└─ 不能（CLI 子进程 / 自有异步 API / 远程服务）   → 加 backend（solver/backends.py 或新模块）
```

- **engine**：`engines.Engine` 加枚举 + `build_factory()` 加分支 + `INSTALL_HINT` 补安装命令 + `preflight()` 覆盖
- **backend**：实现 `name() / is_configured() / solve(url, timeout, force, **kwargs) -> dict`；
  失败必须抛 `SolveError` 并带准确 `error_code`；`is_configured()` 要在**发请求前**判断（缺 key/缺依赖 → 直接 skip）

**返回体必须与 `/solve` 同形状**（至少 `ok/solved`、产物字段、`user_agent`、`cacheable`、`egress_id`）。
一次性 token 一定 `cacheable=false`。

新增后**必做**：
1. 离线单测（假实现/打桩，不联网）
2. 加进 `--help` 与 `docs/API.md`
3. 跑一次 A/B（见下）并更新 `docs/AB实测-三种过盾方法.md`

## 五、A/B 实测协议（别乱跑，会烧 IP 信誉）

```powershell
python scripts/ab_test.py --sites "https://a/,https://b/,https://c/" --gap 5
```

纪律：
- **每格只打一次**，失败不重试（重试会污染结论）
- 方法之间 sleep（`--gap`），不要同时压同一站
- 记录：方法、站点、是否成功、耗时、失败原因
- 结果落 `plans/证据/ab-methods-<时间戳>.json`
- 累计失败 ≥5 就停，隔天再测

## 六、踩过的坑（血泪清单）

1. **headless Chrome 的 UA 带 `HeadlessChrome`** → 强检测信号；pydoll 生产要非 headless（服务器配 xvfb）
2. **`render=explicit` 不会自动渲染 `.cf-turnstile`** → Turnstile 必须主动 `turnstile.render()`
3. **`await grecaptcha.execute()` 的 Promise 可能永不 resolve** → 改"点火 + 轮询全局变量"（58s → 18.5s）
4. **cfsolve 不支持 Windows**（`tokio::signal::unix` / `clock_gettime`）；需 2 处 cfg 补丁
5. **cfsolve 的 profile 是消耗品**，且只支持 Safari/Chromium 类型。**作者自带的 safari profile 在
   我们出口上是可用的**（glassdoor.co.uk 3/3），但录制新 profile 目前卡在 CF 改版
   （`cf-chl token` 运行时生成）——详见 `tools/cfsolve-patches/README.md`
6. **cfsolve 不支持 Windows**（`tokio::signal::unix`、`clock_gettime`）；补丁在
   `tools/cfsolve-patches/cfsolve-windows-and-capture-fixes.patch`（`git apply` 即用）
7. **capture-proxy 只在收到 Ctrl-C/SIGTERM 时落盘**，且 Windows 上信号投递不可靠
   → 补丁改成每 5 秒周期性 flush
8. **SSRF 守卫只守首跳会被 302 绕过** → 必须在 context 上挂 route 拦每个请求
9. **子资源解析失败（NXDOMAIN）不能判成越权** → 三态（blocked/unresolvable/allowed），且子资源被拦不该整次失败
10. **`0.0.0.0` 要拦**：Linux 上 `connect(0.0.0.0)` 等价于连 localhost

## 七、相关文档

| 文档 | 内容 |
|---|---|
| `docs/方案矩阵.md` | 五条路线对照 + 决策树 + 全部实测数据 |
| `docs/00-索引.md` | **总索引**：能力清单 / 参考仓库→取用→落地 / 文档地图 / 证据 |
| `docs/AB实测-三种过盾方法.md` | 本次 A/B 的完整结果与逐站点明细（含同站对比） |
| `docs/参考仓库深度调研.md` | cfsolve / CloakBrowser / Scrapling 深度调研 |
| `docs/纯协议-cfsolve.md` | 纯协议构建（含 Windows 补丁）、profile 录制、契约 |
| `docs/API.md` | `/solve` 契约与错误码 |
| `docs/对接-new-api.md` | 怎么接进 new-api（三种接法 + 验收清单） |
