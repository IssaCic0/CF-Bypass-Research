# Cloudflare 纯协议（jsd cf_clearance）求解器 —— 可行性侦察报告

**日期**：2026-10-31
**对象**：`lza6/Aggregate-to-2api` 的 `api/cf_clearance_solver.py`（597 行，纯 HTTP 零浏览器）
**问题**：这条"纯 CF 协议过人机验证"的路，现在还能不能走？
**样本与脚本**：`D:\apk-analysis\cf-jsd-recon\`（`analyze.py`、`scan_ctype.py`、`main_*.js`、`challenge_page.html`、`orchestrate_chl_page.js`）

---

## 0. 结论（一句话）

**存量实现已结构性失效，不是补丁级问题。** CF 把挑战编排从 `jsd/oneshot`（明文码表 + 明文帧端点）
演进到了 `orchestrate/chl_page/v1`（575 KB 自解压混淆编排器 + 随机化键名的运行时解码 VM）。
旧实现依赖的两个静态提取点（64 字符码表、jsd_url_path 正则）**都已消失**，修复成本约等于重做一个
挑战编排器仿真器，且对每次 CF 版本更新都脆弱。

**建议**：不在纯协议方向继续投入；过盾走浏览器指纹方案；可复用的是调度层设计（见 §6）。

---

## 1. 存量实现的技术定位

`api/cf_clearance_solver.py` 打的是 Cloudflare 的 **`cType=jsd` 挑战**，机制四步：

1. `GET /cdn-cgi/challenge-platform/scripts/jsd/main.js` → 正则抠 `custom_base64_table`(64 字符) + `jsd_url_path`
2. 构造指纹 payload（`navigator/document/window` 属性存在性对照表）
3. `soco4()` 加密链：`JSON → DEFLATE(fixed-Huffman，自实现 288 码表 + LZ77) → 帧[253,1,flag] → XOR(fnv1a+xorshift32+码表字节) → custom base64`
4. `POST /cdn-cgi/challenge-platform/h/b/jsd/oneshot/{jsd_path}/{ray}` → `Set-Cookie: cf_clearance`

它**不是** Turnstile 方案。项目里 Turnstile / managed challenge 走的是另一条完全不同的路：
`deploy/cf_solver/`（Boterdrop-Solver，camoufox 无头浏览器 + 指纹伪装，三种求解：Turnstile /
cf_clearance(浏览器导航) / AWS WAF），`api/turnstile_client.py` 只是它的客户端。
两条通道在 `api/captcha/protocol.py` 统一成 `CaptchaResult`。

**关键事实**：`CfClearanceSolver` 在 `api/` 生产代码里**零调用**，全仓库只有 `tests/` 引用。
主流程 `imagefree_client.py` 传的是 `turnstile_token`（浏览器解出来的），与纯协议无关。
即：作者写完这条通道后也没接进主流程。

---

## 2. 证据链

### 2.1 失效点一：main.js 老路径被 302 重定向，代码硬判 200

```
GET https://steamdb.info/                                     → 403  cf-mitigated: challenge
GET .../cdn-cgi/challenge-platform/scripts/jsd/main.js         → 302
      location: /cdn-cgi/challenge-platform/h/g/scripts/jsd/c787ce48cf6b/main.js?
```

代码 `follow_redirects=False` 且 `if r_jsd.status_code != 200: return None` → 直接失败。
对 `steamdb.info`、`www.glassdoor.com` 实测均返回 `None`，日志只有 `WARNING cf_clearance: main.js HTTP 302`。

### 2.2 失效点二：码表与 jsd_path 已无法静态提取（这一条是致命的）

即使跟随 302 拿到当前 main.js（42–44 KB，三次抓取长度分别为 43789 / 43173 / 43816 字节，
内容每次不同），静态分析结果：

| 检查项 | 结果 |
|---|---|
| 字符串字面量总数 | **3 个**，内容只有 `'t'`、`'c'`、`'g'` |
| 长度 48–96 的字符串（码表候选） | **0** |
| `oneshot` / `jsd/` 关键字 | **0** |
| `atob` / `fromCharCode` / `charCodeAt` | 2 / 2 / 3（说明字符串全部运行时构造） |
| 文件开头特征 | `window._cf_chl_opt = {ppebL7:"g"}; ~function(){ var _0x3e2ed5=(a=>a).length+1560569392; var a2,a3,...`（控制流平坦化变量池 + 随机 seed 键名） |

`{ppebL7:"g"}` 里的 `"g"` 对应挑战页的 `cFPWv: 'g'`（CF 指纹版本 g），键名 `ppebL7` 随机化。
**所有字符串都在运行时由 `charCodeAt/fromCharCode/atob` 拼出来**，正则抠码表的路子彻底没了。

### 2.3 失效点三：旧 oneshot 端点存在但拒绝请求

```
POST /cdn-cgi/challenge-platform/h/b/jsd/oneshot/.../<ray>  → 400  (body 空，无 Set-Cookie)
GET  同上                                                    → 405
```

不是 404（端点还在服务端），而是拒绝 —— 因为当前 ray 对应的挑战类型不是 `jsd`，
且缺前置握手。旧帧格式没有任何可用性证据。

### 2.4 挑战类型分布：抽样 50 站，`cType=managed` 6/6，`jsd` 0/6

`scan_ctype.py` 对 53 个高流量站点采样（含电商 / SaaS / 论坛 / 新闻 / 招聘）：

```
触发 CF 挑战页（含 _cf_chl_opt）的站点：6 个
cType 分布: {'managed': 6}
其中：steamdb.info / www.glassdoor.com / www.epicgames.com / www.quora.com /
      www.udemy.com / www.canva.com（cf-mitigated: challenge）
jsd 类型命中：0
```

另有一批站点（ebay / trustpilot / tripadvisor / forbes / wsj / reuters）返回 401/403 且 body
仅 770–1738 字节、无 `_cf_chl_opt` —— 那是别的 WAF 或 CF 的静默 block，与挑战求解无关。

### 2.5 新的挑战执行体：575 KB 自解压编排器

managed 挑战页（10536 字节）只有 **1 个内联 script**，它动态注入：

```
script.src = '/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=<ray>'
```

实测该端点：**200，575319 字节**。结构特征：

| 特征 | 值 |
|---|---|
| 字符串字面量 | 194 个（含大量中文百分号编码的挑战 UI 文案，说明是国际化挑战页） |
| 最长字面量 | 125423 字节（自解压 payload 模板）+ 704 / 697 字节解码指令 |
| 变量/键名 | 全随机化：`window._cf_chl_opt.QKAhj8`、`aTwwd7`、`pblx2="challenges.cloudflare.com"` |
| 解码函数 | `c0(...)` 运行时解码（如 `c0(\`663u5uus5ubrrOnm663v5uus5w==\`)`） |
| `oneshot` / `jsd` / `wasm` / `turnstile` 字面量 | 0 / 0 / 0 / 0 |

这是一个**自解压 + 运行时解码的挑战编排 VM**。纯协议复现 = 仿真这个 575 KB 混淆 VM
（含它向 `challenges.cloudflare.com` 发起的多轮握手上报），且 CF 每次改版本都要重做。

---

## 3. 编排演化链（理解"为什么救不回来"）

```
旧（本实现针对）        GET  scripts/jsd/main.js        (200, 明文码表 + jsd_url_path)
                        POST /h/b/jsd/oneshot/{path}/{ray}   → cf_clearance

现状（2026-10）         GET  scripts/jsd/main.js        (302 → /h/g/scripts/jsd/{hash}/main.js)
                        main.js: 3 个字符串，码表运行时构造
                        挑战执行体改为 /h/g/orchestrate/chl_page/v1  (575 KB VM)
                        取证面升级到 challenges.cloudflare.com + 设备/浏览器特征多轮上报
```

版本前缀也从 `h/b` 变成 `h/g`（对应 `cFPWv: 'g'`），说明挑战平台版本已升级；
`cTplV: 5`（模板版本 5）同样指向新编排。

---

## 4. 三条可能的补救路线与成本

| 路线 | 做法 | 成本 | 可行性判断 |
|---|---|---|---|
| A. 仿真 orchestrate VM | 反混淆 575 KB 自解压脚本，还原运行时解码与握手上报，用 Node/Go 重放 | 周级起步，且 CF 更新即失效；需要持续维护 | **不建议**（投入产出比最差） |
| B. 无头浏览器执行 | 让真实浏览器/伪装浏览器跑 orchestrate + challenges.cloudflare.com widget，回收 cf_clearance cookie | 服务器资源（headless chromium / camoufox + xvfb）+ 反检测对抗 | **推荐**，即项目 `deploy/cf_solver` 的思路，确定性最高 |
| C. 专找 `cType=jsd` 站点 | 只对仍是 jsd 类型的站点用纯协议 | 抽样 0/50；即使找到也是长尾站点 | **无意义**（覆盖不了业务目标站点） |

> 补一条 D：**绕开盾**。很多场景根本不需要过盾 —— 换入口域名/API 直连/换上游。这条在号池
> 注册场景往往比过盾便宜得多（例如我们做过的多个 APK 逆向项目都是直接找上游 API 端点）。

---

## 5. 对 new-api 号池注册的影响

1. **不要把"纯协议过 CF"写进任何注册失败路径的兜底方案**。它对 managed challenge（当前主流）
   100% 无效，写进去只会产生"以为能兜底、实际必然失败"的假能力。
2. 号池注册遇到 CF 盾时，现实选项只有三个：换入口（首选）、浏览器指纹方案、放弃该源。
3. 纯协议的适用边界：**只有老配置的 jsd 挑战站**，且要用旧版 main.js 结构（码表明文）。
   抽样中未发现此类站点，不建议作为能力储备。

---

## 6. 值得拿的那部分：`solver_guard.py` 的调度层

`api/solver_guard.py`（552 行）与加密算法无关，是"分布式求解节点池调度器"，设计质量高，
且与 CF 版本演化完全解耦 —— 我们换任何求解后端（浏览器农场 / 第三方打码 / 本地 camoufox）
都能直接复用其思路：

- **节点状态机**：`SolverNodeState`（inflight 占用、连续失败、速率窗口、idle 判定、`allow_solve`）
- **加权选点**：`select_node()` 按 成功率/时延/负载 打分；`select_candidates()` 返回有序 failover 列表
- **熔断与恢复**：`record_failure(reason)` 分 reason（rate_limit / timeout / transport / solver_rejected），
  连续失败开熔断，冷却后半开
- **429 专项处理**：`TurnstileRateLimited` 独立异常 → 立即熔断并切换下一节点（不重试同节点）
- **SSRF 防护**：`validate_solver_url()` 拦私网 IP；`validate_solve_params(url, sitekey)` 做求解参数白名单
  （防止自身被当成 SSRF 跳板或对任意站点发起求解）

这套东西和我们 new-api 里代理池的成功率/权重/冷却自适应是同一类问题，可以合并设计成一个通用
"外部资源池调度"组件。

---

## 7. 复现方式

```powershell
cd D:\apk-analysis\cf-jsd-recon
python analyze.py          # 分析本地 main_*.js 样本 + 抓 steamdb 挑战页
python scan_ctype.py       # 50 站 cType 分布采样（含 _cf_chl_opt 提取）
python probe_browser.py            # 真实浏览器过盾实测（headless）
python probe_browser.py --headed   # 同上，有头模式
python diag_challenge.py           # 诊断卡点：iframe/checkbox/button + 截图
```

样本文件：
- `main_0_*.js` / `main_1_*.js` / `main_2_*.js`：g2.com 的 jsd main.js 三次抓取（内容各不同）
- `main_steamdb.js`：steamdb 的 jsd main.js（同样只有 3 个字符串字面量）
- `challenge_page.html`：steamdb 的 managed 挑战页（10536 字节，含完整 `_cf_chl_opt`）
- `orchestrate_chl_page.js`：575 KB 新编排器样本
- `challenge_screenshot.png`：挑战页截图（诊断用）

**注意**：`scan_ctype.py` 与 `analyze.py` 会发起真实网络请求（普通 GET，不注册不提交），
不要高频跑（对目标站点无意义且有礼貌问题）。

---

## 8. 附：真实浏览器过盾实测（2026-10-31，本机 Playwright Chromium 131）

**结论：vanilla Playwright 过不了 managed challenge（headless 与 headed 都失败）。**

| 目标 | 模式 | 首访 | 最终 title | cf_clearance | 耗时 | 结果 |
|---|---|---|---|---|---|---|
| steamdb.info | headless | 403 | 请稍候… | ❌ | 31.8s | FAIL |
| glassdoor.com | headless | 403 | 请稍候… | ❌ | 31.6s | FAIL |
| steamdb.info | headed | 403 | 请稍候… | ❌ | 28.7s | FAIL |
| glassdoor.com | headed | 403 | 请稍候… | ❌ | 29.0s | FAIL |

挑战页 DOM 诊断（steamdb，有头模式，等 6s 后）：

```
title          = "Just a moment..."
可见 iframe    = 0
checkbox       = 0
可见 button    = 0
input          = 1
challenges.cloudflare.com frame 存在（可定位到 body）
点击 frame body 后等 20s → 仍无 cf_clearance，title 不变
```

**判定**：这是 CF 的**静默型 managed challenge**（无可交互元素，不需要点验证框），
通过与否完全取决于浏览器行为指纹 + 挑战脚本能否跑通。vanilla Playwright 带着
CDP 自动化特征（即便抹掉 `navigator.webdriver`），仍被判为自动化 → 25s 内不放行。

**资源开销实测**（进程树差量法，psutil，排除本机既有 Chrome）：

| 状态 | 进程数 | RSS |
|---|---|---|
| 浏览器空闲（无页面） | 5 | 191 MB |
| 打开 1 个空白页 | 8 | **403 MB** |
| 打开 CF 挑战页 | 9 | **551 MB** |

单实例约 **400–550 MB**；单次求解 5–15s（失败按 30s 超时计），即单实例串行吞吐
约 0.07–0.2 次/秒。要跑 1 次/秒需 5–14 个实例 ≈ **3–8 GB 内存**，外加挑战脚本执行
消耗的 CPU。

**尚未验证（明确标注待验证）**：
- camoufox（项目 `deploy/cf_solver` 实际使用的反指纹 Firefox）在本机能否过 managed challenge
- patchright / undetected-chromedriver 等 CDP 抹除方案
- 第三方打码服务（付费，按红线默认不调用）
- 换干净出口 IP 后成功率是否变化（本机 IP 经多轮探测可能已被 CF 提分）

---

## 9. Go 网关能用吗（分层结论）

| 层次 | 可行性 | 依据 |
|---|---|---|
| Go 内跑求解 | ❌ 不可能 | 求解需要真实浏览器栈（JS VM + 渲染 + 指纹），Go 无等价物 |
| Go 直接 HTTP 回放已拿到的 cookie | ⚠️ 需 uTLS | cf_clearance 绑 **IP + UA + JA3**；Go `net/http` 的 TLS 握手指纹与 Chrome 差异明显，CF 会拒。要用 `github.com/refraction-networking/utls` 模拟 Chrome ClientHello，并保持同 IP、同 UA、同 header 顺序 |
| Go 做调度与客户端 | ✅ 完全可以 | 调外部求解服务（HTTP API）、节点池打分/熔断/failover、限流、用量统计 —— 即 `solver_guard.py` 那套的重写 |

**推荐架构**（若确实需要过盾）：

```
Go 网关 (new-api)  ──HTTP──►  求解服务（sidecar：Python/Node + 反指纹浏览器池）
       │                             │
       └── 拿回 cookie/token ──► 用 utls 回放（同 IP/UA/JA3）──► 目标站
```

Go 侧只承担：任务派发、超时/重试/熔断、并发上限、指标与账务。求解能力放在独立进程，
崩溃隔离、可单独扩容。

> ⚠️ **本节结论已被 §10–§11 的实测修正**：camoufox 求解 + Go 原生 `net/http` 回放即可，
> 至少对已验证站点**不需要 uTLS**。见下。

---

## 10. 补充实测：Camoufox 通过（同步被否的对照组是 vanilla Playwright）

在 `D:\参考项目` 找到 `camofox-browser`（Camoufox 的 REST 封装），同时发现**本机已装好
camoufox 二进制 + Python 包**（`~/AppData/Local/camoufox/cache/browsers/official/152.0.4-beta.30/camoufox.exe`），
因此直接实测其引擎能力：

| 引擎 | 目标 | 首访 | 最终 title | cf_clearance | 耗时 | 结果 |
|---|---|---|---|---|---|---|
| Playwright Chromium（headless） | steamdb.info | 403 | 请稍候… | ❌ | 31.8s | FAIL |
| Playwright Chromium（headed） | steamdb.info | 403 | 请稍候… | ❌ | 28.7s | FAIL |
| **Camoufox（headless）** | **steamdb.info** | 403 | Loading… | ✅ | **19.6s** | **PASS** |
| **Camoufox（headless）** | **glassdoor.com** | 403 | Loading… | ✅ | **14.5s** | **PASS** |

glassdoor 拿到的 body **147843 字节**（真实业务页面，非挑战页）；被取到的 UA 为
`Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:156.0) Gecko/20100101 Firefox/156.0`
（Camoufox 每次随机指纹）。

**差别的原因**：Camoufox 在 **C++ 实现层**篡改 `navigator.hardwareConcurrency`、WebGL
renderer、AudioContext、screen geometry、WebRTC；Playwright 的"抹 `navigator.webdriver`"
只是 JS 层补丁，行为指纹与 TLS 特征仍然暴露。

**资源开销**（psutil 进程差量）：

| 状态 | 进程 | RSS |
|---|---|---|
| Camoufox 空闲（无页面） | 3 | 139 MB |
| Camoufox 打开 1 个页面 | 7 | **≈700 MB** |
| 启动耗时 | — | 3.4s |

单上下文约 **700 MB**（比 Playwright 的 403 MB 更重，因为 Firefox 引擎本身更占内存）。
但浏览器实例可复用，多次求解共享同一个进程组，不是每次求解都付 700 MB。

---

## 11. 补充实测：Go 原生 `net/http` 直接回放 cf_clearance（**200 通过**）

用 Camoufox 取得 `cf_clearance`（460 字节）+ 该次会话的 UA，交给 Go 1.26 原生 `net/http` 回放：

```go
req.Header.Set("User-Agent", ua)          // Camoufox 那次的 Firefox UA
req.Header.Set("Cookie", "cf_clearance=...")
// 无 uTLS、无自定义 TLS 配置
```

结果（实测输出）：

```
https://steamdb.info/        status=200 len=201391 challenge_page=false elapsed=7560ms
https://www.glassdoor.com/   status=403 len=11651  challenge_page=false elapsed=6652ms
```

（glassdoor 403 是因为手里只有 steamdb 的 cookie，属预期。）

另用 `curl_cffi` 交叉验证 TLS 指纹敏感性（同一 cookie、同 UA、换三种 TLS 指纹）：

```
impersonate=firefox  -> 200 len=201278
impersonate=safari   -> 200 len=201278
impersonate=chrome   -> 200 len=201278
```

**结论**：对已验证站点，`cf_clearance` + 同 UA + 同 IP 足以通过，**TLS 指纹（JA3）没有被严格校验**。
因此：

- **Go 网关不需要 uTLS 就能回放** —— 上一节"必须上 uTLS"的判断对这批站点过于保守，按实测修正。
- uTLS 只作为**兜底**（遇到严格校验 JA3 的站点再开），不是默认依赖。

**边界条件（必须遵守，否则 cookie 立刻失效）**：
1. **同出口 IP**：cf_clearance 绑 IP，换 IP 即失效（这正是要用代理池固定出口的原因）
2. **同 UA**：必须复用求解时的 UA，不能自定义
3. **有时效**：一般 30 分钟量级，需按站维护 TTL 并提前续期
4. **按域隔离**：一个 cookie 只对一个 zone 有效

---

## 12. 最终结论（替代 §9 的保守判断）

**可行架构（已实测贯通两端）**：

```
┌────────────┐   HTTP    ┌──────────────────────┐
│ Go 网关     │──────────►│ 求解 sidecar          │
│ new-api    │◄──────────│ camofox-browser:9377  │
└─────┬──────┘  cookie   │ (Camoufox 浏览器池)    │
      │                  └──────────────────────┘
      │ net/http + cookie + 同 UA + 同出口 IP
      ▼
   目标站（200）
```

| 环节 | 方案 | 实测状态 |
|---|---|---|
| 求解 | Camoufox（C++ 层指纹篡改） | ✅ 2/2 站拿到 cf_clearance |
| 传输 | Go 原生 `net/http` | ✅ 200 拿到 201KB 真实页面 |
| 兜底 | uTLS 模拟 Firefox/Chrome 指纹 | 待需要时再开 |
| 服务化 | `camofox-browser`（REST，:9377，Docker 就绪） | 未部署，README 宣称空闲 40MB |

**成本口径**：单实例约 700 MB + 启动 3.4s + 单次求解 15–20s。要 1 次/秒需 ≈15–20 个并发
上下文（可共享进程）→ 约 **4–8 GB 内存**。对比第三方付费求解 API（按次计费），
自建的成本是内存+维护，付费的成本是每次调用单价——按 10 万次/月的量级，自建通常更省。

---

## 13. 附录：`D:\参考项目` 工具地图（本次调研产出，1729 个仓库中筛出）

### 过盾 / 反检测（直接相关）
| 仓库 | 定位 | 价值 |
|---|---|---|
| **camofox-browser** | Camoufox 的 REST 封装（:9377，Docker，OpenAPI） | **首选求解服务**，被本报告 §10 验证 |
| FlareSolverr__FlareSolverr | 老牌：Selenium + undetected-chromedriver 代理服务 | 备选，Chromium 系 |
| stealth-browser-mcp | nodriver + CDP + FastMCP，MCP 形态 | 若走 MCP 接入可用 |
| Virtual-Browser__VirtualBrowser | Chromium 指纹浏览器，多环境隔离，带 automation/server | 多账号隔离场景 |
| **yang0__fpbrowser2api** | RoxyBrowser 指纹浏览器 + 插件自动化 → 把 Sora/Flow/Veo/Seedance/Grok/ChatGPT 封成 API | **与我们的 2API 业务同构，方法论值得研究** |
| ohmycaptcha | 自托管 YesCaptcha 风格求解器，19 种任务含 Turnstile | 自建求解服务，零按次成本 |
| CloudFlareInvisibleSolver | 纯协议 jsd 原型（本报告证伪对象的上游） | 已失效，仅存档 |
| riskbypass_demo | riskbypass.com 官方 demo：akamai/aws-waf/captchafox/cloudflare/datadome/forter/hcaptcha/kasada/perimeterx/recaptchav3/reese84 | 12 种风控的协议参考；服务本身付费 |
| cf-turnstile-token | Peak API（peak.fo）Turnstile CLI 客户端 | 付费方案，~1s 出 token，无浏览器 |
| captcha-solver / captcha-solver1 / mcp-captcha-solver / 2Captcha-MCP / CaptchaFoxChallengeSolver / geetest-bypass / hcaptcha-hsj-reverse | 各类验证码求解 | 按需参考 |

### 邮箱（与邮箱池施工方案直接相关）
| 仓库 | 定位 | 价值 |
|---|---|---|
| **cloudflare_temp_email** (dreamhunter2333) | Cloudflare Workers 临时邮箱完整系统（Web + API + 管理） | **自建无限邮箱池的最佳底座**；我们 `mail_extract.py` 的 AI 识别思路即借鉴它 |
| maillab__cloud-mail | CF Workers 邮件服务（mail-worker + mail-vue） | 同类备选 |
| Jinnrry__PMail | 极简自建邮件服务器（一个域名、一行代码、一分钟） | 自建域名邮箱最轻路径 |

### 逆向工程
`android-reverse-engineering-skill`、`SimoneAvogadro__android-reverse-engineering-skill`、
`js-reverse-skill`、`reverse-skill`、`ghidra-re-skill-main`、`PE-reverse-skill`、
`firefox-reverse`、`firmware-reverse-engineering`、`LaurieWired__GhidraMCP`、`ghidra`、`facebook__redex`

---

*本报告只做技术可行性判断，不构成对任何站点的绕过承诺。所有样本来自公开可访问的挑战页。*

---

## 14. 补充调研：`D:\参考项目` 里的纯协议/纯算能不能行（2026-10-31 二轮）

### 14.1 参考目录里的风控求解器全景（`captcha-solver/`，自托管服务 :8877）

12 个模块：`akamai` `aliyun` `arkose` `awswaf` `botguard` `cloudflare` `datadome`
`hcaptcha` `perimeterx` `recaptcha` `turnstile`。

**全部是 harvest-only（浏览器收割），没有一个纯算生成。** 逐条读 README 的定性：

| 模块 | 官方原文定性 | 是否纯算 |
|---|---|---|
| cloudflare | "navigating the **real URL** and polling the cookie jar" | ❌ 浏览器 |
| turnstile | 三种模式（route-intercept / +verify / real-page），全部基于 Playwright 拦截或真实页面 | ❌ 浏览器 |
| akamai | "loads an Akamai-fronted page in CloakBrowser, lets the bmak sensor boot" | ❌ 浏览器 |
| datadome | "tags.js builds an encrypted jspl payload from **live browser signals**" | ❌ 浏览器 |
| awswaf | "navigating the real URL and letting AWS WAF's silent JS challenge run" | ❌ 浏览器 |
| perimeterx | "A genuine mouseDown → hold → mouseUp (via the input layer)… real hold biomechanics" | ❌ 需真实输入 |
| botguard | 见下 | ❌ 明确否掉离线生成 |
| arkose / hcaptcha / recaptcha / aliyun | 浏览器 + 视觉模型（ONNX / Mistral vision） | ❌ 浏览器 |

### 14.2 最有价值的一句行业原文（`botguard/README.md`）

> **Why extraction, not generation**
> BotGuard tokens **cannot** be generated offline. The VM is:
> - polymorphic (program re-fetched + re-keyed per serve),
> - runtime-bound (private clock offset, per-run nonce, Welford timing signature),
> - iframe-realm bound (collection runs inside a hidden `contentWindow.eval`).

CF 的 `chl_page` 三者全中：程序每次重新混淆下发、绑定运行时（ray/时间戳/nonce）、
在隐藏 realm 中采集。这就是"单靠算法在服务端算出来"这条路走不通的机理说明，
不是我们的能力问题。

### 14.3 技能库对 CF 的定性（`ultimate-browsing/engine/waf_profiles.yaml`）

```yaml
cloudflare_turnstile:
  detectors: { cookie: [cf_clearance, __cf_bm, __cfduid], body: ["Just a moment...", ...] }
  capabilities_needed:
    - needs_js_exec        # MCP Playwright Chromium OK — no real-TLS required
  fallback_when_challenge:
    - surrogate_wayback    # 先用 archive/reader 副本，避免起浏览器
    - playwright_mcp
    - playwright_real_chrome
```

两个可直接抄的工程结论：
1. `needs_js_exec` —— CF 挑战需要真正执行 JS，不是算出来的；
2. **`surrogate_wayback`** —— 遇到挑战先用镜像/阅读器副本绕过，而不是硬解。这比任何求解器都便宜。

### 14.4 实验：不用浏览器，纯 JS 引擎能不能跑 CF 挑战脚本

方法：Node 25 + `jsdom` + `node-canvas` 加载真实挑战页（`challenge_page.html`），
`runScripts: "dangerously"`, `resources: "usable"`，让它自己拉 orchestrate 脚本并执行，
统计缺失的浏览器能力与运行时报错。

**脚本确实跑起来了**（`_cf_chl_opt` 从 14 个键被运行改写为 25 个键，注入了
`cOgUHash` / `cOgUQuery` / 随机键名等运行时字段）。

**但 22 项关键能力里缺 12 项**：

```
OK   Canvas 2D / toDataURL / hardwareConcurrency / plugins / languages
OK   screen.colorDepth / requestAnimationFrame / crypto.getRandomValues
OK   iframe.contentWindow / Intl
MISS WebGLRenderingContext      MISS WebGL2RenderingContext
MISS AudioContext               MISS webkitAudioContext     MISS OfflineAudioContext
MISS navigator.deviceMemory     MISS performance.timing
MISS RTCPeerConnection          MISS Worker
MISS Notification               MISS document.fonts
```

**运行时报错（真实输出）**：

```
[Cloudflare Turnstile] Unhandled error: b1[(ve || ve)][(vo || vo)] is not a function
Uncaught [TypeError: b1[(ve || ve)][(vo || vo)] is not a function]
Not implemented: navigation to another Document
Cannot read properties of undefined (reading 'querySelector')
```

即：DOM 操作与加密编排能跑，但**一到指纹采集层（WebGL / Audio / WebRTC / Worker /
字体）立刻断链**，且 error 出现在 CF 自己的异常处理器里（说明它检测到了环境不完整）。

**这 12 项不是"补几个 shim"能解决的**：WebGL 需要软件渲染器（SwiftShader/OSMesa）、
AudioContext 需要音频处理栈、WebRTC 需要完整协议栈、Worker 需要真线程、`document.fonts`
需要字体文件与度量实现 —— 补完就等于自己写一个浏览器。

（同理，Go 侧用 goja/v8go 做同样的事只会更麻烦：goja 不完整支持现代 JS 特性，
v8go 仍需自备整套 DOM/图形 shim。）

---

## 15. 三种"纯"的边界（最终口径）

| 路线 | 定义 | 对 CF managed challenge | 参考目录实证 | 成本 |
|---|---|---|---|---|
| **纯算生成** | 不接触任何浏览器/JS，直接算出 token | ❌ 不可行（VM 每次重混淆 + 运行时绑定） | 无；botguard README 明确否掉 | — |
| **纯 HTTP + TLS 指纹** | curl_cffi 级：伪造 JA3/header 顺序 | ⚠️ 只能**回放**已获得的 cookie，不能自己拿 | ✅ `tls-impersonate.md` 技能 + 本报告 §11 实测 | 极低 |
| **纯 JS 引擎** | Node/goja + DOM shim，无 GUI | ❌ 12/22 关键能力缺失，指纹层断链 | 本次实验（§14.4） | 等于自建浏览器 |
| **浏览器收割** | Camoufox/CloakBrowser 跑真引擎 | ✅ **实测通过**（§10，2/2 站） | ✅ captcha-solver 全部 12 模块 | ≈700 MB/实例 |

**结论**：能纯算的部分（回放、header/TLS、加密链计算）我们已经在用且实测有效；
不能纯算的部分恰好是 CF 用来判定的那部分（运行时绑定的 VM + 硬件指纹采集）。
这不是"没找到方法"，是**挑战本身的设计就要求真实执行环境**。

*（本届结论已用本机实测与参考目录一手源码双重验证。）*

---

## 16. A/B 对比实测：纯算自建 vs Camoufox（用户要求，2026-10-31 三轮）

### 16.1 A 路线做了什么

**A1 — Node + jsdom + node-canvas + 自写 shim（约 400 行）**

补齐 9 项能力：`navigator.*`、`performance.timing`、WebGL/WebGL2（伪造自洽 Intel UHD 630，
含 60+ 常量、32 个扩展、`WEBGL_debug_renderer_info` 的 UNMASKED 字符串）、
AudioContext/OfflineAudioContext（确定性 xorshift 噪声）、Worker(stub)、
RTCPeerConnection（真实形态 SDP）、Notification、document.fonts、fetch 桥接。
WebGL 对象还套了一层万能 `Proxy`（未知方法返回 no-op、未知常量返回 0）。

实测输出：

```
首访 https://steamdb.info/ -> 403 bytes=10557
shim 已安装 9 项
cookie 名: [cf_chl_rc_m]          ← CF 的"挑战失败次数"记账 cookie
cf_clearance: ❌ 没有
_cf_chl_opt 键数: 25              ← 脚本确实执行到了运行时注入
错误 6 条（去重 5）:
  1. [Cloudflare Turnstile] Unhandled error: (intermediate value)[Sc][Sc] is not a function
  2. jsdomError: Uncaught [TypeError: ... is not a function]
  3. jsdomError: Not implemented: navigation to another Document   ← jsdom 天花板
  4. [Cloudflare Turnstile] Unhandled error: Cannot read properties of undefined (reading 'querySelector')
```

**A2 — Go + goja（纯 Go 的 JS 引擎，无 CGO）**

环境假体（window/document/navigator/location/screen/history/performance/crypto/atob/btoa）
+ 注入真实挑战页的 `_cf_chl_opt`（2270 字节）+ 执行 575321 字节混淆编排脚本：

```
脚本 575321 字节，_cf_chl_opt 定义 2270 字节
✅ 执行成功（67ms）
执行后 _cf_chl_opt 键数: 20     ← 跑完了，但没走到采集/提交阶段
```

**结论性发现**：goja 能**完整解析并执行** CF 的 575KB 现代混淆 JS（ES6+ 兼容性够用），
说明"Go 内嵌 JS 引擎"在语法层面完全可行；但执行完不代表过盾——它没有产生任何
提交请求，也没有 cf_clearance，因为我们没有真实 DOM/渲染/网络事件循环。

**A3 — Go + v8go（真 V8）**：本机不可行。
```
CGO_ENABLED=1（默认开）但 where gcc → 无 gcc
go get github.com/rogchap/v8go → TLS handshake timeout（goproxy 网络）
```
v8go 需要 CGO 工具链 + 预编译 V8 库（Windows 上要 MSYS2/MinGW 或 VS Build Tools）。
即便装上，它给的仍是"JS 引擎 + 无 DOM"，与 A2 面对同一堵墙——**换 V8 不改变结论**。

**A 路线依赖库可获得性实测**：

| 库 | 用途 | 结果 |
|---|---|---|
| `jsdom` + `node-canvas` | DOM + Canvas2D | ✅ 装好可用 |
| `node-web-audio-api` | 真实 AudioContext | ✅ 装好（8 包 3s） |
| `gl`（headless-gl） | 真实 WebGL | ❌ **node-gyp 编译失败**（Windows 缺 VS Build Tools），无真实 GL 后端 |
| `v8go` | Go 内嵌 V8 | ❌ 无 gcc |
| `goja` | Go 纯 JS 引擎 | ✅ 装好，67ms 跑通 575KB |

### 16.2 A/B 全量对比表

| 维度 | A1 Node+jsdom+shim | A2 Go+goja | A3 Go+v8go | **B Camoufox** |
|---|---|---|---|---|
| 能否解析执行 CF 脚本 | ✅（25 键） | ✅ 67ms（20 键） | 未测（装不上） | ✅ 原生 |
| WebGL | 伪造（自洽参数） | 无 | — | ✅ C++ 真实 |
| AudioContext | 伪造确定性 | 无 | — | ✅ 真实 |
| Canvas 2D | ✅ node-canvas | 无 | — | ✅ 真实 |
| 页面导航/重定向 | ❌ jsdom 不支持 | ❌ | — | ✅ |
| 布局/渲染 | ❌ | ❌ | — | ✅ |
| **拿到 cf_clearance** | ❌ | ❌ | — | ✅ **2/2** |
| CF 侧反应 | 下发 `cf_chl_rc_m`（记失败） | 无网络 | — | 正常放行 |
| 内存占用 | ≈200–400 MB | ≈50 MB | — | ≈700 MB |
| 端到端耗时 | —（未成功） | 67ms（仅执行） | — | **19.6s / 14.5s** |
| 依赖/部署 | jsdom+canvas+shim 400 行 | goja 1 个依赖 | gcc+V8 | 二进制 + Python/Node 包 |
| 维护风险 | CF 改版即重写 shim | 同左 | 同左 | 跟随 camoufox 上游 |

### 16.3 结论

1. **"纯算"能做的那一半已经做到极限**：CF 的 JS 编排脚本我们能在 Node 和 Go 里都跑起来，
   WebGL/Audio/WebRTC 的返回值也能伪造到自洽。但**过盾失败**，卡点不是算力，是
   *环境真实性*——CF 校验的是"这个环境像不像一个真的浏览器"，而 jsdom/goja 缺的
   导航、布局、渲染、真实 GL 渲染结果，恰好是最难伪造的部分。
2. **headless-gl 在 Windows 装不上**，意味着 A1 连"真实 WebGL 后端"都拿不到，只能伪造参数值；
   而 CF 会交叉校验渲染结果与参数是否匹配。
3 . **Go 内嵌 V8 不改变任何结论**：v8go 装上也只是多一个 JS 引擎，DOM/图形栈还是得自己写。
   真正内嵌 V8 的工程量 ≈ 自研一个无头浏览器，且每次 CF 改版重做。
4. **B 路线（Camoufox）是当前唯一拿到实证通过的方案**：19.6s / 14.5s、2/2 站点、
   Go 侧 `net/http` 回放 200。代价是 ≈700 MB 内存与一个外部进程。

**推荐**：生产用 B（camoufox sidecar），A 保留为"脚本分析工具"（goja 67ms 跑通这一点很适合做
CF 版本变更侦测——每周跑一次，脚本结构一变就告警，比等线上失败强）。

*本节所有数据均为本机实际运行输出，脚本位于 `D:\apk-analysis\cf-jsd-recon\{nodetest,gotest}`。*
