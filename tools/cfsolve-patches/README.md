# cfsolve 本地补丁集

对 [`chris124567/cfsolve`](https://github.com/chris124567/cfsolve) 的本地修改，解决两件事：
**① 让它在 Windows 上能编译/运行；② 让 capture-proxy 能对付"运行时构造"的新版 CF。**

- 上游版本：`master` @ 2026-10-29
- 补丁文件：`cfsolve-windows-and-capture-fixes.patch`（7 个文件，312 行）
- **不是上游支持的平台/行为**，仅供本地研究与验证

## 怎么应用

```bash
git clone https://github.com/chris124567/cfsolve
cd cfsolve
git apply /path/to/cfsolve-windows-and-capture-fixes.patch

# Windows 构建（需要 MSVC + CMake + NASM + libclang）
#   set LIBCLANG_PATH=C:\Python314\Lib\site-packages\clang\native   :: pip install libclang
#   set CMAKE_GENERATOR=Ninja                                        :: pip install ninja
#   在 vcvars64.bat 环境里跑，PATH 里加 NASM
cargo build --release --bin challenge
cargo build --release --bin capture-proxy
```

## 补丁都改了什么

### A. Windows 兼容（3 处）
| 文件 | 改动 |
|---|---|
| `Cargo.toml` | 加 `[target."cfg(windows)".dependencies] windows-sys(Win32_System_Performance)` |
| `src/performance_clock.rs` | `monotonic_us()` 拆成 `#[cfg(unix)]` 原实现 + `#[cfg(windows)]` 用 `QueryPerformanceCounter` |
| `src/capture/proxy.rs` (shutdown) | `shutdown_requested()` 的 unix 分支加 cfg，Windows 只等 `ctrl_c()` |

### B. 对付"运行时构造"的码表（`--alphabet`）
新版 CF 用 `String.fromCharCode` 在运行时拼出 base64 码表，静态扫描（`scan_alphabet`）找不到候选，
报 `expected one custom base64 alphabet, found 0`。

- `src/bin/capture-proxy/cli.rs`：新增 `--alphabet <64 字符串>`（校验 ASCII、64–66 长、前 64 位互不相同）
- `src/capture/proxy.rs`：`rewrite_iframe` 里有覆盖值时跳过静态扫描

### C. 对付"运行时构造"的 RSA modulus（`--modulus` + 运行时注入）
同一原因，`expected one 128-byte modulus in the iframe, found 0`。

- `src/challenge/program.rs`：新增进程级 `MODULUS_OVERRIDE` + `set_modulus_override()`；
  静态扫描失败时回落到覆盖值
- `src/capture/rsa.rs`：`swap_modulus` 增加第 4 个参数；**两条文本扫描都失败时**，
  改为往 iframe 里注入一段 `<script>` 包一层 `BigInt` —— 凡是用真实 modulus 构造的调用都返回
  代理的 modulus（效果与文本替换等价：浏览器用代理 key 加密，代理再用真实 modulus 转发）
- `src/bin/capture-proxy/cli.rs` / `main.rs`：新增 `--modulus <hex>` 并透传

### D. Windows 上可靠落盘（周期性 flush）
上游只在收到 Ctrl-C/SIGTERM 时才写 session 日志；Windows 上 Ctrl-C 事件投递不可靠
（`CTRL_BREAK` 会直接杀进程，`CTRL_C_EVENT` 常常送不到）。补丁在 accept 循环里加了 5 秒定时
`write_all`，**每个 session 一完成就写出去**。

## 实测效果（2026-11-01，本机）

| 阶段 | 结果 |
|---|---|
| 构建（Windows） | ✅ `challenge.exe` 49.6MB、`capture-proxy.exe` 48.1MB |
| B（码表） | ✅ 浏览器 hook `String.fromCharCode` + `Array.join` 抓到码表，跨三次加载**完全一致** |
| C（modulus） | ✅ hook `BigInt` 抓到 128 字节 modulus，跨两次加载一致；注入后日志出现 `injecting a BigInt shim` → `rewrote the modulus (session 0)` |
| 浏览器过盾 | ✅ Chrome 走 MITM 代理 **15–20s** 拿到 `cf_clearance` |
| session 落盘 | ✅ 3 个 session 目录都写出来了（`capture.json` / `original.html` / `rewritten.html` / `session.json`） |
| **提取完整会话** | ❌ **仍失败**：`extracting the session params: expected one cf-chl token, found 0` |

## 还差什么（第 3 个卡点）

`session.json` 里明确写着：

```json
{ "alphabet": null, "secret": null, "complete": false, "posts": [],
  "notes": ["extracting the session params: expected one cf-chl token, found 0"] }
```

也就是说 `capture-proxy` 还要从挑战流程里提取 **`cf-chl` token 与 secret**，这两样在新版 CF 里
同样是运行时生成/下发的，静态匹配拿不到。要继续补，就得把 CF 挑战协议的一部分在 capture 侧
重新实现一遍 —— 这已经等于把 `challenge`（求解器）里那套东西再做一份，属于**周级工作**，
不适合当作"补个工具链"。

**结论**：本补丁把"从 0 到能跑"这段打通了 2/3；剩下的 1/3 建议等上游适配，或按上面思路单独立项。

## 分析器（T-70 / T-71，Python 侧）

| 脚本 | 做什么 | 命令 |
|---|---|---|
| `analyze/primitive_gap.py` | deobf 脚本调用了哪些内建原语 / VM 实现了哪些 / 缺哪些 | `python tools/cfsolve-patches/analyze/primitive_gap.py <iframe.deobfuscated.js> [--vm-dir D:\cfsolve-upstream\src\vm] [--json --out x.json]` |
| `analyze/alias_shapes.py` | 计算属性 `obj[expr]` 的形态签名聚类 + 「是否可静态求值」实测 | `python tools/cfsolve-patches/analyze/alias_shapes.py <iframe.deobfuscated.js> [--out alias-shapes.json]` |
| `scripts/research.py gaps <corpus-id> [--json]` | 直接对 corpus 里的会话快照跑缺口清单（VM 目录取 `CFSOLVE_VM_DIR` 或 upstream/src/vm） | `python scripts/research.py gaps 2026-11-03-abcd1234` |
| `scripts/research.py diff <id-a> <id-b>` | 两份快照的五段 Markdown 差分报告，落 `plans/证据/research-diff-<a>-<b>.md` | `python scripts/research.py diff <a> <b>` |

### 口径（`静态确认`）

- **已实现清单来自 `builtins!` 注册表**（`src/vm/interpret/builtin.rs` 的 `BUILTINS`），正则提取，不手抄。
- `STRING_MEMBERS`（access.rs）是「识别但显式 unsupported」，`CONSTRUCTORS` / `STATIC_FUNCTIONS` 是「可解析但常返回 Symbolic」——**都不算实现**，故不纳入已实现清单（否则会把真缺口算成已实现）。
- 注册表目录缺失或提取到 0 条时**直接失败**（`RegistryError`），不输出空集。
- 注释与字符串里的属性名**不算调用**；同输入两次输出逐字节一致（无时间戳、键排序稳定）。

### 已知局限（**不是全自动适配**）

1. **字面扫描看不到运行时拼出的方法名**——新版 CF 的 computed-key 手法（`obj[["sli","ce"].join("")]` 之类）扫不出来。它消除的是 80% 的常见缺失，**不是**「自动适配新版本」。
2. 裸 `recv.method(` 的字面扫描**无法判定 receiver 类型**，一律归入口径 `*.prototype.method`：只要有任一原型实现了该方法名就算已实现（`String.prototype.slice` 在，就不会因为 `s.slice()` 报缺）。误报/漏报的风险在此，不在别处。
3. 不做数据流/别名传播、不解析 `eval`、不展开模板字面量里的代码。
4. `alias_shapes.py` 的「是否可静态求值」由一个**简化求值器**实测：只认字面量、`const/let/var` 声明的常量与常量对象、成员取值、`+` 连接；`CALL`/`FUNC` 一律判动态。它证明的是「按这套求值器算不出来」，不是「运行期一定算不出来」。
5. **Rust 侧未接入**：`properties.rs` / `expression.rs` 的白名单仍是硬编码；改成「读清单」属 L3（触及 11-03 刚打穿的核心路径），需独立 commit + 反向用例；`CFSOLVE_ALIAS_RULES=off` 开关也尚未实现。

