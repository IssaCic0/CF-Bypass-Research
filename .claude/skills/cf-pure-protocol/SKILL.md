---
name: cf-pure-protocol
description: CF 纯协议求解（cfsolve）操作手册——profile 录制/复用/分发、challenge 二进制构建、token 提取原理、自录工具链修复、与回落链集成。当需要纯算/纯协议过 Cloudflare、录制或复用 profile、诊断 cfsolve 报错时使用。
---

# CF 纯协议（cfsolve）技能

## 核心认知（侦察 2026-11-03 确证，读源码 src/ 得出）

1. **profile 里没有 token**。profile = 环境壳（`answers` 26 条 + `challenge_page_answers` 41 条）+ `transport`（TLS/时序/headers）。cf-chl token / secret / modulus 是 `challenge` 每次求解时**从实时页面现场提取**的（`src/challenge/source.rs` AST 静态分析 + 反混淆）。
2. **profile 可长期复用、可跨机分发**——绑出口 IP 的是 clearance（每次求解产物），不是 profile。但 profile 的"人设"（如 Safari-iOS）与出口 IP 组合影响成功率，`Rejected` 多半是这组合问题。
3. **求解链路**（`solver.rs`）：自己导航抓页 → `inline_javascript` 抽内联脚本 → `deobfuscate` 反混淆 → AST 找 `setRequestHeader('cf-chl', token)` → 解码初始 VM 程序 → 解出 secret/modulus → VM 答题 → 真 TLS 提交。
4. **capture-proxy 自录失败的根因**：MITM 重写 iframe（BigInt shim 注入）破坏了静态 AST 结构 → 提取 `expected one cf-chl token, found 0`。只影响自录场景，不影响求解。

## 环境准备（Windows 已验证路径）

```powershell
# 前置：Rust + MSVC(vcvars64) + Ninja + NASM + libclang
pip install libclang ninja; choco install nasm
set LIBCLANG_PATH=C:\Python314\Lib\site-packages\clang\native
set CMAKE_GENERATOR=Ninja
git clone https://github.com/chris124567/cfsolve; cd cfsolve
git apply <本项目>/tools/cfsolve-patches/cfsolve-windows-and-capture-fixes.patch
cargo build --release --bin challenge --bin capture-proxy
# 产物 ~50MB challenge.exe（含 wreq/BoringSSL TLS 栈）
```

## 自录 profile 正确姿势（绕开 AST 破坏）

1. **capture-proxy 不重写 iframe**：浏览器 hook 只需捞 `--alphabet`/`--modulus` 覆盖参数传入（补丁已支持），避免注入 shim。
2. 浏览器（需 Safari/Chromium 系，cfsolve 不支持 Firefox profile）走代理 → 手动过一次挑战 → Ctrl-C 落盘 sessions/。
3. `cargo run --example build_profile -- <session>/capture.json > answers.json` 生成环境壳。
4. `cargo run --example fill_profile` 可补齐 profile 要求的字段（engine/timings/navigator 等必填，缺字段报错会逐个点名）。

## 服务集成（本项目）

```powershell
python solver/service.py --cfsolve-bin D:/cfsolve/challenge.exe `
  --cfsolve-profile <profiles目录> --cfsolve-timeout 120
# 回落链自动变为: cfsolve(纯协议) → camoufox(浏览器) → flaresolverr
# --pure-only 模式: 72MB 内存无浏览器；want=turnstile 加 --sitekey --blank-page
```

## 常见报错速查

| 报错 | 根因 | 处理 |
|---|---|---|
| `page has no .cf-turnstile container` | 该站对此 persona 不弹挑战（非失败） | 换站或换 profile persona |
| `clearance form set no cf_clearance cookie` | 域名跳转（如 glassdoor.com→.it）后最后一步没拿到 | 用最终落地域（如 glassdoor.co.uk） |
| `expected one custom base64 alphabet, found 0` | 新版 CF 运行时构造码表 | 补丁 `--alphabet` 手动传入（hook String.fromCharCode 捞） |
| `expected one 128-byte modulus` | 同上 | 补丁 `--modulus`（hook BigInt 捞） |
| `expected one cf-chl token, found 0`（capture 时） | MITM 重写破坏 AST | 见"自录正确姿势"① |
| `Rejected` | profile 人设与出口 IP 组合可疑 | 换出口或重录匹配人设 |

## 与本项目的证据/文档

- 侦察与实测: `docs/纯协议-cfsolve.md`（§2.7 补丁实录、§2.8 结论修正）
- 补丁: `tools/cfsolve-patches/cfsolve-windows-and-capture-fixes.patch`（7 文件 312 行）
- 同站 A/B: cfsolve 18.43s < camoufox 19.56s；`--pure-only` 72MB；`plans/证据/ab-methods-*.json`
- 上游源码参考: `D:/cfsolve-upstream`（depth-1 clone，2026-10-29 master）
