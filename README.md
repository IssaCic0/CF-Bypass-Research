# Cloudflare 反爬机制研究与过盾方案 A/B 实测

> 对 Cloudflare 挑战体系（jsd / managed challenge）做的实证研究：**纯协议能不能行、纯算能算到哪一步、浏览器方案贵在哪**。所有结论都来自本机真实运行输出，不是推断。

**一句话结论**：CF 的挑战脚本我们用**纯 JS 引擎（Node/jsdom 与 Go/goja）都能完整执行**，WebGL / Audio / WebRTC 的指纹返回值也能伪造到自洽；但**拿不到 `cf_clearance`**。卡点不是算力，是"环境真实性"——CF 校验的是这个环境像不像一台真的浏览器。目前唯一实测通过的路线是 C++ 层指纹篡改的 Camoufox。

---

## ✅ 最终方案（已实现，已端到端跑通）

**Camoufox 求解 sidecar + Go 原生 HTTP 回放。**

```
Go 网关(gocfsolve.Client) ──POST /solve──► solver/service.py (:8878, Camoufox)
          ◄── cf_clearance + user_agent ──┘
          │
          └─ net/http + 同 UA + 同 Cookie + 同出口 IP ──► 目标站 200
```

| 组件 | 路径 | 状态 |
|---|---|---|
| 求解服务（Camoufox，按域缓存，`/solve` `/health` `/stats`） | `solver/service.py` | ✅ 已实现并运行 |
| Go 客户端（缓存 + 提前续期 + `Do()` 自动回放） | `gocfsolve/cfsolve.go` | ✅ 已实现 |
| 端到端验证程序 | `gocfsolve/examples/endtoend/main.go` | ✅ 跑通 |

**真机实测结果**：

```
✅ 取得 cf_clearance（463 字节, UA=Firefox/156.0…, 求解 18.12s, cached=false）
📄 回放结果：status=200 长度=202117 挑战页=false 耗时=1.34s
🔁 二次调用：cached=true（不再启动浏览器）

跨进程命中服务端缓存：总耗时 0.00s
```

```bash
# 1) 起服务
pip install camoufox && python -m camoufox fetch
python solver/service.py --port 8878

# 2) 跑端到端
cd gocfsolve && go run ./examples/endtoend -target https://steamdb.info/
```

详细选型理由、部署方式、已知限制、踩坑记录 → **[docs/最终方案与实现结果.md](docs/最终方案与实现结果.md)**

---


---

## 📌 核心结论

| 路线 | 能否拿到 cf_clearance | 实测证据 | 成本 |
|---|---|---|---|
| 纯算：离线算出 token | ❌ | 挑战 VM 每轮重新混淆下发、绑运行时、在隐藏 realm 采集 | — |
| 纯 HTTP + TLS 指纹（curl_cffi 级） | ⚠️ 只能**回放**已有 cookie | 三种 TLS 指纹回放均返回 200 | 极低 |
| 纯 JS 引擎 + 自写 shim | ❌ | 脚本执行到运行时注入阶段，但仍报错、无 cookie | 自建浏览器 |
| **浏览器收割（Camoufox）** | ✅ **2/2 站点** | steamdb 19.6s / glassdoor 14.5s | ≈700 MB/实例 |

**能纯算的部分我们已经在用，而且有效**：cookie 回放、TLS 指纹、header 顺序、加密链计算。
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

**B｜Camoufox（对照组，唯一通过）**

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
  service.py            # ★ 最终方案：Camoufox 求解服务（POST /solve，按域缓存）
gocfsolve/
  cfsolve.go            # ★ 最终方案：Go 客户端（缓存 + 提前续期 + Do() 自动回放）
  examples/endtoend/    # 端到端验证程序（求解 → 回放 → 校验 200）
docs/
  最终方案与实现结果.md                 # ★ 选型理由 / 实测数据 / 部署 / 踩坑
  Cloudflare过盾方案可行性侦察报告.md   # 完整侦察报告（15 节，含全部原始输出）
scripts/
  analyze.py            # 静态分析 jsd main.js：字符串字面量 / 码表 / 关键字命中
  scan_ctype.py         # 50 站采样，统计 CF 挑战类型 cType 分布
  probe_browser.py      # Playwright headless / headed 过盾对照实验
  probe_camoufox.py     # Camoufox 过盾实验（当前唯一通过）
  diag_challenge.py     # 挑战页 DOM 诊断：iframe / checkbox / 按钮 / 截图
node-shim/
  shims.js              # 12 项浏览器能力伪造层（WebGL/Audio/WebRTC/Worker/fonts…）
  run2.js               # A1 主程序：jsdom + shims 跑 CF 挑战脚本
go-jseng/
  main.go               # A2：Go + goja 执行 575 KB 混淆编排脚本
go-replay/
  main.go               # Go 原生 net/http 回放 cf_clearance
```

---

## 🚀 复现

### 环境

- Python 3.11+（`playwright`、`camoufox`、`httpx`、`curl_cffi`、`psutil`）
- Node 18+（`jsdom`、`canvas`）
- Go 1.21+（`goja`）

```bash
pip install playwright camoufox httpx curl_cffi psutil
python -m playwright install chromium
cd node-shim && npm i jsdom canvas
```

### 跑实验

```bash
# 1) 静态分析（需先自行抓取目标站的 jsd main.js 存为 main_0_*.js）
python scripts/analyze.py

# 2) 挑战类型分布采样
python scripts/scan_ctype.py

# 3) 浏览器过盾对照（需要本机 Chromium）
CHROME_PATH=/path/to/chrome python scripts/probe_browser.py
CHROME_PATH=/path/to/chrome python scripts/probe_browser.py --headed

# 4) Camoufox（唯一通过的方案）
python scripts/probe_camoufox.py

# 5) A1：纯 JS 引擎 + 伪造层
cd node-shim && node run2.js

# 6) A2：Go + goja
CF_SCRIPT_PATH=/path/to/orchestrate_chl_page.js \
CF_OPT_PATH=/path/to/chl_opt.js \
go run go-jseng/main.go

# 7) 回放验证
CF_COOKIE_DIR=/path/to/dir go run go-replay/main.go   # 目录内需 cookie.txt / ua.txt
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

1. **生产用浏览器收割路线**：Camoufox（REST 服务）+ Go 原生回放，已实测贯通两端。
2. **别丢掉纯 JS 引擎**：goja 67ms 跑通 575 KB 混淆脚本，很适合做 **CF 版本变更探针**——
   定时抓脚本比对结构，一变就告警，比等线上注册失败强得多。成本 ≈50 MB / 一次 67ms。
3. **优先"绕开"而不是"过"**：很多场景换入口域名 / 上游 API 直连比过盾便宜一个数量级。
4. **TLS 指纹是回放层技术**，不是求解层：没有 cookie 时光有 JA3 也没用。

---

## ⚠️ 免责声明

本项目仅用于**安全研究与授权测试**：理解反爬机制、评估不同技术路线的工程成本。
所有请求均为普通 GET（不注册、不提交、不批量），样本取自公开可访问的挑战页。
请勿用于未授权的访问、绕过访问控制或任何违法用途。使用者自行承担全部责任。

## 📄 许可

MIT
