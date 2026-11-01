# 挑战类型：`jsd`（旧非交互式检测）

> 一句话：**2023–2024 那套纯协议路子，在 2026 年已经结构性失效。** 这不是"补个补丁"的问题。

## 最短可行解法

**当前没有可行解。** 唯一"最短"的做法是：先判断目标是不是 jsd 类型（见下），如果不是就直接放弃这条路线。

判断方法（1 次 HTTP）：

```bash
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
  -A "$UA" "https://<target>/cdn-cgi/challenge-platform/scripts/jsd/main.js"
# 302 → 说明走的是新编排（h/g），纯协议路线不适用
# 200 → 可能是老站，值得进一步分析（样本里没遇到）
```

## 实测证据

| 观察 | 结果 | 来源 |
|---|---|---|
| `GET .../scripts/jsd/main.js` | **302** → `/h/g/scripts/jsd/{hash}/main.js?` | `docs/Cloudflare过盾方案可行性侦察报告.md` §2.1 |
| 当前 main.js 静态分析 | 42–44 KB，**字符串字面量只有 3 个**（`'t'` `'c'` `'g'`）；长度 48–96 的码表候选 **0**；`oneshot`/`jsd/` 关键字 **0** | 同上 §2.2 |
| 旧 `oneshot` 端点 | `POST` → **400**（空 body、无 `Set-Cookie`）；`GET` → **405** | 同上 §2.3 |
| 53 站采样 | 触发 CF 挑战 6 站，`cType` **全部 `managed`**；`jsd` 命中 **0** | 同上 §2.4 |

结论：**所有字符串都在运行时由 `charCodeAt`/`fromCharCode`/`atob` 拼出**，"正则抠 base64 码表"这条路彻底没有落点。

## 踩的坑

1. **盲目跟随 302 也不算成功。** 拿到新 main.js 后你以为可以照旧提取码表——但码表已经不在了；正则抓不到时很多实现会静默回落到标准 base64 表，结果服务端只回 400，看起来像"网络问题"。（`D:\参考项目\CloudFlareInvisibleSolver\test.py:203` 就是这种写法：正则失败 → `alphabet or "ABCDEFGHIJ..."` → XOR keystream 与字符集同时错。）
2. **别把 `h/b` 和 `h/g` 搞混。** 路径前缀从 `h/b` 变成 `h/g`（对应 `cFPWv: 'g'`）说明挑战平台版本已升级。
3. **"端点还在"不等于"能用"。** `oneshot` 返回 400 而不是 404——它在服务端还在，但当前 ray 对应的挑战类型不是 `jsd`，且缺前置握手。

## 不可行的分支

- ❌ 正则提取 `custom_base64_table` —— 码表已改为运行时构造。
- ❌ 复现旧 `oneshot` 帧格式 —— 缺前置握手，400 拒绝。
- ❌ 用 `managed` 站的 ray 去打 `jsd/oneshot` —— 挑战类型不匹配。
- ❌ 专找仍是 jsd 的老站 —— 抽样 0/53，即使找到也是长尾，覆盖不了业务目标。
- ⚠️ 反混淆 575 KB 编排 VM 并重放 —— 周级起步、CF 一更新即失效，投入产出比最差。
