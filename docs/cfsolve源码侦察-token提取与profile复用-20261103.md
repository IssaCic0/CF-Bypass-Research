# cfsolve 上游源码侦察报告：token 提取与 profile 复用

> 日期：2026-11-03 · 对象：`chris124567/cfsolve` master@2026-10-29（`D:/cfsolve-upstream` depth-1）
> 起因：验证用户假设——"token 动态下发，搭桥传过来就行；profile 是否可复用可分发？"

## 一、结论（全部有 file:line 证据）

### 1. cf-chl token **不需要录制，也不需要桥**——challenge 每局现场提取

`src/solver.rs:137 solve()` → 自己导航抓挑战页 → `session.rs` → `InitialRequest::from_parsed`（`src/challenge/source.rs:51`）：

- `source.rs:280-297`：AST `Visit` 访问器，匹配 `setRequestHeader('cf-chl', <value>)` 的调用表达式，用 `expression::static_value` 解出**静态字符串 token**（含随机变量名变体，有单测 `finds_session_parameters_with_different_randomized_names`）
- `source.rs:60-70`：`exactly_one(tokens)` + 校验 `/fo/` POST 路径尾段与 token 一致 + origin 不变
- 前置链：`inline_javascript`（source.rs:297，lohtml 解析内联脚本，要求含 `_cf_chl_opt`）→ `deobfuscate`（src/deobfuscate.rs，44 行入口）→ AST 访问

**这就是为什么 challenge 用作者自带 safari profile 在本机 3/3 过了 glassdoor.co.uk——token 是每次现解的，profile 里根本没有 token。**

### 2. profile = 纯"环境壳"，可长期复用、可跨机分发

`src/browser.rs:35-51`：profile 必填段 = engine / transport / request headers / page & orchestrator timelines / resource timing / interaction / probe timings / navigator。**无任何会话参数字段**。

- `secret`/`modulus`：`program.rs:169-193`，从**实时页面**的初始 VM 程序解出（`installed::read` → key transform → `Codec::try_new`），不落 profile
- `clearance` 绑出口 IP 是**每次求解产物**的属性（CF 服务端签发），不是 profile 的属性
- 推论：一台机器录的 profile 可以分发给所有机器用；成功率取决于"profile 人设 ↔ 出口 IP 组合"（`Rejected` 的主因）

### 3. capture-proxy 自录失败根因（修正此前判断）

`src/capture/sessions.rs:338 extract_codec`：对 iframe **原始 HTML** 做同样的静态提取（`extract_codec(&original)`，sessions.rs:167）。报 `expected one cf-chl token, found 0` 的场景是 **MITM 已重写 iframe**（BigInt shim 注入改变了 AST 结构）——是**自录工具链问题**，不是求解路线问题。

修复方向（三选一，成本递增）：
1. capture 不重写 iframe，hook 只捞参数以 `--alphabet/--modulus` 传入（补丁已支持 CLI 覆盖）
2. 仅在重写副本上注入，保留 original 的干净副本供提取（上游 `Session.iframe` 本来就存 original/rewritten 两份——sessions.rs `write()` 写 original.html + rewritten.html，说明数据结构已备好）
3. CF 再改版导致静态提取失效时，才需要"真桥"（goja 现解 token）——**当前不需要**

### 4. 对本项目回落链的意义

- v5.0.0 回落链 `cfsolve → camoufox → flaresolverr` 中，cfsolve 是**全功能**求解器（token 现解 + VM 答题 + 真 TLS），profile 只需按出口 IP 池备几套人设
- 剩余工作从"自研 token 提取"降级为"**profile 资产管理**"：为每个出口 IP 池录/配一套环境壳，改版时更新 `challenge_page_answers`
- `--pure-only`（72MB）路线的高并发产能不受自录工具链阻塞

## 二、关键源码地图

```
src/
├── solver.rs            # solve/clear 入口，每 solve 独立 client+cookie jar（可并发）
├── browser.rs           # profile 加载与校验（缺字段报错点名）
├── session/             # 求解会话状态机
├── challenge/
│   ├── source.rs        # ★ inline_javascript + InitialRequest（cf-chl token 提取）
│   ├── program.rs       # ★ PreparedSession::try_new（encoder/key_transform/secret/modulus）
│   ├── codec.rs         # Codec（alphabet+modulus+secret 校验）
│   └── installed.rs     # 解码初始 VM 程序
├── deobfuscate/         # 反混淆（收敛循环）
├── capture/
│   ├── sessions.rs      # ★ extract_codec（自录侧同款提取）+ write(original/rewritten 两份)
│   └── proxy.rs         # MITM 代理（重写/rewrap）
├── vm/                  # JS VM
└── bin/{challenge,capture-proxy}/  # 两个 CLI
```

## 三、遗留待验证

- capture 侧"保留干净 original 提取"（方向 2）未实做——需改 proxy.rs 重写逻辑，估计几十行补丁
- profile 跨机分发后与不同出口 IP 的成功率矩阵未测（需真机预算，属 N1/N2 同类约束）
- 上游活跃度：master 2026-10-29，若跟进新版 CF 的静态提取适配，方向 3 可永久免除
