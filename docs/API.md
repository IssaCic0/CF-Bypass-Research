# 求解服务 API 参考

> 本文是 `/solve` 契约的**权威来源**；`solver/service.py` 顶部的 docstring 与之一致。
> 改接口时**两边都要改**（或用 `node scripts/validate-findings.mjs` 之后的手工复核兜住）。

- Base URL：`http://127.0.0.1:8878`（默认只监听回环）
- 认证：除 `GET /health` 外，全部端点要求 `Authorization: Bearer <token>`
- 内容类型：`application/json; charset=utf-8`
- 协议：HTTP/1.1，支持 keep-alive

## 认证

token 的装载优先级：环境变量 `CF_SOLVE_TOKEN` → token 文件（默认 `./.env`）→ **首次启动自动生成**并写入 `./.env`（`CF_SOLVE_TOKEN=<token>`，权限 0600）。

```bash
# 取 token（Linux/macOS）
TOKEN=$(grep '^CF_SOLVE_TOKEN=' .env | cut -d= -f2)
# PowerShell
$TOKEN = (Select-String -Path .env -Pattern '^CF_SOLVE_TOKEN=(.+)$').Matches[0].Groups[1].Value
```

服务启动日志会打印 token 的**指纹**（sha256 前 12 位）而不是 token 本体。

---

## POST /solve

求解一个受 Cloudflare 保护页面的 `cf_clearance`。

### Parameters

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `url` | string | 是 | — | 目标绝对地址，必须 `http://` 或 `https://`，≤ 2048 字符 |
| `timeout` | number | 否 | `45` | 本次求解预算（秒），会被收敛到 **[5, 120]** |
| `force` | boolean | 否 | `false` | `true` = 忽略服务端域名缓存，强制重新求解 |
| `want` | string | 否 | `clearance` | 要哪种产物：`clearance`（CF WAF，默认）/ `awswaf` / `turnstile` / `recaptcha_v3`，或**万盾扩展类型**（见下节） |
| `sitekey` | string | `turnstile`/`recaptcha_v3` 必填 | — | 从页面取：`data-sitekey`，或 reCAPTCHA 的 `api.js?render=<sitekey>` |
| `action` | string | 否 | — | Turnstile / reCAPTCHA v3 的 action，要**与目标站一致**（不一致拿到的 token 会被判无效） |
| `cdata` | string | 否 | — | Turnstile 的 customer data（目标站用了就且要一致） |
| `route` | string | 否 | `fixed` | **v4.0 新增**：`fixed`（默认，v3 行为）/ `auto`（先探测再决策）/ `direct`（不求解）/ `pure`（纯协议优先）/ `browser`（浏览器优先）。**v4.3 起 `prefer` 真的影响选路**：`pure` 跳过 pydoll/flaresolverr，`browser` 跳过 cfsolve，被跳过的后端在 `attempts` 里标 `reason=prefer-skip` |

#### `route=auto` 的语义与响应变化

`auto` 会先发**一次普通 GET** 探测目标（复用 `/detect` 的能力与 SSRF 逐跳守卫，独立并发闸门，失败不影响求解），然后：

| 探测结论 | 行为 | 响应 |
|---|---|---|
| 未命中任何防护特征 | **不求解**，直接告诉调用方去请求目标站 | `200 {"ok":true,"solved":false,"no_challenge":true,"route":"auto",…}` |
| Cloudflare `managed`/`jsd` | 照常求解，**纯协议优先**（零浏览器） | 正常成功响应 + `routed_by` |
| Cloudflare `turnstile`/`recaptcha` | 照常求解，浏览器优先（token 必须在浏览器内取） | 正常成功响应 + `routed_by` |
| AWS WAF | 照常求解，浏览器优先；要 token 请把 `want` 设为 `awswaf` | 正常成功响应 + `routed_by` |
| 万盾厂商（DataDome / PerimeterX / Akamai / Arkose / hCaptcha / reCAPTCHA） | **自动分发**到 `captchas/`（`routed_by.reason=multicaptcha`，`detail.multicaptcha_want` 指明选了哪个 want） | 与显式传该 want 相同 |
| 无求解器的防护（Kasada / Incapsula / Shape…） | 明确拒绝（`unsolved` + `stage=detect`），不白等一个超时 | 错误响应 + `routed_by.unsupported` |
| 其它厂商（DataDome / PerimeterX / Kasada …） | **直接拒绝**：`200 {"ok":false,"error_code":"unsolved","stage":"detect"}`，不白等一整个超时 | 带 `routed_by.detail.unsupported` |
| 探测失败 / 闸门满 | 退回 `fixed`，照常求解 | 正常成功响应 + `routed_by.reason="detect-unavailable"` |

成功的求解响应会多一个字段（老客户端忽略即可）：

```json
"routed_by": {
  "mode": "auto", "action": "solve", "reason": "cloudflare-managed", "prefer": "pure",
  "detect": {"protected": true, "providers": ["Cloudflare Bot Management"],
             "challenge_type": "managed", "route": "browser"}
}
```

> **不传 `route` 时行为与 v3 完全一致**，且**不会**产生任何额外探测请求（有用例锁死这条）。

### Response（成功）

```json
{
  "ok": true,
  "solved": true,
  "cf_clearance": "……463 字节……",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
  "cookies": { "cf_clearance": "…", "__cf_bm": "…" },
  "cookie_list": [
    {
      "name": "cf_clearance",
      "value": "…",
      "domain": ".example.com",
      "path": "/",
      "expires": 1761000000.0,
      "http_only": true,
      "secure": true,
      "same_site": "None"
    }
  ],
  "expires_in": 1800,
  "expires_at": 1761000000.0,
  "elapsed": 18.12,
  "cached": false,
  "egress_id": "default"
}
```

字段语义（四条容易踩的）：

- `cookies` 是**兼容字段**（name→value，丢了 domain/path/expires）。新代码请用 `cookie_list`。
- `expires_in` / `expires_at` 取 `cf_clearance` 自带的 `expires`（真实 TTL），并受 `--ttl` **上限**约束；
  剩余寿命短于 30 秒的凭证**不会被返回**（会直接报 `unsolved`），避免客户端拿到静默 403 的死凭证。
- `cached: true` 时 `elapsed` 恒为 `0.0`（表示本次没有求解动作）；不要把它当成"上次求解耗时"。
- `egress_id` 是本次求解用的**出口标识**（有代理池时是"该域粘住的那个出口"），也是缓存键的一部分。
  缓存键 = 域名 + 出口标识（**UA 不进键**）。理由：缓存里存的是成对的「cookie + 当时的 UA」，
  回放时客户端用的就是这对值，所以旧 UA 的凭证配旧 UA 依然自洽；把 UA 放进键反而会让
  跨进程共享归零。真错了（站点也校验 UA）会以 403 暴露给客户端，由自愈逻辑重解。

按 `want` 区分产物（`want` 字段会回显）：

| `want` | 产物字段 | 是否可缓存 | 说明 |
|---|---|---|---|
| `clearance`（默认） | `cf_clearance` | ✅ 按域缓存 | CF WAF 站点级 cookie，绑域+出口 IP+UA，约 30 分钟 |
| `awswaf` | `aws_waf_token` | ✅ 按域缓存 | AWS WAF 的 `aws-waf-token` cookie |
| `turnstile` | `token` + `token_field=cf-turnstile-response` | ❌ 一次性 | 300s 名义寿命，绑页面会话；本服务用官方 widget 在**目标 origin** 下渲染取得 |
| `recaptcha_v3` | `token` + `token_field=g-recaptcha-response` | ❌ 一次性 | 120s 名义寿命，绑 action |

一次性 token 的 `cacheable` 恒为 `false`、`cached` 恒为 `false`（复用必然失败，所以设计上不缓存）。

#### 万盾扩展类型（v6.1 嫁接层）

除上表四类 CF/AWS 产型外，`want` 还接受 11 种**扩展类型**，由 `captchas/` 子包承接
（自带 `common/`，与上游 `captcha-solver` 解耦，是我们自己的副本）。
命中这些值时**完全不进主链路**：不查缓存、不建浏览器、不走后端回落链，
响应里的 `routed_by.backend` 恒为 `"captchas"`。

| `want` | 厂商 / 产物 | 额外必填 | 说明 |
|---|---|---|---|
| `turnstile_x` | Turnstile widget → `token` | `sitekey` + `url` | 与主链路 `turnstile` 是**两套实现**；CF 站请用主链路 |
| `recaptcha_x` | reCAPTCHA v2/v3/invisible → `token` | `sitekey` + `url` | `version`：`v2`（默认）/`v3`/`invisible`；可加 `enterprise`、`secret`、`classifier` |
| `hcaptcha` | hCaptcha → `token` | `sitekey` + `url` | `action:"invisible"` 走 execute 路径 |
| `cloudflare_x` | `cf_clearance` | `url` | 另一套 CF 实现（route 拦截）；**常规用途请用主链路 `clearance`** |
| `awswaf_x` | `aws-waf-token` | `url` | 另一套 AWS WAF 实现；常规用途用主链路 `awswaf` |
| `botguard` | Google OAuth `bgRequest` token | — | 可选 `email`/`password`；URL 有默认值 |
| `datadome` | `datadome` clearance cookie | `url` | 可选 `referer`（让 DataDome 看到与真实流程一致的 framing） |
| `perimeterx` | `_px3` cookie | — | 可选 `render_flow`（默认 `outlook_signup`）触发 press-hold 闸门 |
| `akamai` | `_abck` cookie | `url` | 走 `bmak` 遥测 sensor |
| `aliyun` | `{sceneId, certifyId, deviceToken, data}` | `scene_id` + `prefix` | 拖动轨迹时序敏感，**强制子进程**执行（实测主线程以外 0/12） |
| `arkose` | `fc_token` | `public_key` | FunCaptcha 视觉拼图，ONNX 分类；可选 `game_type`（默认 `4`） |

共同契约：

- **成功信号统一**：所有类型都归一成顶层 `solved` bool（`token` / `cf_clearance` /
  `verify_success` / `success` 任一为真即可）。读 `solved`，不要按类型分支。
- **错误契约与主链路一致**：跑了没解出来 → `200` + `solved:false` + `error`；
  参数错 → `400`；超时 → 该类型自己的 deadline 到点后 `200` + `solved:false`。
- **不缓存、不 prewarm**：扩展类型全部一次性/会话级产物，`route=auto` 命中万盾厂商时
  会直接分发（见下），但不会写域名缓存。
- **依赖**：需要 `cloakbrowser`（`pip install cloakbrowser`，自托管反检测 Chromium）。
  未安装时该类型报依赖错误，主链路不受影响。
- `real_page` / `pre_actions` / `post_fetch` 对支持它们的类型（turnstile_x/recaptcha_x/
  hcaptcha/cloudflare_x/awswaf_x/akamai）可用，语义与主链路一致。

- **出口粘性（cookie 类必读）**：`cloudflare_x` / `awswaf_x` / `datadome` / `perimeterx` /
  `akamai` 的产物**绑出口 IP**。服务端在调用方未显式给 `proxy` 时，会沿用主链路为该域
  粘住的出口（代理池），并在响应里回传 `egress_id`。**客户端回放必须走同一出口**，
  否则 cookie 立刻失效（表现为静默 403）。
- `proxy` 字段是**调用方可信输入**：引擎会用你给的代理去连目标站。服务本身要求 Bearer
  token，非可信调用方不应暴露；它也能让你把浏览器指到任意 `host:port`（含内网代理）。
  这一点与主链路的 `--proxy` 配置不同（后者只来自服务端配置）。

后端优先级（配置了多个时）：**`cfsolve`（纯协议）→ 本地浏览器 → `flaresolverr`**，
失败会自动回落并在 `attempts` 里留下每一跳的原因。

配置了兜底后端（`--flaresolverr`）时，还会多两个字段：

| 字段 | 说明 |
|---|---|
| `backend` | 实际解出凭证的后端（`camoufox` / `flaresolverr`） |
| `attempts` | 尝试轨迹：`[{backend, ok:true}]` 或 `[{backend, ok:false, error_code, stage}]`，未配置的后端记 `{skipped:true}` |

### Response（求解失败 —— 注意是 HTTP 200）

```json
{
  "ok": false,
  "solved": false,
  "error_code": "unsolved",
  "error": "45s 内未取得 cf_clearance（页面标题：'Just a moment...'）",
  "stage": "wait",
  "hint": "该域可能已升级到 Turnstile 或更严的 managed challenge；也可能是出口 IP 被提分。…"
}
```

**两分法契约（客户端只需看一处）**：

- `2xx` → 读 `ok` / `solved`
- 非 `2xx` → 读 `error_code` / `error` / `stage` / `hint`
- 两者**永不**同时出现

### 错误码表

| `error_code` | HTTP | `stage` | 含义与建议动作 |
|---|---|---|---|
| `invalid-request` | 400 | `validate` | url/timeout/force/body 不合法，**或页面重定向/子资源请求了私网地址被拦**；按 `hint` 修参数 |
| `payload-too-large` | 413 | `validate` | **v5.0 新增**：请求体超过 `--max-body-bytes`（默认 65536 字节）。与 400 的区别：400=请求坏了**不该**重试，413=可减小请求体后**重试**（如拆分 URL 列表）。连接会被服务端主动关闭 |
| `unauthorized` | 401 | `admit` | 缺 token 或 token 错；检查 `.env` |
| `busy` | 429 | `admit` | 队列满 / inflight 满；降低并发或加进程 |
| `rate-limited` | 429 | `admit` | 同域求解过频（预留，默认未启用） |
| `unsolved` | **200** | `wait` | 浏览器跑完但没拿到 clearance；换站验证服务本身、或该域已升级 |
| `timeout` | 504 | `wait` | 超过预算；调大 `timeout`（≤120）或降并发 |
| `transport` | 502 | `navigate` | 网络层失败（DNS/连接/TLS） |
| `browser-unavailable` | 503 | `admit` | 浏览器启动失败或中途断开；看 `/health` 的 `last_error` |
| `not-configured` | 503 | `admit` | 未安装 camoufox；`pip install camoufox && python -m camoufox fetch` |
| `internal` | 500 | `internal` | 未预期异常；看服务端 traceback |

### Example

```bash
curl -s -X POST http://127.0.0.1:8878/solve \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://steamdb.info/","timeout":45}' | jq .
```

---

## GET /health — 公开，不需要 token

用于容器健康检查与运维判断。**语义是四态**，不是恒 200：

- `200` + `state: ready` —— 浏览器在线、worker 线程活着
- `503` + `state: starting` —— 首次启动中（浏览器还没起来）
- `503` + `state: degraded` —— 曾经好过，现在挂了；`last_error` 说明原因。worker 会指数退避自愈
- `503` + `state: wedged` —— **v6.2**：worker 卡死（任务超预算 + 宽限仍未返回）。**不会自愈**，
  附带 `wedged_reason` 与 `wedge_restart`；服务模式下进程会以退出码 75 退出交 supervisor 拉起

### 两级视图（**免鉴权只给最小集**）

`/health` 免鉴权，但**返回字段取决于你有没有带 token**：

| 调用方 | 返回 |
|---|---|
| 不带 token（容器探活、负载均衡） | `ok` / `state` / `browser_alive` / `worker_alive` **四个字段** |
| 带 `Authorization: Bearer <token>` | 完整运维视图（下面这一坨） |

为什么：完整视图里有 `last_error`（可能带被拦截子资源的主机名）、代理地址与评分、
缓存域数、UA 指纹、persona —— 这些对"这个容器要不要被摘掉"没用，对同网段的其他人却有用。
健康检查只需要状态码 + `state`。

```json
{
  "ok": true,
  "state": "ready",
  "browser_alive": true,
  "worker_alive": true,
  "browser": "camoufox",
  "backends": ["cfsolve", "camoufox"],
  "captcha_types": ["akamai", "aliyun", "arkose", "awswaf_x", "botguard",
                    "cloudflare_x", "datadome", "hcaptcha", "perimeterx",
                    "recaptcha_x", "turnstile_x"],
  "egress_id": "default",
  "cache_backend": "sqlite:/var/lib/cfsolve/cache",
  "browser_os": "windows",
  "ua_fingerprint": "64898c51c64b",
  "cached_domains": 1,
  "queue_depth": 0,
  "solves": 2,
  "cache_hits": 1,
  "failures": 0,
  "timeouts": 0,
  "rejected_busy": 0,
  "browser_restarts": 0,
  "last_success_at": 1761000000.0,
  "last_error": "",
  "last_error_at": 0.0,
  "uptime_sec": 120.5,
  "avg_elapsed_sec": 16.18,
  "proxies": {
    "size": 2,
    "healthy": 1,
    "entries": [
      {
        "proxy": "http://gw1.example.net:8080",
        "egress_id": "ip:203.0.113.9",
        "egress_ip_known": true,
        "score": 100,
        "weight": 1,
        "consecutive_failures": 0,
        "cooldown_left_sec": 0.0,
        "success": 12,
        "failure": 0
      }
    ]
  }
}
```

`backends` 是**实际生效的后端链**（按尝试顺序）。⚠️ 注意语义边界：上面的三态
（`ready`/`starting`/`degraded`）只反映**本地 Camoufox worker**，不反映链上其它后端的可用性
（cfsolve 的二进制被删、pydoll 的 Chrome 被系统更新挪走，都不会让 `state` 变）。

`proxies` 只在配置了代理池时出现，且**永远不含用户名/密码**（只给脱敏后的 `scheme://host:port`）。

`captcha_types`（v6.1）是**万盾嫁接层受理的扩展 `want` 清单**（11 种，见「万盾扩展类型」一节）。
它只是"受理清单"，**不代表依赖已就绪**：没装 `cloakbrowser` 时这些类型会报
`not-configured` + 安装提示，而 CF 主链路完全不受影响。
带 `_x` 后缀的是避让主链路命名的变体（`turnstile_x` / `recaptcha_x` / `cloudflare_x` / `awswaf_x`），
它们与主链路的同名能力是**两套实现**（CloakBrowser vs Camoufox/pydoll/cfsolve）。

`ua_fingerprint` 是**当前浏览器 UA 的 sha256 前 12 位**（不是 UA 本体），纯观测字段：
UA **不在缓存键里**，所以两个进程这个值不同**照样共享缓存**（键是 `host|egress`）。
它不同只说明两边的浏览器指纹不同 —— 若目标站严格校验 UA，命中旧凭证会以 403 暴露，
此时用 `GetFresh` 强制重解即可。

`browser_os` 表示是否固定了 OS persona（`--browser-os`）；`random` 表示每次启动随机（Camoufox 默认）。

配置了 `--prewarm` 时还会多一个 `prewarm` 字段：

```json
"prewarm": {
  "enabled": true, "targets": 10, "runs": 42, "renewed": 7, "skipped": 410,
  "failures": 1, "last_run_at": 1761000000.0, "last_error": "",
  "hosts": {"https://steamdb.info/": {"failures": 0, "last_error": "", "last_renewed_at": 1761000000.0}}
}
```

语义：`skipped` 是"剩余寿命还够、无需续期"的次数；单个域连续失败会进入指数退避
（`next_attempt_at` 可见），**不会原速重试**（对已提分的域连续求解只会更糟）。

### 卡死状态 `wedged`（v6.2）

`state` 可能出现的第四种值：`wedged`。语义是"worker 线程卡死不返回，服务已废，**等不会好**"。

- 触发：一个任务超过 `timeout + --wedge-grace`（默认 30s）仍未返回，或浏览器启动超过 300s 未就绪；
- 与 `degraded` 的区别：`degraded` 会自愈（浏览器自动重拉），`wedged` 不会；
- 响应里额外带 `wedged_reason` 与 `wedge_restart`；
- 服务模式下（`--wedge-restart`，默认开）进程**以退出码 75 退出**，由 supervisor 拉起 ——
  `docker compose` 的 `restart: unless-stopped` 与 `scripts/run-pool.ps1` 的重启循环都会接住。

> 为什么独立状态而不是合并进 `degraded`：运维看到 `degraded` 的默认反应是"等等看"，
> 而那对卡死是无效动作。状态必须诚实地告诉人"手动/自动重启是唯一出路"。

### research 段（T-76，**仅带 token 的完整视图**）

```json
"research": {
  "corpus_variants": 3,
  "assertions_total": 6,
  "assertions_skipped": 1,
  "last_snapshot_at": 1761000000.0,
  "last_diff_at": 1761000100.0,
  "note": ""
}
```

数据源：`corpus/INDEX.json`（variants 计数）与 `corpus/assertions.json`（断言矩阵，
由 `scripts/research.py` 产出）。**防御性读取**：文件缺失（当前仓库默认状态）时全 0 /
`null`，且 `note` 必须非空说明原因 —— "没有样本/断言全在 skip"这件事**不可能被忘记**。
免鉴权最小集**不含**此段。

---

## POST /detect、GET /detect?url= — 需要 token

**只发一次普通 GET，不启浏览器、不求解**，回答"这个站有没有盾、是什么盾、我该走哪条路"。
规格见 `docs/检测-防护识别.md`。

```http
POST /detect  {"url":"https://target/"}
GET  /detect?url=https://target/
→ 200 {"url":…, "status":403, "protected":true,
       "providers":[{"name":"Cloudflare","confidence":100,"hits":[…]}, …],
       "cloudflare_challenge_type":"managed",
       "recommendation":{"route":"browser","reason":"…","hint":"…"},
       "final_url":"https://target/",
       "backend":"detect"}
```

`recommendation.route` 取值：`direct` / `direct_unless_blocked` / `browser` / `pure_protocol_may_work`
/ `browser_token` / `browser_cookie` / `unsupported_or_browser`。

#### 探测结果按域缓存（v4.1）

同一域名 300 秒内的重复探测会直接返回**缓存结论**（响应里多一个 `"cached": true`），
不再发出站请求。目的是摊掉 `route=auto` 每次多出来的 ≈0.5s。

| 参数 | 位置 | 说明 |
|---|---|---|
| `fresh=1` / `"fresh": true` | `GET /detect?url=…&fresh=1` / `POST` body | 跳过缓存，强制实时探测（运维要看当前真实状态时用） |

服务端开关：`--detect-cache-ttl <秒>`（默认 300，**0 = 关闭**）。

> 缓存命中会**绕过探测并发闸门**（`--max-detect`）——这是有意的：命中不发出站请求，
> 不占出站额度。若你要求"每个请求都必须过闸门"，把 `--detect-cache-ttl` 设为 0。

**`final_url` 是实际拿到响应的那个 URL**（跟随重定向之后）。注意每条跳转都会被独立校验（见下）。

### 与「防护识别」相关的两条硬约束

1. **重定向逐跳校验**：`/detect` 会跟随最多 3 跳（`PROBE_MAX_REDIRECTS`），
   但**每一跳**都过 SSRF 守卫（`guard_hop`）。非 http(s) 协议、解析到私网/回环/云元数据的跳转一律 400。
   超跳数直接 502 `transport`（防止重定向环把 handler 线程挂住）。
2. **独立准入闸门**：`/detect` 有**自己的**并发上限（`--max-detect`，默认 4），与求解的
   `--max-inflight` 分开计。满了立刻 `429 busy` —— 探测是一次同步出站请求，没有背压就等于
   给调用方一个免费的流量放大器。

探测发出的 `User-Agent` **与求解侧同一身份**：浏览器已就绪时用它的真实 UA，
否则按 `--browser-os` 对应的 persona 推。用另一套身份探出来的"有没有盾"和真实求解时看到的并不一致。

## POST /shutdown — 需要 token，且**只允许回环地址**

优雅退出：**先停收新连接 → 等在途请求收尾（最多 10s）→ 再关资源**。
存在的理由：Windows 上 `Stop-Process` / `kill -TERM` 等价于强制终止，
正在求解的调用方会拿到 `WinError 10054 连接被强制关闭`（实测），滚动重启必掉请求。

```http
POST /shutdown      Authorization: Bearer <token>
→ 202 {"ok": true, "state": "shutting-down", "peer": "127.0.0.1"}
```

| 限制 | 说明 |
|---|---|
| 来源 | 默认**只允许 `127.0.0.1` / `::1`**；跨机触发会 401，需要显式 `--allow-remote-shutdown` |
| 鉴权 | 与其它端点同一套 Bearer（`--no-auth` 联调模式下不校验） |
| 时序 | 先返回 202 再异步退出 —— 调用方不会拿到"连接被重置" |

实测（本机，7.02s 的真求解在途中触发 shutdown）：在途请求**拿到 200 + solved=true**，
端口随后释放，日志 `优雅退出：在途请求已全部完成`。

## GET /stats — 需要 token

`/health` 的同义别名（字段一致）。**注意**：它现在要求 token（v2 起除 `/health` 外全部需要鉴权），
所以"旧客户端不带 token 直接调"会得到 401 —— 如果你有这种客户端，改成用公开的 `/health`。

## YesCaptcha 兼容层：`POST /createTask` + `POST /getTaskResult`（可选）

**为什么要它**：存量爬虫/自动化栈大量按 YesCaptcha（2Captcha 系）的"异步任务"协议写代码。
本服务原生是**同步** `POST /solve`，接进去要写适配器。这两个端点是给那类客户端的**薄兼容层**，
让它们把 base_url 一改就能用，不必改代码。

**它只是适配层**：内部仍然调用 `/solve` 的同一套求解链路（同一份缓存、同一套准入、同一份指标），
没有第二条求解路径。不传这两个端点时，`/solve` 的行为**一个字节都没变**。

### 鉴权

三选一，优先 Bearer：

| 方式 | 用法 |
|---|---|
| `Authorization: Bearer <token>` | 与本服务其它端点一致（推荐） |
| body 里的 `clientKey` | YesCaptcha 客户端的习惯写法；值与 Bearer token 相同 |

### `POST /createTask`

```json
{ "clientKey": "<token>", "task": { "type": "TurnstileTaskProxyless",
                                    "websiteURL": "https://example.com/",
                                    "websiteKey": "0x4AAAAAAA..." } }
```

响应（成功）：

```json
{ "errorId": 0, "errorCode": "", "errorDescription": "", "taskId": "a1b2c3d4e5f60718" }
```

### `POST /getTaskResult`

```json
{ "clientKey": "<token>", "taskId": "a1b2c3d4e5f60718" }
```

未完成：

```json
{ "errorId": 0, "errorCode": "", "errorDescription": "", "status": "processing" }
```

完成：

```json
{ "errorId": 0, "errorCode": "", "errorDescription": "", "status": "ready",
  "solution": { "token": "0.abc...", "userAgent": "Mozilla/5.0 ..." } }
```

### 支持的 task type（v6.2：27 种 = 主链路 7 + 嫁接层 12 + 明确拒绝 8）

**A. 主链路类型**（Camoufox / pydoll / cfsolve，按域缓存）

| `task.type` | 映射到 | 必填 | 可选 | `solution` 结构 |
|---|---|---|---|---|
| `AntiCloudflareTask` | `want=clearance` | `websiteURL` | — | `{"cookies":{"cf_clearance":"…"},"userAgent":"…"}` |
| `TurnstileTaskProxyless`<br>`TurnstileTaskProxylessM1` | `want=turnstile` | `websiteURL`、`websiteKey` | `action`、`cdata` | `{"token":"…","userAgent":"…"}` |
| `RecaptchaV3TaskProxyless`</br>`…M1` / `…M1S7` / `…M1S9` | `want=recaptcha_v3` | `websiteURL`、`websiteKey` | `pageAction` | `{"gRecaptchaResponse":"…","userAgent":"…"}` |

*M1 / S7 / S9 是上游的模型变体标记，对本服务是同一件事。*

**B. 嫁接层类型**（`captchas/` 子包，需 `pip install cloakbrowser`；不缓存）

| `task.type` | 映射到 `want` | 必填 | `solution` 结构 |
|---|---|---|---|
| `NoCaptchaTaskProxyless`<br>`RecaptchaV2TaskProxyless`<br>`RecaptchaV2EnterpriseTaskProxyless` | `recaptcha_x`（v2 / Enterprise） | `websiteURL`、`websiteKey` | `{"gRecaptchaResponse":"…","userAgent":"…"}` |
| `RecaptchaV3EnterpriseTask` / `…M1` | `recaptcha_x`（v3 + enterprise） | `websiteURL`、`websiteKey` | `{"gRecaptchaResponse":"…","userAgent":"…"}` |
| `HCaptchaTaskProxyless` | `hcaptcha` | `websiteURL`、`websiteKey` | `{"gRecaptchaResponse":"…","userAgent":"…"}` |
| `FunCaptchaTaskProxyless` | `arkose` | `websiteURL`、`websiteKey`(public key) | `{"token":"…","userAgent":"…"}` |
| `DataDomeTaskProxyless` ∗ | `datadome` | `websiteURL` | `{"cookies":{"datadome":"…"},"userAgent":"…"}` |
| `PerimeterXTaskProxyless` ∗ | `perimeterx` | `websiteURL` | `{"cookies":{"_px3":"…"},"userAgent":"…"}` |
| `AkamaiTaskProxyless` ∗ | `akamai` | `websiteURL` | `{"cookies":{"_abck":"…"},"userAgent":"…"}` |
| `AwsWafTaskProxyless` ∗ | `awswaf_x` | `websiteURL` | `{"cookies":{"aws-waf-token":"…"},"userAgent":"…"}` |
| `CloudflareTurnstileTaskProxyless` ∗ | `turnstile_x` | `websiteURL`、`websiteKey` | `{"token":"…","userAgent":"…"}` |

`∗` = **本仓库扩展名**：YesCaptcha 官方没有对应 task type，但本服务真解得了，
命名沿用生态的 `xxxTaskProxyless` 风格。上游客户端不认识这些名字是正常的 ——
它们存在的意义是"我们的能力有名字可调"，不是冒充官方类型。

**C. 明确不做的类型**（共 8 种，返回 `ERROR_TASK_NOT_SUPPORTED` + 原因，不静默）

`ImageToTextTask` / `…Muggle` / `…M1`、`HCaptchaClassification`、`ReCaptchaV2Classification`、
`AwsClassification`、`FunCaptchaClassification`：这些需要视觉模型后端（OCR / 纯图片分类），
本服务不内置。
`AntiTurnstileTaskProxyLess`：请用 `TurnstileTaskProxyless`。

### 两条刻意的拒绝（都不是"没实现"，是设计）

1. **不接受任务级代理**（`proxyType`/`proxyAddress`/`proxyPort` 等字段）。
   本项目的凭证是"绑定求解时出口 IP"的，而回放侧按 `--egress-id` 选出口。
   若允许任务级代理，就会出现"求解走代理 A、回放走出口 B"→ **稳定的静默 403**
   （这正是 `solver/service.py` 里 `--proxy` 与 `--cfsolve-bin/--pydoll` 被设为启动硬冲突的同一条原因）。
   需要换出口请用启动参数 `--proxy` / `--egress-id`。
   传了任务级代理字段 → `ERROR_PROXY_NOT_SUPPORTED` + 说明。
2. **不提供 `getBalance`**。本服务没有计费模型，返回一个假的余额数字只会误导客户端。

### 业务错误一律 HTTP 200 + `errorId=1`

YesCaptcha 生态的客户端**按 `errorId` 判断成败**，不看 HTTP 状态码。所以这两个端点的业务错误
（含鉴权失败）统一返回 `200` + `errorId=1`，否则客户端会把它当网络错误无限重试。
唯一例外是**传输层畸形**（body 不是 JSON、超过 `--max-body-bytes`）：那属于客户端 bug，
仍按本服务既有约定返回 `400` / `413`。

| `errorCode` | 触发 |
|---|---|
| `ERROR_KEY_DOES_NOT_EXIST` | 缺少/错误的 token 或 clientKey |
| `ERROR_TASK_NOT_SUPPORTED` | 未知 / 明确不做的 `task.type`（`errorDescription` 里带原因） |
| `ERROR_TASK_PROPERTY_EMPTY` | 必填字段缺失或为空（`task` / `task.type` / `websiteURL` / `websiteKey` / `taskId`） |
| `ERROR_TASK_PARAM_INVALID` ∗ | 字段存在但值非法（超长 / 非 http / 类型错） |
| `ERROR_PROXY_NOT_SUPPORTED` ∗ | 传了任务级代理字段 |
| `ERROR_NO_SUCH_CAPCHA_ID` | `taskId` 不存在、已过期（结果保留 10 分钟）或已清理 |
| `ERROR_NO_SLOT_AVAILABLE` | 待处理任务已达上限（`--compat-max-pending`，默认 64） |
| `ERROR_CAPTCHA_UNSOLVABLE` | 求解失败（跑完但没拿到凭证 / 目标站拒绝） |
| `ERROR_SERVICE_OVERLOAD` ∗ | 本服务准入拒绝（队列满 / 并发上限） |
| `ERROR_TIMEOUT` / `ERROR_TRANSPORT` / `ERROR_INTERNAL` ∗ | 与 `/solve` 的 `timeout`/`transport`/`internal` 一一对应 |

`∗` = 本服务扩展码（生态词表里没有对应项）。`ERROR_NO_SUCH_CAPCHA_ID` 的 **`CAPCHA` 拼写就是生态原样**，
不要"修正"成 `CAPTCHA` —— 按 YesCaptcha 文档写死的客户端匹配的就是这个字符串。

> **v6.2 破坏性变更**：v5 用的自造码 `ERROR_TASK_TYPE_NOT_SUPPORTED` / `ERROR_TASK_ID_INVALID`
> 已改为生态码。按错误码分支的客户端请对照上表更新；只看 `errorId` 的客户端不受影响。

### v6.2：兼容层也会**自动挑选**嫁接层

`/solve` 的 `want` 命中扩展类型时走 `captchas/`；兼容层同理（见上表 B）。
区别只在触发方式：`/solve` 用 `want`，兼容层用 `task.type`，映射表是 `solver/compat.py` 里的
`MC_TASK_TYPE_MAP`（与 `TASK_TYPE_MAP` 分立 —— 两者执行路径、依赖与缓存语义都不同）。

### 一个必须知道的代价：兼容层**没有** `route=auto`

YesCaptcha 的协议里没有 `route` 字段，所以兼容层一律按 `fixed` 走 —— 也就是说它**不会**先探测。

实测（2026-11-04，真进程）：对 `https://example.com/`（无 CF 挑战）发一个
`AntiCloudflareTask`，会**白等满 45 秒**才返回 `ERROR_CAPTCHA_UNSOLVABLE: 45s 内未取得 cf_clearance`。

而同样的目标用原生 `POST /solve {"url": "...", "route": "auto"}` 只需 **0.55 秒、0 次求解**
（探测判定"无盾"直接放行，见 `docs/检测-防护识别.md`）。

**所以**：能用原生 `/solve` 的场景就用原生；兼容层是给"客户端代码已经按 YesCaptcha 写好、
不想改"的情况用的。要这个收益，请在调用侧先走一次 `POST /detect`，只把真有 CF 标记的站
丢给 `/createTask`（`scripts/pick-prewarm-targets.py` 就是按这个思路筛目标的，可直接借鉴）。

### 生命周期与上限（防止把兼容层变成资源泄漏）

- 结果保留 **10 分钟**（`COMPAT_TASK_TTL`，**从任务完成时刻起算**，不是从创建时刻），过期后 `getTaskResult` 返回 `ERROR_TASK_ID_INVALID`；
- 待处理任务上限默认 **64**（`--compat-max-pending`），满了立刻 `ERROR_NO_SLOT_AVAILABLE`；
- 每个任务跑在守护线程里，进程退出不会挂在上面；`POST /shutdown` 的优雅退出照常生效。

## GET /audit — 需要 token（v6.3）

读**审计 JSONL 的尾部**（`--audit-log` 写的那个文件）。存在的理由：审计一直在落盘，
但此前没有任何查看手段 —— 只能 ssh 上去 `tail -f | jq`，而"昨天那次失败的完整链路"
恰恰在文件里。参考 [logdy](https://github.com/logdyhq/logdy-core) 的思路：
把日志变成浏览器里能看的东西。

```bash
curl -s -H "Authorization: Bearer $TOKEN" "localhost:8878/audit?lines=50"
```

```json
{
  "lines": [ {"event":"solve","req_id":"ab12","host":"e.com","ok":false,
              "error_code":"unsolved","ms":16530,"ts":1761000000.0}, ... ],
  "total_lines_estimate": 812,
  "truncated": true,
  "note": ""
}
```

| 字段 | 说明 |
|---|---|
| `lines` | 最近 N 条，**新的在前**（与 `/logs` 契约一致） |
| `total_lines_estimate` | **这段**读到的行数，不是全文行数（`truncated=true` 时尤其别当总数用） |
| `truncated` | 只读了文件尾部一段（大文件不全文读取） |
| `note` | 未开启审计 / 文件不可读 / 有坏行等原因说明 |

三条硬约束：

1. **有界读取**：从文件末尾倒着按 64KB 块读，最多 512KB。审计文件可能几百 MB，
   一次 `read()` 就把内存打爆（本项目有过 21GB 内存爆炸史）。
2. **坏行跳过而不是 500**：审计是 append 写入，进程被杀时最后一行可能是半截 JSON。
   跳过的行数会写在 `note` 里，不静默。
3. **只读**：不轮转、不删、不加锁。轮转仍是外部 logrotate 的事。

`lines` 是调用方可控输入，与 `/logs` 同一套夹取规则（`[0, 200]`，非法值回默认，不报 400）。

## GET /status — 需要 token

"**现在正在干什么**"。与 `/stats` 的分工：`/stats` 给总量与运维全量字段，
`/status` 给**在途请求明细**（排障第一问："是卡在哪个请求上、卡了多久"）。

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8878/status
```

```json
{
  "ok": true, "code": 200, "state": "ready", "pid": 12345,
  "backends": ["camoufox"], "cache_backend": "sqlite:/data/cache",
  "uptime_sec": 812.4, "queue_depth": 0,
  "inflight_count": 1, "events_total": 37,
  "inflight": [{ "rid": 12, "method": "POST", "path": "/solve", "elapsed_sec": 3.21 }],
  "multicaptcha_inflight": [{ "type": "datadome", "url": "https://example.com/", "started": 1761000000.0 }],
  "wedge": { "wedged": false, "reason": "", "wedge_restart": true }
}
```

- `multicaptcha_inflight`（v6.1）：万盾嫁接层正在跑的求解。**与 `inflight` 分开列** ——
  两者的超时预算与产物语义不同（主链路是浏览器串行任务，嫁接层是各厂商独立管线），
  混在一张表里排障看不出是谁卡住了。
- `wedge`（v6.2）：卡死状态快照。`wedged=true` 时 `reason` 给原因、`wedge_restart` 说明
  进程会不会自己退出重启。排障第一问"是不是卡死了"不必再跳去 `/health` 看。

- `inflight` **最久的排最前**（卡住的就是它）。
- `GET /status` **不会把自己算进 `inflight_count`**（否则调用方每次都要减一）。
- 参与上表：**刻意不含** `last_error`、代理地址、UA 指纹 —— 那些属于 `/stats`（同样需要 token，
  但 `/status` 的目标读者是"盯着面板看的人"，没必要把这些高敏字段也铺在屏幕上）。

## GET /logs — 需要 token

"**刚才那几条为什么失败**"。内存环形缓冲（最近 200 条），新的在前。

```bash
curl -s -H "Authorization: Bearer $TOKEN" 'http://127.0.0.1:8878/logs?lines=5'
```

```json
{ "ok": true, "total": 37, "kept": 37, "dropped_fields": 0,
  "lines": [{ "ts": 1762..., "event": "solve", "req_id": "a1b2c3d4", "host": "example.com",
              "ok": false, "error_code": "timeout", "stage": "solve" }] }
```

- `total` 是**进入缓冲的累计条数**（缓冲定长，`kept` 才是当前保留数）。
- `lines` 参数是**调用方可控输入**：`lines=abc` 回默认 50，`lines=9999` 夹到 200，`lines=-5` 当 0 —— **都不报 400**
  （只读诊断端点，为参数写错返回错误只会让排障更烦）。
- **字段安全靠白名单**（`solver/eventlog.py` 的 `ALLOWED_FIELDS`）：token / cookie / authorization
  这类字段即使被误传也进不来；被丢弃的字段数会体现在 `dropped_fields` 里，不静默。
- 每个 worker 各有自己的一份（多进程部署时逐个查）。


## GET /metrics — 需要 token

Prometheus 文本格式。暴露的指标：

| 指标 | 类型 | 用途 |
|---|---|---|
| `cfsolve_solves_total` | counter | **真实发起的求解次数（含失败）**；参数错误/队列满这类没真正发起的终止性错误不计入 |
| `cfsolve_backend_total{backend,result}` | counter | 按后端的调用结果（`result=ok\|error`），用来定位"哪一棒在退步"（v4.1） |
| `cfsolve_host_solves_total{host}` | counter | 按域求解次数，**只保留 top-20**（域名是调用方可控输入，全量当标签会把 Prometheus 打爆）（v4.1） |
| `cfsolve_host_failures_total{host}` | counter | 按域失败次数，同样只保留 top-20（v4.1） |
| `cfsolve_cache_hits_total` | counter | 缓存命中 |
| `cfsolve_failures_total` | counter | 求解失败 |
| `cfsolve_timeouts_total` | counter | 超时 |
| `cfsolve_rejected_total` | counter | 准入拒绝（队列满/inflight 满） |
| `cfsolve_process_rss_bytes` | gauge | **v6.2** 进程常驻内存（字节）。内存爆炸的第一现场信号（本项目有过 21GB/31GB 两次） |
| `cfsolve_process_peak_rss_bytes` | gauge | **v6.2** 进程峰值常驻内存（字节） |
| `cfsolve_process_threads` | gauge | **v6.2** 进程线程数（worker 卡死时线程只增不减是一条线索） |
| `cfsolve_browser_restarts_total` | counter | 浏览器重启 |
| `cfsolve_browser_alive` | gauge | 浏览器存活（0/1） |
| `cfsolve_queue_depth` | gauge | 队列深度 |
| `cfsolve_solve_seconds` | histogram | 求解耗时分布 |
| `cfsolve_research_corpus_variants` | gauge | 研究语料变体数（T-76；数据源 `corpus/INDEX.json`，缺失时为 0） |
| `cfsolve_research_assertions{state}` | gauge | 断言矩阵按状态计数（`state=pass\|fail\|skip`；矩阵文件缺失时全 0） |
| `cfsolve_research_snapshot_age_seconds` | gauge | 距上次研究快照的秒数；**无快照时为 `NaN`，绝不填 0**（0 会假装"刚拍过快照"） |

不暴露任何域名、cookie 或 token。

`cfsolve_process_*` 三条序列在**采集失败时直接不出现**（不是报 0）——"没有数据"与"数据是 0"
必须能区分；`/metrics` 也不会因为采集失败而 500。


```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8878/metrics
```

---

## GET /console — 需要 token（T-78 只读控制台）

单文件 HTML 控制台，**零构建、零新依赖、零写入**。四个区块：服务状态 / 缓存与命中 /
研究资产（`/health.research`）/ 最近错误。每块都有空态文案，不会出现空白面板。

**五条硬约束（tests/test_console.py 逐条锁死）**：

1. **零写入**：无表单、无 POST、无任何改状态入口
2. **零新依赖**：HTML/CSS/JS 全部内联在 `solver/console_html.py` 的一个字符串常量
3. **token 保护**：与其它数据端点同一套 Bearer 鉴权；**页面不含 token**，
   前端 `prompt()` 现问、只存 `sessionStorage`（不写 localStorage）
4. **CSP + 无内联事件**：响应头 `Content-Security-Policy: default-src 'none'`、
   `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`；
   DOM 只用 `textContent`/`createElement`，**禁止 innerHTML**，事件全部 `addEventListener`
5. **数据脱敏**：只渲染白名单字段；`last_error` **原文不下发到控制台**
   （健康视图提供 `has_last_error` 布尔 + `last_error_at` 时间戳，控制台只显示
   "有没有错 + 何时"，原文在服务端日志按 request_id 对取）

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8878/console -o console.html
```

**安全说明**：单文件零构建 ⇒ 内联脚本必须 `script-src 'unsafe-inline'`（这是
"零依赖"的已知代价，与 `GET /` 状态页同一防线；`connect-src 'self'` 保证页面里的
fetch 只能打本服务，被注入也无法把 token 外传）。若要更严，下一版迁静态文件 +
`script-src 'self'`。

---

## request_id 贯穿（T-79）

每个请求有一个 12 位小写十六进制标识（`uuid4().hex[:12]`），**四处同值**：

| 出口 | 形式 |
|---|---|
| 响应头 | `X-Request-Id: <rid>`（所有 JSON 响应） |
| 响应体 | envelope 顶层字段 `request_id`（成功与错误 envelope 都有） |
| 审计 JSONL | `request_id` 列（白名单内，与既有 `req_id` 同值） |
| `/solve` 的 `attempts` | 每跳附 `rid` 字段（成功结果与错误 extra 两条路径都覆盖） |

**调用方自带**：请求带 `X-Request-Id` 头时，若值合法（`[A-Za-z0-9_-]{1,64}`）则**原样沿用**
（便于调用方侧串联自己的日志）；非法（含空格/控制字符/超长/HTML 片段）**丢弃并新生成**，
绝不反射回显 —— 这是头注入与日志注入的第一道防线。

**高基数纪律**：rid **绝不**进入 `/metrics` 的 label（tests 锁死）。要按 rid 查，走审计
JSONL 或 `GET /logs`。

---

## 回放要点（v4.6 实测补充，**接入方最容易踩**）

1. **必须用求解时的最终 URL 回放**（跟随 302 之后那个）。原因：`cf_clearance` 的 cookie `domain`
   可能比请求的 host 更具体 —— 例如请求 `https://epicgames.com/`，站点 302 到
   `https://www.epicgames.com/`，凭证的域就是 **`www.epicgames.com`**；
   此时拿它去回放 `epicgames.com` 会被严格的客户端判为"无同域 cookie"而拒绝发请求（这是正确行为）。
2. **回放要带全部 `cookie_list`**，不能只带 `cf_clearance`（CF 还会校验 `__cf_bm` 等）。
3. **回放的 UA 必须是响应里的 `user_agent`**（`cf_clearance` 绑 UA）。
4. 判据：带凭证那一跳若是 2xx/3xx 且正文没有挑战特征 ⇒ **CF 已过**；
   后面若再出现 403/重定向环，多半是**站点业务**（如跳登录页），不是 CF。

## 审计日志（`--audit-log`，v4.3）

`--audit-log <path>` 开启 JSONL 审计：每次 `solve` / `detect` / `shutdown` 落一行。
**字段白名单**：`event/req_id/host/ok/cached/elapsed/clearance_len/ms/error_code/stage/backend/route/reason/peer/ts`。
刻意**不含** clearance / token / cookies —— 审计回答"谁在什么时候打了哪个站、结果如何"，
不回答"凭证是什么"。写盘失败只降级（打一条 `audit-write-failed` 到 stdout），**不会让请求失败**。
轮转交给外部（logrotate / 容器日志驱动），服务内不做。

## 启动参数（与契约相关的部分）

| 参数 | 默认 | 作用 |
|---|---|---|
| `--port` / `--host` | `8878` / `127.0.0.1` | 监听地址。默认只回环；对外暴露前必须先配好 token 与反代 |
| `--ttl` | `1500` | 缓存有效期**上限**（秒）；真实值取 cookie 的 `expires`，且不超过该值 |
| `--max-queue` | `16` | 有界队列容量。满了立刻 429 `busy`，**不排队等** |
| `--max-inflight` | `8` | 并发求解闸门。单进程仍串行求解，这个值限制的是"允许多少请求进来等" |
| `--max-detect` | `4` | **探测（`/detect`）**并发闸门，与求解额度**分开计**。满了立刻 429 `busy` |
| `--egress-id` | `default` | 无代理池时本进程的出口标识。**换出口必须换它** |
| `--cache-dir` | 空（内存） | 落盘缓存目录（多进程共享）。落盘失败会降级为内存并在启动日志与 `/health` 里写明原因 |
| `--proxy` | 空 | 代理出口，可重复。按域一致性哈希做粘性 |
| `--proxy-egress-ip` | 空 | `http://host:port=1.2.3.4`，登记实测出口 IP（改 IP 自动失效旧缓存） |
| `--browser-os` | 空（随机） | 固定 Camoufox 的 OS persona（`windows`/`macos`/`linux`）。默认随机，**每次启动会变**；生产建议固定并与出口 IP 地理一致 |
| `--prewarm` | 空 | 保活目标，逗号分隔（可只写域名）。空=不启用 |
| `--prewarm-file` | 空 | 保活目标文件，一行一个，允许 `#` 注释 |
| `--prewarm-interval` | `60` | 保活扫描间隔（秒） |
| `--renew-before` | `300` | 剩余寿命低于该值时提前续期（秒），应大于单次求解耗时 |
| `--engine` | `camoufox` | 浏览器引擎：`camoufox` / `cloakbrowser` / `patchright`。缺失依赖会**启动即失败**并打印安装命令 |
| `--cfsolve-bin` | 空 | 纯协议后端：cfsolve 的 `challenge` 可执行文件（见 `docs/纯协议-cfsolve.md`） |
| `--cfsolve-profile` | 空 | 纯协议后端：浏览器 profile（`profiles/*.json`） |
| `--cfsolve-timeout` | `120` | 纯协议后端单次超时（秒） |
| `--flaresolverr` | 空 | 可选兜底后端地址（自托管 FlareSolverr 兼容服务） |
| `--token-file` | 环境变量 `CF_SOLVE_TOKEN_FILE`，未设置时 `.env` | token 文件；首启自动生成 `CF_SOLVE_TOKEN=`。容器里务必指到**挂载卷**（如 `/data/.env`）：token 变了派生密钥就变，共享缓存会被整库作废 |
| `--no-auth` | 关 | 关闭鉴权，**仅限本机调试** |
| `--allow-private-hosts` | 关 | 允许访问私网/回环目标，**仅限本机联调** |
| `--headed` | 关 | 有头模式，调试用 |

### 两个启动即失败的硬冲突（不是警告）

1. **`--proxy` 与 `--cfsolve-bin` / `--pydoll` 不能同时启用。**
   这两个后端在各自进程里直连（cfsolve 自带 BoringSSL、pydoll 自带 Chrome），走不了代理池
   ⇒ 它们发出的 `cf_clearance` 绑的是**本机直连 IP**，而 Go 侧回放会按凭证的 `egress_id` 走代理
   ⇒ 求解与回放出口不一致 ⇒ 稳定的静默 403。与其上线后表现为"随机 403"，不如启动就拒绝。
2. **`--engine` 指定的引擎缺依赖 → 直接退出并打印安装命令**（不静默退回其它引擎）。
   静默退回会让运维以为在跑 A 其实在跑 B，排查成本极高。

### 请求体编码

只接受带 `Content-Length` 的请求体（上限 64 KB）。**不支持 `Transfer-Encoding: chunked`**：
收到时返回 400 并**主动关闭连接** —— 因为 `http.server` 不解析 chunked，未读的 chunk 字节会留在
socket 里被当成下一个请求的开头（请求走私/反序列化面），而这段逻辑在鉴权之前就会被执行。

### SSRF 防护的范围与残留风险（诚实披露）

- ✅ 覆盖：调用方给的 URL、**浏览器跟随的所有重定向**、页面加载的全部子资源、
  以及 WebSocket 握手（`route_web_socket`，Playwright ≥1.48）。
- ✅ 判定是三态：解析到私网/回环/链路本地/多播/保留地址 → `blocked`（abort + 整次 `invalid-request`）；
  **解析不出来（NXDOMAIN）→ 放行**，让浏览器自然失败（真实页面大量引用这类广告/统计域名，
  拦了会把主链路打成 400）；**`0.0.0.0` / `::` → 仍然拦截**，因为在 Linux 上
  `connect(0.0.0.0)` 等价于连 localhost，放行等于留一条打本机端口的 SSRF 通道。
  若你的环境用 pihole 之类把广告域名 sinkhole 到 `0.0.0.0`，请让求解机 DNS 返回 NXDOMAIN，
  或显式 `--allow-private-hosts`（后者会关掉整个守卫）。
- ✅ 守卫**装不上就拒绝求解**（fail closed），不会静默裸奔。
- ⚠️ **残留风险（未消除）**：守卫是"先解析再放行"，浏览器随后会**再解析一次**，
  存在 DNS rebinding / TOCTOU 窗口（攻击者控制的域名先解析到公网、再解析到内网）。
  当前缓解手段：默认只监听回环 + 强制 Bearer token + 目标白名单在部署侧收窄。
  若要把这一层也堵死，需要把浏览器出口固定到代理（`--proxy`），或用 egress 防火墙按目标 IP 过滤。
- 这也意味着：**不要把本服务直接暴露到公网**。它设计成"内网 sidecar + 网关调用"。

### 关于落盘缓存的加密与权限

- 内容用 Fernet 加密，密钥由 token + 随机盐（`salt.bin`）经 PBKDF2 派生；**换 token 自动作废旧缓存**。
- 读取失败 / 解密失败 / schema 不符 → 视为未命中并删文件，不会打断服务。
- 写入用"同目录临时文件 + 原子替换"，多进程共享目录时读者不会看到半截文件。
- ⚠️ **Windows 上无法用 chmod 落实 0600/0700**（NTFS ACL 另算）。所以 Windows 部署时
  "文件权限"这一层保护实际是弱的，真正的保护来自加密 + 目录本身不对外。Linux 上会生效。

---

## 后端契约（新后端必须遵守，否则整套缓存会静默失效）

任何一个 cookie 类后端返回的结果都必须满足：

| 字段 | 要求 | 违反后果 |
|---|---|---|
| `expires_in` | **> 0** | `0` 会让 Go 侧 `expiresAt()` 立刻判过期，**每次回放都重新求解** |
| `expires_at` | 绝对时间戳，且在未来 | 服务端 `FileStore` 按 `expires_at <= now` 直接删文件，等于不缓存 |
| `egress_id` | **本进程真实的出口标识**（不要写死后端名） | Go 侧按它选代理；写假的会让"出口一致性"校验形同虚设 |
| `cookie_list` | 结构化（含 domain/path/expires） | 回放要按域过滤 + CF 的 host-only/父域同名去重 |

统一实现是 `solver/backends.py::derive_ttl()` —— **不要再在别处自己算 TTL**。
这条契约历史上被违反过两次（cfsolve、pydoll 各一次），每次的表现都是"缓存看起来有，其实全无效"。
不变量测试：`tests/test_backends.py` 的 `derive_ttl` 系列 + `tests/test_strategies.py` 的两个 TTL 用例。

## Go 客户端（`gocfsolve`）关键选项

```go
cli := cfsolve.New(
    "http://127.0.0.1:8878",
    cfsolve.WithToken(token),
    cfsolve.WithEgressID("default"),
    // 配了代理池时**必须**逐个登记出口，否则回放会 fail closed
    cfsolve.WithEgressProxy("ip:203.0.113.7", "http://user:pass@gw1.example.net:8080"),
    cfsolve.WithRevalidate(true),              // 回放遇 403/挑战页 → 失效并重解一次（默认开）
    cfsolve.WithRevalidateConcurrency(2),      // 跨域重解并发上限（默认 2）
    cfsolve.WithCacheLimit(1024),              // 本地缓存条目上限（默认 1024）
)
```

行为要点（每条都对应一个曾经真实会咬人的问题）：

1. **同一域的并发 `Get` 只求解一次**（per-key 单飞）。没有它，50 个并发冷域 = 50 次求解。
2. **回放自愈**：403 / `cf-mitigated` / 200 但正文是挑战页 ⇒ 丢缓存 + 重解一次 + 重放。
   **只重试一次**，绝不循环。带 body 且不可重放的请求（`GetBody == nil`）不重试。
3. **出口绑定**：只要登记过任何 `WithEgressProxy`，回放就按 `clearance.EgressID` 选代理；
   选不到 → `ErrNoEgressProxy`（**fail closed**，不会"悄悄直连试试"）。
4. **探活/统计用 5s 超时客户端**，不跟求解的 240s 一起挂。
5. 缓存命中返回**深拷贝**（`CookieList`/`Cookies`），调用方改它不会污染缓存。
