# Cloudflare 过盾求解服务——纯协议优先的 CF 凭证获取与回放

> 对 Cloudflare 挑战体系（jsd / managed challenge / Turnstile）的实证研究 + 生产级求解服务。
> 所有结论都来自本机真实运行输出，不是推断。

## 🎯 这个项目能给你什么

| 你要做什么 | 我们给你什么 | 实测效果 |
|---|---|---|
| 高频访问受 CF 保护的站 | 一次求解、30 分钟内**全部 <3ms 回放** | 热路径 5589 QPS / p99 2.93ms / 0 错 |
| 大量站点、不确定哪些有盾 | `route=auto` 自动探测：无盾直接过、有盾自动解 | 无盾站 0.55s 放行、**0 次求解** |
| 要 Turnstile 一次性 token | `want=turnstile`，**纯协议直解**（无浏览器进程） | 官方测试 sitekey 6–8s |
| 要 `cf_clearance` cookie 供任意 HTTP 客户端用 | `want=clearance` + Go 客户端自动回放 | 回放 200、1.3–1.7s |
| 服务高可用 | 多进程池 + 失败摘除 2s / 回归 28s + 403 自愈 | 摘除/回归真机演练通过 |
| 内存敏感环境 | `--pure-only`：**72MB 无浏览器进程**的纯协议模式 | 同站 A/B 18.43s 最快 |

**架构一句话**：纯协议 cfsolve（Rust，JS VM + 环境快照回放）打头 → pydoll → Camoufox 浏览器兜底
→ FlareSolverr，按域缓存 + `prewarm` 保活把 18–43s 的求解挪出请求路径。

---

## ✅ 最终方案（已实现，已端到端跑通，v5.0.0）

**多后端有序回落链：纯协议 cfsolve → pydoll → Camoufox → FlareSolverr + Go 原生 HTTP 回放。**

```
Go 网关(gocfsolve.Client/Pool) ──POST /solve──► solver/service.py (:8878)
          │                    route=auto: 探测 → 无盾站直接放行(不启任何后端)
          │                    有序链: cfsolve(纯协议) → pydoll → Camoufox → FlareSolverr
          ◄── cf_clearance + user_agent + attempts 轨迹 ──┘
          │
          └─ net/http + 同 UA + 同 Cookie + 同出口 IP ──► 目标站 200
             (缓存命中热路径: 零 Python 参与, 离线压测 5589 QPS / p99 2.93ms)
```

| 组件 | 路径 | 状态 |
|---|---|---|
| 求解服务（多后端回落链，按域缓存，`/` 状态页 `/solve` `/detect` `/health` `/stats` `/status` `/logs` `/metrics` `/shutdown`；机器可读规格见 `docs/openapi.json`） | `solver/service.py` | ✅ 已实现并运行 |
| **纯协议后端（cfsolve，Rust，无浏览器进程）** | `solver/cfsolve_backend.py` + `tools/cfsolve-patches/` | ✅ 同站 A/B 18.43s 最快（快于 Camoufox 19.56s） |
| pydoll 后端（零 WebDriver CDP 直连 Chrome） | `solver/pydoll_backend.py` | ✅ steamdb/udemy 通过 |
| Go 客户端（缓存 + 提前续期 + 403 自愈 + `Do()` 自动回放） | `gocfsolve/cfsolve.go` | ✅ 已实现 |
| 多进程池（按域一致性哈希 + 失败摘除/自动回归） | `gocfsolve/pool.go` + `scripts/run-pool.ps1` | ✅ 摘除 2s / 回归 28s 实测 |
| 端到端验证程序 | `gocfsolve/examples/endtoend/main.go` | ✅ 跑通 |

**真机实测结果**：

```
✅ 取得 cf_clearance（463 字节, UA=Firefox/156.0…, 求解 18.12s, cached=false）
📄 回放结果：status=200 长度=202117 挑战页=false 耗时=1.34s
🔁 二次调用：cached=true（不再启动浏览器）

跨进程命中服务端缓存：总耗时 0.00s
```

```bash
# 1) 一次性准备
pip install -r requirements.txt
python -m camoufox fetch

# 2) 起服务（首次启动自动生成 token 并写入 ./.env）
python solver/service.py --port 8878

# 3) 跑端到端（会自动读 ./.env 里的 token）
cd gocfsolve && go run ./examples/endtoend -target https://steamdb.info/
```

> **开箱走的是浏览器路径，不是纯协议。** 上面两条准备命令只装了 Camoufox（浏览器后端，
> 每次求解 16–20s、约 1.2GB 内存）。纯协议后端 `cfsolve` 是一个**需要单独构建的独立二进制**
> （Rust，约 50MB），不在 pip 依赖里 —— 想要"纯协议优先"的 1.3–1.7s 回放速度，必须按
> `docs/纯协议-cfsolve.md` 构建并把路径传给 `--cfsolve-bin`。
> 运行 `python scripts/doctor.py` 会明确告知当前是哪种形态：`⚠ 纯协议后端 cfsolve: 未配置`
> 表示当前只能用浏览器后端。

### 第 4 步：确认你成功了

只看这三行，不要靠"感觉跑通了"：

```
✅ 求解服务在线：map[browser:camoufox state:ready ...]
✅ 取得 cf_clearance（463 字节, UA=Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0…, 求解 18.12s, cached=false）
📄 回放结果：status=200 长度=202117 挑战页=false 耗时=1.34s
```

- 只看到第一行 → **只通了健康检查**，求解没跑。
- 前两行有、第三行失败 → **求解成功但回放失败**，先查"同出口 IP / 同 UA"。
- 三行齐 → 全链路通。

详细选型理由、部署方式、已知限制、踩坑记录 → **[docs/最终方案与实现结果.md](docs/最终方案与实现结果.md)**
接口契约（含错误码表） → **[docs/API.md](docs/API.md)**
按挑战类型拆分的排查手册 → **[docs/challenges/](docs/challenges/)**

---


---

## 📌 核心结论

| 路线 | 能否拿到 cf_clearance | 实测证据 | 成本 |
|---|---|---|---|
| 纯算：离线算出 token | ❌ | 挑战 VM 每轮重新混淆下发、绑运行时、在隐藏 realm 采集 | — |
| 纯 HTTP + TLS 指纹（curl_cffi 级） | ⚠️ 只能**回放**已有 cookie | 三种 TLS 指纹回放均返回 200 | 极低 |
| 纯 JS 引擎 + 自写 shim（现场编造环境） | ❌ | 脚本执行到运行时注入阶段，但仍报错、无 cookie（22 项能力缺 12 项） | 自建浏览器 |
| **纯协议 cfsolve（回放录制的真实环境快照）** | ✅ | 同站 A/B **18.43s 最快**；`--pure-only` 72MB 内存无浏览器 | 低（需一次 profile 录制） |
| **浏览器收割（Camoufox）** | ✅ **2/2 站点** | steamdb 19.6s / glassdoor 14.5s | ≈700 MB/实例 |

> **四条后端可切换**：纯协议 `cfsolve`（默认首选）/ pydoll / Camoufox（浏览器兜底）/ FlareSolverr。
> 选型、成本、许可证与实测数据见 **[docs/方案矩阵.md](docs/方案矩阵.md)**；
> 三个参考仓库的深度调研见 **[docs/参考仓库深度调研.md](docs/参考仓库深度调研.md)**；
> 纯协议的构建与 profile 录制见 **[docs/纯协议-cfsolve.md](docs/纯协议-cfsolve.md)**。

**当前支持四类产物**（`want` 参数区分；Turnstile 在**纯协议后端**同样支持——见下）：

| `want` | 产物 | 真机验证 |
|---|---|---|
| `clearance`（默认） | `cf_clearance` cookie | ✅ A 组 24/25 |
| `turnstile` | Turnstile 一次性 token | ✅ 官方测试 sitekey 通过（6–8s）；**纯协议同样通过**（见下） |
| `recaptcha_v3` | reCAPTCHA v3 一次性 token | ✅ Google 官方 demo 页通过（2297 字节 token，18.5s） |
| `awswaf` | `aws-waf-token` cookie | ⚠️ 仅离线测试（无 AWS-WAF 目标站） |

### 🎯 Turnstile token 的纯协议求解（cfsolve 原生，实测通过）

Turnstile widget 不需要完整挑战页流程——cfsolve 的 widget 模式在**空白页**渲染
指定 sitekey 的 widget、走完 Turnstile 自己的交互、拿一次性 token：

```bash
# 纯协议直解（无浏览器进程，72MB）：challenge.exe 原生参数
challenge.exe --page https://目标站/ <outdir> --solve     --sitekey 0x4AAAAAAADnPIDROrmt1Wwj --blank-page     --fingerprint safari-26-ios.json
# 服务化：POST /solve {"want":"turnstile","sitekey":"0x...","url":"..."}
#         → cfsolve 后端自动接线 --sitekey --blank-page [--action --cdata]
```

- ✅ **既有实证**：官方测试 sitekey（`1x00000000000000000000AA` 系列）6–8s 通过
- ⚠️ **前置条件（重要）**：出口 IP 必须干净。CF 对被提分的出口 IP 会在网络层注入
  挑战页，widget 还没渲染就死——2026-11-03 实测：连 CF 官方 demo 页
  （`demo.turnstile.workers.dev`，2.4KB 无保护静态页）都被注入 639KB 挑战页。
  这不是求解算法的问题，是网络层前置条件。出口被提分时：`--proxy` 换干净出口，
  或等 IP 信誉恢复。
- widget 的 `origin` 由 `--page` 的 URL 决定，sitekey 必须与该域匹配

**能纯算的部分我们已经在用，而且有效**：cookie 回放、TLS 指纹、加密链计算。
（注意：**header 顺序不在其中**——Go 标准库按字段名字典序写 header，无法复现浏览器的顺序；
`gocfsolve` 只保证补齐 Firefox 侧必有的头，不保证顺序。）
真正不能纯算的，恰好是 CF 用来做判定的那一层。

---

## 🔬 研究内容

### 1. 挑战体系演化（实证）

```
旧（2023–2024）  GET  /cdn-cgi/challenge-platform/scripts/jsd/main.js   ← 明文码表 + jsd_url_path
                POST /h/b/jsd/oneshot/{jsd_path}/{ray}                 ← 返回 cf_clearance

现状（2026）     GET  scripts/jsd/main.js → 302 → /h/g/scripts/jsd/{hash}/main.js
                main.js 只剩 3 个字符串字面量（'t' 'c' 'g'），码表运行时构造
                挑战执行体 = /h/g/orchestrate/chl_page/v1?ray=…（575 KB 自解压混淆 VM）
                取证面 = challenges.cloudflare.com + 硬件指纹多轮上报
```

- 50 站采样：触发 CF 挑战的 6 站 `cType` **全部是 `managed`**，`jsd` 命中 **0**。
- 旧 `jsd/oneshot` 端点仍存在但拒绝请求（`POST 400` / `GET 405`）。

### 2. A/B 实测：四条路线

**A1｜Node + jsdom + node-canvas + 自写 shim（约 400 行）**

补齐 9 项能力：`navigator.*`、`performance.timing`、WebGL/WebGL2（伪造自洽的 Intel UHD 630，
60+ 常量、32 个扩展、`UNMASKED_RENDERER`）、AudioContext（确定性 xorshift 噪声）、
Worker、RTCPeerConnection（真形态 SDP）、Notification、document.fonts、fetch 桥接。

```
shim 已安装 9 项
cookie 名: [cf_chl_rc_m]        ← CF 的"挑战失败次数"记账 cookie
cf_clearance: ❌ 没有
_cf_chl_opt 键数: 25            ← 脚本确实执行到了运行时注入阶段
错误: (intermediate value)[Sc][Sc] is not a function
     Not implemented: navigation to another Document   ← jsdom 天花板
     Cannot read properties of undefined (reading 'querySelector')
```

**A2｜Go + goja（纯 Go 的 JS 引擎，无 CGO）**

```
脚本 575321 字节，_cf_chl_opt 定义 2270 字节
✅ 执行成功（67ms）
执行后 _cf_chl_opt 键数: 20
```

> **这条值得单独记**：goja 把 575 KB 的现代混淆 JS **完整跑通了**。
> 说明"Go 内嵌 JS 引擎"在语法层完全够用——只是跑完不等于过盾。

**A3｜Go + v8go（真 V8）**：本机不可行（`CGO_ENABLED=1` 但无 gcc）。
即便装上，它给的仍是"JS 引擎 + 无 DOM"，与 A2 撞同一堵墙——**换 V8 不改变结论**。

**B｜Camoufox（浏览器对照组，通过；后被纯协议 cfsolve 在同站 A/B 中以 18.43s vs 19.56s 超越）**

| 目标 | 引擎 | 首访 | cf_clearance | 耗时 |
|---|---|---|---|---|
| steamdb.info | Playwright headless | 403 | ❌ | 31.8s |
| steamdb.info | Playwright headed | 403 | ❌ | 28.7s |
| **steamdb.info** | **Camoufox** | 403 | ✅ | **19.6s** |
| **glassdoor.com** | **Camoufox** | 403 | ✅ | **14.5s** |

差别原因：Camoufox 在 **C++ 实现层**篡改 `hardwareConcurrency` / WebGL renderer /
AudioContext / screen geometry / WebRTC；Playwright 抹 `navigator.webdriver` 只是 JS 层补丁。

### 3. 回放层：Go 原生就能用（重要）

用 Camoufox 取得的 cookie 交给 Go 1.26 原生 `net/http`：

```
https://steamdb.info/  →  status=200  len=201391  challenge_page=false  elapsed=7560ms
```

`curl_cffi` 交叉验证（同一 cookie、同 UA、换三种 TLS 指纹）：firefox / safari / chrome **全部 200**。

**结论**：这批站点没有严格校验 JA3，**Go 网关不需要 uTLS 就能回放**（uTLS 仅作兜底）。

**四条硬边界**（违反即失效）：同出口 IP、同 UA、约 30 分钟时效、按域隔离。

**v5.0.0 稳定性实测（90 分钟 soak，19 样本）**：全程 `ready`、0 错误、句柄 Δ=-51（T-45 基线 Δ≤+50 内）、RSS 701→383MB 回落（`plans/证据/v5-hit-rate-24h.jsonl.summary.json`）。完整 SLO 表见 `docs/项目规格.md` §6。

### 4. 依赖可获得性（都是实测装的结果）

| 库 | 用途 | 结果 |
|---|---|---|
| `jsdom` + `node-canvas` | DOM + Canvas2D | ✅ 可用 |
| `node-web-audio-api` | 真实 AudioContext | ✅ 可用 |
| `gl`（headless-gl） | 真实 WebGL | ❌ node-gyp 编译失败（缺 VS Build Tools） |
| `goja` | Go 纯 JS 引擎 | ✅ 67ms 跑通 575 KB |
| `v8go` | Go 内嵌 V8 | ❌ 无 gcc |

---

## 📁 目录结构

```
solver/
  service.py            # ★ 服务主体：HTTP 层 + Solver 状态机（v5.0 下沉后 2073 行）
  bootstrap.py          # CLI 装配（参数解析 + 组件装配 + 信号处理，v5.0 下沉）
  metrics.py            # Prometheus 文本渲染（v5.0 下沉，覆盖率 95%）
  request.py            # 请求解析与校验（D1 下沉：纯函数，52 项边界单测，覆盖率 99%）
  compat.py             # YesCaptcha 兼容层（createTask/getTaskResult；映射纯函数 + 任务注册表）
  statuspage.py         # 只读状态页（GET /，单文件 HTML+JS，零依赖零构建）
  errors.py             # 错误码与 SolveError（对外契约，HTTP 状态映射在此）
  cache.py              # 缓存层：SqliteStore(WAL，多进程共享) / FileStore / MemoryStore
  auth.py               # Bearer 鉴权 + token 首启自动生成（0600/指纹日志）
  probe.py              # route=auto 探测：无盾站识别 + 按域短 TTL 缓存
  route.py              # 后端有序链决策（skip/固定/auto 映射）
  backends.py           # 多后端接缝：BackendId 白名单 + 有序路由 + attempts 轨迹
  cfsolve_backend.py    # ★ 纯协议后端：cfsolve CLI 包装（无浏览器进程）
  pydoll_backend.py     # pydoll 后端（零 WebDriver CDP 直连 Chrome）
  engines.py            # 浏览器引擎：camoufox / cloakbrowser / patchright
  prewarm.py            # 保活：过期前主动续期，把贵操作挪出请求路径
  proxypool.py          # 代理池：按域一致性哈希粘性 + 评分 + 冷却退避
  challenges.py         # 挑战类型判据
  detect.py             # /detect 端点实现
gocfsolve/
  cfsolve.go            # ★ Go 客户端：缓存 + 提前续期 + 403 自愈 + Do() 自动回放
  pool.go               # 多 worker 池：按域一致性哈希 + 失败摘除/自动回归
  cfsolve_test.go       # Go 侧离线测试（httptest，不联网）
  revalidate_test.go    # 403 自愈回归（13 例）
  examples/endtoend/    # 端到端验证程序（求解 → 回放 → 校验 200）
tools/cfsolve-patches/  # cfsolve 的 Windows 构建补丁 + 新版 CF 适配补丁 + profile 录制工具
docs/cfsolve源码侦察-token提取与profile复用-20261103.md  # ★ token 现场提取原理 + profile 复用机制
scripts/
  run-pool.ps1          # 多 worker 启动（共享缓存目录 + 可选 prewarm）
  run_limited.py        # ★ Windows Job Object 内存上限运行器（--max-mb N，超限杀进程树）
  e2e_local.py          # 真进程 E2E（30 断言，离线）
  hot_update_drill.py   # 热更新演练（17 断言）
  soak.py               # 长跑采样（--resume 断点续采、--token）
  loadtest.py           # 离线压测三模式（hot/cold/share）
  doctor.py             # 环境自检（起一次服务打 /health）
  capture_sample.py     # CF 挑战样本采集（默认 dry-run；产出带 sha256 的 manifest）
  validate-findings.mjs # 校验 docs/findings.json（证据路径必须存在 + 定级规则）
  analyze.py            # 静态分析 jsd main.js：字符串字面量 / 码表 / 关键字命中
  scan_ctype.py         # 50 站采样，统计 CF 挑战类型 cType 分布
  probe_browser.py      # Playwright headless / headed 过盾对照实验
  probe_camoufox.py     # Camoufox 过盾实验
  diag_challenge.py     # 挑战页 DOM 诊断：iframe / checkbox / 按钮 / 截图
tests/                  # 542 项 Python 测试（注入假浏览器/假后端，离线）
docs/
  项目规格.md            # ★ 单一事实源：是什么/边界/承诺什么/不承诺什么
  API.md                # ★ 接口契约（参数/响应/错误码表）
  方案矩阵.md            # 四条后端选型对比
  纯协议-cfsolve.md     # ★ 纯协议路线：构建 + Windows 补丁 + profile 录制
  findings.json         # 研究结论的机器可读清单（带 verdict 与证据路径）
  challenges/           # 按挑战类型拆分的排查手册（jsd / managed / turnstile）
  最终方案与实现结果.md                 # 选型理由 / 实测数据 / 部署 / 踩坑
  部署-容器.md / 运维-压测与自检.md / 检测-防护识别.md / 对接-new-api.md
  Cloudflare过盾方案可行性侦察报告.md   # 完整侦察报告（15 节，含全部原始输出）
requirements.txt        # 精确锁版本的运行期依赖
requirements-dev.txt    # 开发/CI 依赖（pytest-cov）
run.ps1 / run.bat       # ★ 统一入口：doctor / test / go / smoke / e2e / bench / verify / stop（v4.0）
policy.md               # 质量门禁（覆盖率 80%、证据门禁、提交规则），写死数值
workflow_status.md      # 任务契约 / 验收表 / 验证日志 / 审查记录
test_ledger.md          # 测试台账：每个验收项对应哪些用例
evidence/               # 验收证据（真实命令 + 真实输出），一个任务卡一个 json
.github/workflows/ci.yml# CI：python(覆盖率门槛) + go(vet/test -race) + findings 三 job，全程离线
Dockerfile              # 容器镜像（camoufox 二进制在 build 阶段 fetch；3.54GB 含插件预热）
docker-compose.yml      # 多 worker + 共享缓存卷
plans/
  下一步改进指南*.md     # 施工图（任务卡 + 验收门禁），plans/证据/ 存真机证据
```

---

## 🚀 复现

### 环境

- Python 3.11+（版本见 `requirements.txt`，**已锁版本**）
- Go 1.21+（仅 `gocfsolve/` 客户端与测试需要）
- Node 18+（仅 `scripts/validate-findings.mjs` 需要）

```bash
pip install -r requirements.txt -r requirements-dev.txt   # 运行期 + 测试依赖（版本已锁）
python -m camoufox fetch                                  # 必需：拉 Camoufox 的 Firefox 二进制

# 装完先自检（会起一次服务做 /health 探活，20 秒内给结论）
python scripts/doctor.py
```

### 多进程部署（要吞吐就看这一节）

单进程只**串行**求解（Playwright sync API 绑定线程），所以「加吞吐」= 「加进程」。
关键是**共用同一个落盘缓存目录**，否则每个进程会各解一次同一个域，扩容等于白扩：

```powershell
# 起 4 个 worker（端口 8878..8881），共用一个缓存目录与同一个 token
pwsh scripts/run-pool.ps1 -Count 4 -CacheDir "$env:LOCALAPPDATA\cfsolve\cache"

# 可选：保活（在过期前主动续期，把贵操作挪出请求路径）
pwsh scripts/run-pool.ps1 -Count 4 -Prewarm "steamdb.info,glassdoor.com" -RenewBefore 300
```

Go 侧用 `cfsolve.NewPool` 接（按域一致性哈希 + 失败摘除/自动回归）：

```go
pool, err := cfsolve.NewPool(
    []string{"http://127.0.0.1:8878", "http://127.0.0.1:8879",
             "http://127.0.0.1:8880", "http://127.0.0.1:8881"},
    cfsolve.WithPoolToken(token),
    cfsolve.WithPoolMaxFailures(2),
    cfsolve.WithPoolDownDuration(30*time.Second),
)
defer pool.Close()
cl, err := pool.Get(ctx, target)      // 同域永远打到同一个 worker
```

### 一键入口（v4.0，推荐）

```powershell
.\run.ps1 doctor     # 环境自检：依赖/浏览器二进制/端口/缓存目录/token + 起一次服务打 /health
.\run.ps1 test       # Python 全量测试 + 覆盖率门槛（≥80%，低于门槛直接红）
.\run.ps1 go         # Go: go vet + go test -race
.\run.ps1 smoke      # 起服务 → /health → /detect → SSRF 守卫，全链路冒烟
.\run.ps1 e2e        # 真进程 E2E：鉴权/metrics/route=auto/审计/优雅退出（离线，约 17s）
.\run.ps1 bench      # 离线压测三模式（hot 回放吞吐 / cold 准入 / share 多进程缓存）
.\run.ps1 verify     # 提交前必跑：以上全部
.\run.ps1 stop       # 停掉占着端口的进程
```

`run.bat` 是同一套子命令的 cmd 包装。

### 跑测试（离线，不联网、不起浏览器）

```bash
python -m pytest tests/ -q                      # Python 侧（注入假浏览器）
python -m pytest tests/ -q --cov=solver --cov-fail-under=80   # 带覆盖率门槛
cd gocfsolve && go vet ./... && go test -race ./...           # Go 侧（httptest + 竞态检测）
node scripts/validate-findings.mjs              # 校验研究结论的证据链
python scripts/capture_sample.py --url https://<target>/ --out samples/x        # 试运行（不联网）
python scripts/capture_sample.py --url https://<target>/ --out samples/x --real # 真抓（受 --max-requests 预算约束）
```

### 压测与缓存共享演练（默认离线，不打真实站点）

```bash
python scripts/loadtest.py --mode hot   --workers 8 --duration 5   # 稳态回放吞吐（E2）
python scripts/loadtest.py --mode cold  --workers 16               # 冷路径准入 + 去重（E1/F2）
python scripts/loadtest.py --mode share --processes 4              # 多进程同域只解一次（G2/A3）
python scripts/loadtest.py --mode hot --dry-run                    # 只看计划
```

> **多进程去重（v4.4 起）**：先后到达的重复求解由缓存挡掉；**同时**抢同一个新域由
> `claims` 抢占挡掉（`SqliteStore.try_claim`，失败路径也会释放）。`share` 模式的 burst 阶段
> 就是量化这件事的地方：修复前 4 worker × 3 轮 = **12 次**求解，现在 = **3 次**（1 次/轮）。
>
> ⚠️ 仍未覆盖：**不同缓存目录 / 不同 token** 的 worker 之间没有任何互斥
> —— 那种部署下"扩容等于白扩"是配置问题，不是实现问题（用 compose 就不会踩）。

### 跑实验

```bash
# 1) 静态分析（需先自行抓取目标站的 jsd main.js 存为 main_0_*.js）
python scripts/analyze.py

# 2) 挑战类型分布采样
python scripts/scan_ctype.py

# 3) 浏览器过盾对照（需要本机 Chromium）
CHROME_PATH=/path/to/chrome python scripts/probe_browser.py
CHROME_PATH=/path/to/chrome python scripts/probe_browser.py --headed

# 4) Camoufox（浏览器兜底方案）
python scripts/probe_camoufox.py

# 5) A1/A2/A3 三轮"纯 JS 引擎"实验的**结论**保留在 docs/ 与 findings，
#    实现目录（node-shim / go-jseng / go-replay）已按 D-3 决策 `git rm`
#    —— 它们不在主链路上，留着只会让人误以为"还有第三条路可以跑"。
```

### 关于样本文件

仓库**不包含** Cloudflare 的混淆脚本样本（`main.js` / `orchestrate_chl_page.js`）——
它们是 CF 的版权代码且每次请求都不同。需要复现时请自行抓取：

```bash
# 抓 jsd main.js（跟随 302）
curl -L -A "<UA>" "https://<target>/cdn-cgi/challenge-platform/scripts/jsd/main.js"

# 从挑战页 HTML 里抠出 window._cf_chl_opt = {...}; 存为 chl_opt.js
```

---

## ⚙️ 求解延迟与吞吐（实测解剖）

求解延迟的构成与上限（源码级证据见 `docs/纯协议-cfsolve.md` §6）：

| 阶段 | 耗时 | 可否压缩 |
|---|---|---|
| 导航 + TLS 握手 ×N 跳 | 网络往返 | ❌ 物理下限 |
| 人类行为模拟（ready_delay 1000ms + click 250ms + timeline） | ~1.5–2s | ⚠️ 只能换"天生快"的真机 profile 人设 |
| **PoW 工作量证明**（SHA-256 转满 CF 下发的 `budget_ms`） | 大头，随 CF 负载浮动 | ❌ **转快 = 直接拒签**（真浏览器同样要转） |

**提速的正解在架构层，不在求解器内**（全部已实现）：

| 手段 | 效果 |
|---|---|
| `--prewarm` / `--prewarm-file` | 过期前主动续期，热门域请求路径永远 <3ms（11 项单测；目标域须与 persona 匹配，用 `/detect` 批量筛） |
| 多进程求解池 `run-pool.ps1` | 冷启动风暴吞吐 ×N |
| `route=auto` 预检测 | 无盾站 0.55s 放行、0 次求解（33 厂商特征） |
| 403 自愈 | clearance 失效自动重解一次，调用方无感 |

**长跑进程保护（必用）**：`python scripts/run_limited.py --max-mb 2048 -- <command>`
（Windows Job Object，超限自动杀整个进程树——求解器对个别 CF 变体可能内存失控，实测 2GB 上限正常拦截。）

## ⚠️ 当前已知状态（2026-11-03）

- CF 于当日下发**新版挑战脚本**（computed-property 字符串别名 + 运行时字段名解码）：
  - 已打穿：反混淆 → 46 字段 payload 构建 → POST 200（此前完全卡死），补丁见
    `tools/cfsolve-patches/cfsolve-newcf-computed-key-20261103.patch`，
    源码级分析见 `docs/cfsolve源码侦察-token提取与profile复用-20261103.md`
  - 剩余卡点：响应程序分析（stage2）对最新形状未收敛——**回落链无感**（自动落 Camoufox），
    等 CF 变体轮换或上游跟进后复测
  - counter/token/input 字段名解码算法已逆向并**单测固化**（`field_names` 模块，2 passed），
    变体窗口后一条命令验证

---

## 🧭 工具地图（研究期间筛出的可用资源）

| 类别 | 项目 | 定位 |
|---|---|---|
| 过盾引擎 | **camofox-browser** | Camoufox 的 REST 封装（:9377，Docker），本报告验证的求解服务 |
| | FlareSolverr | 老牌：Selenium + undetected-chromedriver |
| | stealth-browser-mcp | nodriver + CDP，MCP 形态 |
| | VirtualBrowser | Chromium 指纹浏览器，多环境隔离 |
| 自托管求解 | **captcha-solver** | 12 种风控（akamai/datadome/botguard/perimeterx/turnstile…），全部 harvest-only |
| | ohmycaptcha | 自托管 YesCaptcha 风格，19 种任务含 Turnstile |
| 协议参考 | riskbypass_demo | 12 种风控的协议实现参考（服务本身付费） |
| | CloudFlareInvisibleSolver | 纯协议 jsd 原型（本报告证伪对象的上游） |
| 邮箱 | cloudflare_temp_email | CF Workers 临时邮箱完整系统 |
| | Jinnrry__PMail | 极简自建邮件服务器 |

**行业定性参考**（来自 `captcha-solver/botguard/README.md`）：

> BotGuard tokens **cannot** be generated offline. The VM is **polymorphic**（每次重新混淆下发）、
> **runtime-bound**（私有时钟偏移 + per-run nonce + Welford 时序签名）、
> **iframe-realm bound**（采集在隐藏 `contentWindow.eval` 里跑）。

CF 的 `chl_page` 三者全中。这就是"离线纯算"走不通的机理说明。

---

## 💡 工程建议

1. **生产用多后端回落链**：纯协议 cfsolve（首选，72MB 无浏览器）→ pydoll → Camoufox（浏览器兜底）
   + Go 原生回放，已实测贯通两端。缓存命中热路径零 Python 参与。
2. **别丢掉纯 JS 引擎**：goja 67ms 跑通 575 KB 混淆脚本，很适合做 **CF 版本变更探针**——
   定时抓脚本比对结构，一变就告警，比等线上注册失败强得多。成本 ≈50 MB / 一次 67ms。
3. **优先"绕开"而不是"过"**：很多场景换入口域名 / 上游 API 直连比过盾便宜一个数量级。
4. **TLS 指纹是回放层技术**，不是求解层：没有 cookie 时光有 JA3 也没用。

---

## ⚠️ 免责声明

本项目仅用于**安全研究与授权测试**：理解反爬机制、评估不同技术路线的工程成本。
所有请求均为普通 GET（不注册、不提交、不批量），样本取自公开可访问的挑战页。
请勿用于未授权的访问、绕过访问控制或任何违法用途。使用者自行承担全部责任。

---

## ❓ FAQ

<details>
<summary><b>纯协议为什么不行？</b></summary>

分两层回答：
- **现场编造环境（jsdom 自写 shim / goja 裸跑）**：不行。CF 校验的不是算法，是**环境真实性**——
  隐藏 realm 里对 DOM/WebGL/Audio/WebRTC 的属性做交叉校验，纯 JS 假体给不出自洽答案
  （实测 22 项能力缺 12 项）。
- **纯协议 cfsolve（回放真实浏览器录制的环境快照）**：✅ **可行且已接入生产回落链**——
  同站 A/B 18.43s 最快（快于 Camoufox 19.56s），`--pure-only` 72MB 内存无浏览器进程。
  构建与 profile 录制见 [docs/纯协议-cfsolve.md](docs/纯协议-cfsolve.md)。

**补充关键事实（源码侦察确证）**：cf-chl token 等会话参数**不在 profile 里**——
`challenge` 每次求解时自己从实时页面提取；profile 只是可长期复用、可跨机分发的"环境壳"。
绑出口 IP 的是 clearance（每次求解产物），不是 profile。详见
[docs/cfsolve源码侦察-token提取与profile复用-20261103.md](docs/cfsolve源码侦察-token提取与profile复用-20261103.md)。

详见 [docs/challenges/managed-challenge.md](docs/challenges/managed-challenge.md)。
</details>

<details>
<summary><b>为什么必须在 C++ 实现层改指纹？</b></summary>

Playwright 抹 `navigator.webdriver` 只是在 JS 层打补丁，本仓库实测 4 次全失败（headless 与 headed 都 403）。
Camoufox 在 C++ 层篡改 `hardwareConcurrency`/WebGL renderer/AudioContext/screen/WebRTC，
是唯一实测 2/2 通过的路线。
</details>

<details>
<summary><b>cookie 能撑多久？</b></summary>

约 30 分钟量级，但**不要拍死**：服务已改为读 `cf_clearance` cookie 自带的 `expires`（真实 TTL，
上限为配置值的 2 倍），客户端再提前 5 分钟续期。
</details>

<details>
<summary><b>回放为什么必须同出口 IP、同 UA？</b></summary>

`cf_clearance` 是与「域 + 出口 IP + UA」绑定的凭证。换任意一项，服务端会静默返回 403 ——
这是最难排查的一类故障。所以求解服务的缓存键里带“出口标识”，`gocfsolve` 也把 UA 一起校验。
</details>

<details>
<summary><b>要多少内存？</b></summary>

单实例实测：Camoufox 空闲 191 MB → 打开 1 个空白页 403 MB → 打开挑战页 551 MB。
本仓库按 **≈700 MB/实例** 规划；单进程串行求解（Playwright sync API 绑定线程），要提吞吐就多进程。
</details>

<details>
<summary><b>能保证过任何站点吗？</b></summary>

**不能。** 本项目证明的是"当前这批 managed challenge 站点、用这条路线、在本机实测可以通过"。
CF 一旦上新的指纹面，Camoufox 需要跟随上游升级；深保护站点（JSVMP / WASM 自带密钥）依然很难。
把这里的能力当成银弹会直接踩坑。
</details>

<details>
<summary><b>为什么求解服务要鉴权？只监听 127.0.0.1 不够吗？</b></summary>

不够。它会把调用方给的 URL 用真浏览器打开，是天然的 SSRF 跳板；而且单 worker 意味着
一个恶意请求就能把服务占满。现在：除 `/health` 外全部要求 Bearer token，
并对目标做私网/回环地址拦截，同时有并发与队列上限（满了立刻 429，不进无界队列）。
</details>

<details>
<summary><b>怎么快速判断是不是服务坏了，还是目标变严了？</b></summary>

```bash
curl -s http://127.0.0.1:8878/health | jq '.state, .last_error'
```
`state: ready` → 服务正常，问题在目标侧（换一个已知能过的站验证）。
`state: degraded` → 浏览器挂了，看 `last_error`；worker 会指数退避自愈。
</details>


## 📄 许可

MIT
