# 对接文档：把 CF 求解服务接进 new-api

> 面向：负责 new-api 侧的对接会话 / 开发者。
> 你只需要读完这一页就能接。**不需要改本仓库任何代码。**

- 求解服务：本仓库 `solver/service.py`（HTTP sidecar）
- new-api：任意版本，只要能配"自定义上游 / 自定义请求头 / 前置脚本"三者之一

---

## 0. 先搞清楚一件事：为什么不能"只在 new-api 里加个 Cookie"

`cf_clearance` **同时绑定三样东西**：目标域 + 求解时的**出口 IP** + 求解时的 **User-Agent**。
new-api 自己发请求时，只要这三样里任一与求解时不同，目标站就会返回 **403 挑战页**（而且不报错、看起来像"渠道挂了"）。

所以要接的不是"一个 cookie 值"，而是**一条"先取凭证、再用同一身份回放"的链路**。
这也是为什么下面三种接法都必须满足两条硬约束：

1. **求解服务与 new-api 走同一个出口 IP**（同机部署，或同一个代理出口）；
2. **new-api 发上游请求时，User-Agent 必须用 `/solve` 返回的那个**。

---

## 1. 求解服务接口（只需记住这 3 个）

启动：

```bash
python solver/service.py --host 127.0.0.1 --port 8878 --cache-dir /var/lib/cfsolve/cache
# 首次启动会生成 .env，里面有 CF_SOLVE_TOKEN=<token>
```

### 1.1 取凭证 `POST /solve`

```http
POST http://127.0.0.1:8878/solve
Authorization: Bearer <CF_SOLVE_TOKEN>
Content-Type: application/json

{"url":"https://target.site/some/page", "timeout":45}
```

成功（HTTP 200）：

```json
{
  "ok": true, "solved": true, "want": "clearance",
  "cf_clearance": "……463 字节左右……",
  "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0",
  "cookies": {"cf_clearance":"…","__cf_bm":"…"},
  "cookie_list": [{"name":"cf_clearance","value":"…","domain":".target.site","path":"/","expires":1761000000.0,"http_only":true,"secure":true,"same_site":"None"}],
  "expires_in": 1500, "expires_at": 1761000000.0,
  "elapsed": 16.3, "cached": false, "cacheable": true, "egress_id": "default"
}
```

求解失败也**是 HTTP 200**（这点最容易接错）：

```json
{"ok": false, "solved": false, "error_code": "unsolved", "error": "45s 内未取得 cf_clearance", "stage": "wait", "hint": "…"}
```

**两分法（必须照做）**：

| HTTP 状态 | 读哪个字段 |
|---|---|
| `2xx` | 读 `ok` / `solved` / 产物字段 |
| 非 `2xx` | 读 `error_code` / `error` / `stage` / `hint` |

两者**永不**同时出现。

### 1.2 健康检查 `GET /health`（公开，不需要 token）

```json
{"ok": true, "state": "ready", "browser_alive": true, "queue_depth": 0, "cache_backend": "file:/var/lib/cfsolve/cache", ...}
```

- `200 + state=ready` → 可用
- `503 + state=starting` → 启动中（浏览器还没起来）
- `503 + state=degraded` → 挂了，看 `last_error`

**new-api 侧请用这个做渠道健康检查**，不要用 `/solve` 探活（那会真的起浏览器）。

### 1.3 统计 `GET /stats`、指标 `GET /metrics`（需 token）

`/metrics` 是 Prometheus 文本，可直接接监控。

---

## 2. 三种接法（按改动从小到大）

### 方案 A（**推荐**）：中间薄代理，new-api 只改 base_url

在本机跑一个 ~80 行的小代理：`new-api → 薄代理 → 目标站`。薄代理负责"取凭证 + 套 UA/Cookie + 转发"。

```
new-api 渠道 base_url: http://127.0.0.1:8899   ← 改成这个
                        ↓
薄代理 8899:  收到请求 → 按 host 调 /solve（内部有缓存）→ 用返回的 UA+Cookie 转发到真实站点
```

优点：new-api 侧**零改造**（只改 base_url 和一个上游域名映射）；凭证逻辑全在一处；
缺点：多一跳（+1~2ms）。

实现要点（照抄即可）：

```
func forward(r *http.Request) (*http.Response, error) {
    target := realHost(r)                       // 你的映射规则：base_url 的 host → 真实站
    cl, err := cfsolve.Get(ctx, "https://"+target+"/")   // 命中缓存则 0ms
    if err != nil { return nil, err }
    out := r.Clone(ctx)
    out.URL.Scheme, out.URL.Host = "https", target
    out.Host = target
    out.Header.Set("User-Agent", cl.UserAgent)          // ← 必须
    out.Header.Set("Cookie", cl.CookieHeaderFor(target)) // ← 必须（按域过滤）
    return http.DefaultTransport.RoundTrip(out)
}
```

> 我们已有 Go 客户端 `gocfsolve`（`Get` / `DoWith` / `CookieHeaderFor` / `Pool`），
> 薄代理直接 import 它即可，**不用自己写解析**：`gocfsolve.New(baseURL, WithToken(tok))`。

### 方案 B：new-api 支持"前置脚本/自定义 header"时，直接调 /solve

如果 new-api 的渠道支持在发请求前跑一段脚本（或自定义 header 模板），那就：

1. 发请求前：`POST /solve {"url": <目标页>}` → 拿 `cf_clearance` + `user_agent`
2. 上游请求设：
   - `User-Agent: <返回的 user_agent>`
   - `Cookie: cf_clearance=<返回的 cf_clearance>`（有 `cookie_list` 就按域拼全）
3. 若返回 403 或页面含 `Just a moment` → 调一次 `POST /solve {"force": true}` 重试（**最多 1 次**）

Python 片段：

```python
import httpx

SOLVER = "http://127.0.0.1:8878"
TOKEN  = open(".env").read().split("CF_SOLVE_TOKEN=")[1].split()[0]

def fetch(target: str) -> httpx.Response:
    r = httpx.post(f"{SOLVER}/solve", headers={"Authorization": f"Bearer {TOKEN}"},
                   json={"url": target, "timeout": 45}, timeout=180)
    if r.status_code != 200:
        raise RuntimeError(f"solver {r.status_code}: {r.text[:200]}")
    d = r.json()
    if not d.get("solved"):
        raise RuntimeError(f"unsolved: {d.get('error_code')} {d.get('hint','')}")
    return httpx.get(target, headers={"User-Agent": d["user_agent"],
                                      "Cookie": f"cf_clearance={d['cf_clearance']}"}, timeout=60)
```

### 方案 C：多实例（要吞吐时）

单进程串行求解（≈16s/次），要吞吐就起 N 个进程共用缓存目录：

```powershell
pwsh scripts/run-pool.ps1 -Count 4 -CacheDir "$env:LOCALAPPDATA\cfsolve\cache"
```

Go 侧用 `cfsolve.NewPool([]string{...4 个地址...}, cfsolve.WithPoolToken(tok))`，
它按域一致性哈希选节点，失败自动摘除/回归。

---

## 3. 必须处理的 5 个细节（不处理会踩坑）

| # | 细节 | 怎么处理 |
|---|---|---|
| 1 | **失败也是 200** | 先判断 `resp.StatusCode`，再判断 `body.ok`；不要只看状态码 |
| 2 | **求解慢**（首解 8–31s） | 上游请求超时给足（≥60s）；或先用 `/health` 预热 + `--prewarm` 保活 |
| 3 | **凭证约 30 分钟** | 服务端已按 cookie 的 `expires` 缓存并在到期前续期；客户端无需自己管 |
| 4 | **403 要能自愈** | 收到 403 或挑战页 → 用 `{"force": true}` 重解 **一次**，不要循环重试（会加深封禁） |
| 5 | **换 IP 就失效** | new-api 若配了代理池，**必须**让求解与回放同出口；本服务支持 `--proxy` + 按域粘性，并把出口写进缓存键 |

---

## 4. 错误码表（对接时按这个映射到你自己的错误处理）

| `error_code` | HTTP | 含义 | 你的动作 |
|---|---|---|---|
| `invalid-request` | 400 | 参数/目标非法（含页面跳到私网被拦） | 不要重试，记日志 |
| `unauthorized` | 401 | token 错 | 修配置 |
| `busy` | 429 | 队列满/并发超限 | 退避后重试（指数退避） |
| `unsolved` | **200** | 跑完了但没拿到凭证 | 换站点验证服务本身；或该域已升级 |
| `timeout` | 504 | 超预算 | 调大 timeout（≤120）或降并发 |
| `transport` | 502 | DNS/连接失败（可能是瞬时） | **可重试**（指数退避） |
| `browser-unavailable` | 503 | 浏览器挂了/重启中 | 看 `/health`，退避重试 |
| `not-configured` | 503 | 未安装 camoufox 等 | 修环境 |
| `internal` | 500 | 未预期 | 看服务端日志 |

---

## 5. 验收清单（接完自检）

- [ ] `curl /health` 返回 `200 / state=ready`
- [ ] `curl -X POST /solve -H "Authorization: Bearer $TOKEN" -d '{"url":"<目标>"}'` 拿到 `solved:true`
- [ ] 用返回的 UA + cf_clearance 手动 `curl` 目标页，返回 200 且不含 `Just a moment`
- [ ] new-api 发一次真实请求，抓包确认：上游请求的 `User-Agent` 与 `/solve` 返回的一致
- [ ] 故意停掉求解服务，new-api 侧表现为**可识别的上游错误**（不是挂死）
- [ ] 连续 10 次请求中至少 8 次命中缓存（`/solve` 响应 `cached:true` 或耗时 <100ms）
