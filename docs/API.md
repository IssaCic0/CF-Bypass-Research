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
| `want` | string | 否 | `clearance` | 要哪种产物：`clearance`（CF WAF，默认）/ `awswaf` / `turnstile` / `recaptcha_v3` |
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

用于容器健康检查与运维判断。**语义是三态**，不是恒 200：

- `200` + `state: ready` —— 浏览器在线、worker 线程活着
- `503` + `state: starting` —— 首次启动中（浏览器还没起来）
- `503` + `state: degraded` —— 曾经好过，现在挂了；`last_error` 说明原因。worker 会指数退避自愈

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
（`next_attempt_in_sec` 可见），**不会原速重试**（对已提分的域连续求解只会更糟）。

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
| `cfsolve_browser_restarts_total` | counter | 浏览器重启 |
| `cfsolve_browser_alive` | gauge | 浏览器存活（0/1） |
| `cfsolve_queue_depth` | gauge | 队列深度 |
| `cfsolve_solve_seconds` | histogram | 求解耗时分布 |

不暴露任何域名、cookie 或 token。

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8878/metrics
```

---

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
| `--token-file` | `.env` | token 文件；首启自动生成 `CF_SOLVE_TOKEN=` |
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
