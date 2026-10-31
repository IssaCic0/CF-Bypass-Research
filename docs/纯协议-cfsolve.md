# 纯协议路线：接入 cfsolve（构建与使用）

> 这条路线**不需要浏览器进程**，用 [`chris124567/cfsolve`](https://github.com/chris124567/cfsolve) 的
> `challenge` 可执行文件完成求解。它跑得动 CF 的挑战脚本，靠的是**重放一份从真实浏览器录下来的
> 环境快照**（profile），而不是现场编造指纹。
>
> ✅ **本机已成功构建并跑通**（Windows，见下方"本机构建实录"）。
> ⚠️ 但用**作者自带的 safari profile** 成功率是 **0/4**（挑战页），因为那份 profile 是
> 作者那台 iPhone 的环境；要真正可用**必须自录 profile**，而自录需要一台能过盾的
> **Safari/Chromium**（cfsolve 不支持 Firefox profile）。
>
> 本仓库**不内置**二进制（体积 + 构建产物）。

## 零、本机构建实录（Windows，2026-11）

| 步骤 | 结果 |
|---|---|
| libclang | 缺 → 用 PyPI 包 `libclang`(18.1.1) 自带的 `libclang.dll`，设 `LIBCLANG_PATH` 指向 `site-packages/clang/native` |
| cmake | 3.28 不认识 “Visual Studio 18 2026” 生成器；升到 4.4.4 仍不认 → **改用 MSVC 开发环境 + Ninja**（`vcvars64.bat` + `CMAKE_GENERATOR=Ninja`） |
| NASM | BoringSSL 汇编必需 → `choco install nasm` |
| **源码 Unix-only** | `tokio::signal::unix`、`libc::clock_gettime` → **cfsolve 不支持 Windows**；本地打 2 处条件编译补丁后通过 |
| 结果 | `challenge.exe` 49.6 MB，构建 1m37s |

本地补丁（**仅本机副本，非上游支持**）：
1. `Cargo.toml` 加 `[target."cfg(windows)".dependencies] windows-sys(Win32_System_Performance)`
2. `src/performance_clock.rs`：`monotonic_us()` 拆成 `#[cfg(unix)]` 原实现 + `#[cfg(windows)]` 用 `QueryPerformanceCounter`
3. `src/capture/proxy.rs`：`shutdown_requested()` 的 unix 分支加 cfg，Windows 只等 `ctrl_c`

## 零点五、真机数据（自带 profile）

| 站点 | 耗时 | 结果 |
|---|---|---|
| steamdb.info | **3.2s** | 直接拿到**正常页 204KB**（无挑战）→ 无事可做 |
| glassdoor.com | **23.7s** | 跑了完整挑战流程，但 `clearance form set no cf_clearance cookie` |
| epicgames.com | 15.7s | 页面里没有 `.cf-turnstile` 容器 |
| quora.com | 6.7s | 同上 |
| udemy.com | 4.9s | 同上 |

**结论**：纯协议栈在 TLS/HTTP 层是通的（能拿到真实页面），但**自带 profile 与本机出口不匹配 → 挑战页 0/4**。

## 零点六、接进服务后的真机端到端（回落链）

```
POST /solve {"url":"https://steamdb.info/"}
→ attempts: [{"backend":"cfsolve","ok":false,"error_code":"unsolved"},
             {"backend":"camoufox","ok":true}]
→ backend=camoufox, elapsed=20.58s, clearance=463 字节
```

即：**纯协议先试 → 失败 → 浏览器接管并成功**，这条链在真机上跑通了。

## 一、构建（一次性，约 10–30 分钟）

前置：Rust(按 `rust-toolchain.toml`) + CMake + C/C++ 编译器 + **libclang** + Git。

```bash
git clone https://github.com/chris124567/cfsolve
cd cfsolve
cargo build --release --bin challenge     # 产物：target/release/challenge
cargo build --release --bin capture-proxy # 产物：target/release/capture-proxy（录 profile 用）
```

- Linux：`apt install cmake libclang-dev build-essential git`
- macOS：`brew install cmake llvm git`（并让 `LIBCLANG_PATH` 指向 llvm 的 lib）
- **Windows**：需要 MSVC + CMake + `libclang.dll`（LLVM 发行版自带），并设置
  `LIBCLANG_PATH=C:\Program Files\LLVM\bin`。本机就是因为缺这个才没跑通构建。

## 二、录制 profile（关键，也是这条路线唯一的"消耗品"）

仓库自带一个 `profiles/safari-26-ios.json`，但它对应的是**录制者那台设备/IP**。
要让成功率接近作者水平，原则上应当：

1. 起 `capture-proxy`；
2. 把**你自己的真实浏览器**（同一出口 IP、同一款浏览器）的代理指向它；
3. 在浏览器里手动过一次 CF 挑战；
4. 它会记录挑战脚本发出的质询与你的浏览器给出的应答 → 生成 profile 目录/JSON；
5. 用这份 profile 去跑 `challenge`。

> 直白说：**profile 决定成败**。用作者自带的 safari profile，等于冒充"作者那台 iPhone 的
> 出口环境"——一旦出口 IP 与它差异大，成功率就会掉。这是它最容易踩的坑，也是它必须
> 要求"回放同 IP + 同 UA"的原因。

## 二点五、本机尝试自录 profile（失败，记录在此避免重复劳动）

已构建 `capture-proxy.exe`（48MB，同上补丁），并尝试用 **CDP 驱动本机 Chrome** 走它的 MITM 代理录 profile：

```bash
capture-proxy --fingerprint profiles/safari-26-ios.json   --site https://www.glassdoor.com/ --listen 127.0.0.1:<free>   --out ./captures --ca-dir ./proxy-ca
# Chrome: --proxy-server=http://127.0.0.1:<free> --ignore-certificate-errors
```

结果：Chrome 在 **120 秒内始终停在挑战页**（title 一直是"请稍候"），`captures/` **0 字节** → 没录到任何质询应答。

原因（两条叠加）：
1. **glassdoor 对本机 IP 很硬**：同一台机器上 pydoll（Chrome 直连）也过不了它（48.99s 无 clearance）；
2. **MITM 会改变指纹链路**：Chrome 客户端 ↔ capture-proxy 之间是 Chrome 的 TLS，而 capture-proxy ↔ CF 之间用它 profile 里的 transport（Safari）重新发起 —— 客户端自称 Safari 但实际是 Chrome 的 JS 环境，**自相矛盾**，比直连更难通过。

**结论**：本机无法自录 profile。要拿到可用的 profile，最现实的是
**在真实手机 Safari / 真实桌面 Chrome 上人工过一次盾**（用作者说的手动流程），或在一台
"Chrome 能过盾"的机器上录。

## 三、本仓库怎么接

```bash
python solver/service.py \
  --cfsolve-bin /path/to/cfsolve/target/release/challenge \
  --cfsolve-profile /path/to/profile-or-profiles/safari-26-ios.json \
  --cfsolve-timeout 120
```

接上之后，后端优先级自动变成：

```
cfsolve（纯协议） → Camoufox（浏览器） → FlareSolverr（若配置）
```

- 纯协议命中 → 不启浏览器，耗时与内存都省下来；
- 纯协议失败（profile 过期 / Rejected / NotChallenged）→ **自动回落浏览器**，调用方无感；
- 响应里的 `attempts` 会记录每一跳的结果，便于判断"是纯协议失效了还是目标变严了"。

### 契约细节（我们这一侧的适配）

| 项 | 行为 |
|---|---|
| 输出解析 | stdout 首行：`cf_clearance=...` → 视为 clearance；否则视为 token |
| UA | 从 profile 的 `transport.headers['User-Agent']` 读取并回传（**回放必须用它**） |
| `want=turnstile/recaptcha_v3` | 追加 `--sitekey --blank-page [--action --cdata]`（cfsolve 本身支持 widget） |
| 错误映射 | `NotChallenged`/`Blocked`/`Rejected` → `unsolved`（附可执行 hint）；`Network` → `transport(502)`；超时 → `timeout(504)` |
| 临时目录 | 每次调用建一个临时目录作为它的输出目录，用完即删（它要求目录不存在或为空） |
| 缓存 | clearance 走我们的按域缓存（键含出口标识）；token 一律不缓存 |

## 四、什么时候别用它

- **目标站点已升级到需要交互的 managed/embedded 挑战**：纯协议没有鼠标，点不了验证框。
- **profile 与你的出口 IP 差异大**：成功率会掉，且失败是 `Rejected` 这种"看起来像被封"的错误。
- **你需要绝对稳定**：profile 是消耗品，CF 一改就得重录；浏览器路线没有这个维护负担。

## 五、本仓库的诚实披露

- 本机**未构建、未实测**（缺 libclang）。所以这条路线在我们的记录里是
  `needs_validation`，不是 `confirmed`。
- 接口层（参数拼装、输出解析、错误映射、UA 提取、临时目录清理）已有 11 项离线单测覆盖
  （`tests/test_strategies.py`），保证"接上就能跑"的部分是可靠的。
