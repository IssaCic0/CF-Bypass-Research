# A/B 实测：三种过盾方法的速度与成功率

> 实测时间 2026-11-01，本机（Windows，同一出口 IP），3 个站点 × 3 种方法，**每格只打一次**
> （失败不重试，避免污染结论）。原始数据：`plans/证据/ab-methods-20261101-030208.json`。

## 一、结果表（按「平均成功耗时」升序）

| 排名 | 方法 | 类型 | 成功率 | 平均成功耗时 | 耗时区间 | 单次最慢 |
|---|---|---|---|---|---|---|
| 🥇 1 | **Camoufox** | 浏览器（Firefox，C++ 层改指纹） | **3/3 = 100%** | **18.74 s** | 15.87 – 21.12 s | 21.12 s |
| 🥈 2 | **pydoll** | 浏览器（Chromium，CDP 直连、零 WebDriver） | 2/3 = 66.7% | 74.52 s | 60.91 – 88.12 s | 88.12 s |
| 🥉 3 | **cfsolve** | **纯协议**（Rust CLI，无浏览器进程） | **0/3 = 0%** | — | 1.73 – 19.97 s（失败） | 19.97 s |
| — | CloakBrowser | 浏览器（Chromium，87 处 C++ 补丁） | 未测 | — | — | — |
| — | patchright | 浏览器（Chromium 反检测 fork） | 未测 | — | — | — |
| — | FlareSolverr | 浏览器（外部 sidecar，Selenium 系） | 未测 | — | — | — |

> **cfsolve 那行别误读**：它 1.7–20s 就返回，但**是失败返回**（`page has no .cf-turnstile container` /
> `clearance form set no cf_clearance cookie`）。它快是因为"没解出来就退出"，不是"解得快"。

## 二、逐站点明细

| 站点 | Camoufox | pydoll | cfsolve |
|---|---|---|---|
| steamdb.info | ✅ 21.12 s | ✅ 60.91 s | ❌ 3.07 s（page has no .cf-turnstile） |
| glassdoor.com | ✅ 19.23 s | ❌ 48.99 s（未取得 cf_clearance） | ❌ 19.97 s（未拿到 clearance） |
| udemy.com | ✅ 15.87 s | ✅ 88.12 s | ❌ 1.73 s（page has no .cf-turnstile） |

**观察**：
- Camoufox 三站全过，且**最快**（15.9–21.1s）—— 与本仓库历史 V-1 数据（中位 16.3s，96%）一致。
- pydoll 能过 2/3，但**慢 4 倍**（60–88s）。原因：它是"真 Chrome 从零启动 + pydoll 内建 CF 求解器轮询"，
  没有 Camoufox 那种"实例复用 + 特定指纹面"的优化。
- cfsolve 在**自带 safari-iOS profile** 下 0/3：profile 是作者那台 iPhone 的环境，与我们的出口/环境不匹配。
  它的价值要等**自录 profile** 才能体现（见 §五）。

## 三、每种方法的机制、前提、优缺点

### 1. Camoufox（当前最优，本仓库默认）

- **类型**：浏览器 / Firefox / **C++ 实现层**改指纹（WebGL、Audio、hardwareConcurrency、screen、WebRTC…）
- **需要什么**：`pip install camoufox && python -m camoufox fetch`（≈3.4 GB 二进制），≈1.2 GB 内存/实例
- **优点**：三站最快且全过；指纹改动在编译层，JS 层无补丁痕迹；免费、可离线分发；实例可复用
- **缺点**：内存重（1.2 GB/实例）；Firefox 系；跟 CF 对抗需跟随上游升级；默认随机 OS persona（要用 `--browser-os` 固定）
- **适用**：生产主力；要稳定、可接受 16s 冷求解与常驻内存

### 2. pydoll（CDP 直连，值得作为"第二意见"）

- **类型**：浏览器 / Chromium（本机真 Chrome 156）/ **CDP 直连，零 WebDriver**
- **需要什么**：`pip install pydoll-python` + 本机 Chrome（≈0 额外磁盘，用系统 Chrome）
- **优点**：不加载 chromedriver、无 `navigator.webdriver` 补丁痕迹；自带 `expect_cloudflare_turnstile()`；
  可直接复用**用户本机 Chrome**（带真实历史 profile 时更像真人）；安装成本最低
- **缺点**：**慢 4 倍**（60–88s）；headless 下 UA 带 `HeadlessChrome`（强检测信号，必须非 headless）；
  2/3 成功率；每次冷启动 Chrome
- **适用**：作为 Camoufox 之外的第二引擎/兜底；或"想用本机真 Chrome"的场景

### 3. cfsolve（纯协议，潜力最大但要自录 profile）

- **类型**：**纯协议** / Rust CLI / 无浏览器进程 / 自带 JS 虚拟机 + 重放录制的环境快照
- **需要什么**：Rust + CMake + **libclang** + **NASM** 构建（Windows 还要打 2 处 cfg 补丁）；一份 profile
- **优点**：无浏览器进程（内存/启动成本最低）；作者未给耗时承诺，但架构上是"毫秒级 VM + 网络往返"；
  与本仓库后端链已打通（`--cfsolve-bin/--cfsolve-profile`）
- **缺点**：**profile 是消耗品**（CF 改版要重录）；只支持 Safari/Chromium 类型的 profile；
  自带 profile 与本机环境不匹配 → 0/3；**不支持 Windows（需本地补丁）**
- **适用**：Linux/macOS 部署 + 能自录 profile 的场景；或用它做"先试纯协议、失败再上浏览器"的第一跳

### 4. CloakBrowser / patchright / FlareSolverr（未测，供参考）

| 方法 | 类型 | 为什么没测 |
|---|---|---|
| CloakBrowser | 浏览器（Chromium，87 处 C++ 补丁） | 二进制专有、最新版需订阅，且不可随仓库分发 |
| patchright | 浏览器（Chromium 反检测 fork） | 本机未安装；历史 V-1 里同类 Playwright 系实测 0/4 |
| FlareSolverr | 外部 sidecar（Selenium/UC） | 需自托管实例；上游已进维护模式 |

## 四、怎么选（决策表）

| 你的情况 | 选它 |
|---|---|
| 要**最快最稳**、能接受 1.2 GB/实例 | **Camoufox**（默认） |
| 想用**本机真 Chrome**、或要第二引擎兜底 | pydoll |
| 部署在 Linux/macOS、能录 profile、要**最省资源** | cfsolve（纯协议） |
| 有多台机器、要吞吐 | Camoufox 多进程 + `NewPool`（`scripts/run-pool.ps1`） |

**当前生产建议**（本仓库默认就支持）：

```
后端优先级：cfsolve（纯协议，若配了 profile） → pydoll（可选） → Camoufox（主力） → FlareSolverr（兜底）
```

## 五、要让纯协议真正可用，还差什么

cfsolve 的成败**全在 profile**：

1. 需要一个 **Safari 或 Chrome(ium)** 浏览器（cfsolve 只支持这两类 profile）；
2. 用它的 `capture-proxy` 把你**真实浏览器过盾的过程**录下来（代理 → 手动过一次盾 → 生成 profile）；
3. profile 必须与你**实际出口 IP / UA** 一致，否则就是本次实测的 0/3。

本机能过盾的是 Camoufox（Firefox）→ profile 类型不匹配；所以纯协议这条路在本机**卡在这一步**
（详见 `docs/纯协议-cfsolve.md`）。

## 六、复现

```powershell
# 环境变量指向已构建的 cfsolve 与本机 Chrome
$env:CFSOLVE_BIN="D:\cfsolve\challenge.exe"
$env:CFSOLVE_PROFILE="D:\cfsolve\profiles\safari-26-ios.json"
$env:CHROME_PATH="C:\Program Files\Google\Chrome\Application\chrome.exe"

python scripts/ab_test.py --sites "https://steamdb.info/,https://www.glassdoor.com/,https://www.udemy.com/" --gap 5
```
