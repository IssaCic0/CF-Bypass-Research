# 挑战类型：`Turnstile`（以及一个常见混淆）

> 一句话：**Turnstile 不是 `cf_clearance`。** 本仓库已在 v2.2 实现 Turnstile token 求解
> （`want=turnstile`），并有真机验证；但它产出的是一次性 token，**不能**当 clearance 缓存复用。

## 最短可行解法

```bash
curl -s -X POST http://127.0.0.1:8878/solve -H "Authorization: Bearer $TOKEN"   -H 'Content-Type: application/json'   -d '{"url":"https://target/","want":"turnstile","sitekey":"0xAAAA...","action":"login"}'
# → {"ok":true,"solved":true,"want":"turnstile","token":"...","token_field":"cf-turnstile-response",
#    "cacheable":false,"expires_in":300,...}
```

把 `token` 塞进目标页面的 `cf-turnstile-response` 字段（或目标站自己的回调）提交。

**实现要点（两条都是真机踩出来的）**
1. **必须在目标 origin 下渲染 widget**：token 绑 origin，换个域拿到的交上去会被拒。
   做法是 `route.fulfill` 拦截目标文档 URL、直接返回本地存根页。
2. **必须显式 `turnstile.render()`**：用 `api.js?render=explicit` 时**不会**自动渲染
   `.cf-turnstile` 元素；只放个 div 会白等到超时（第一版就是这么错的，真机 60s 拿不到 token）。
   现在用 `render=explicit` + 主动 render + callback 写全局变量，再轮询读取。

## 实测证据

| 项目 | 事实 |
|---|---|
| 本仓库实现 | 只有 `cf_clearance` 求解（`solver/service.py`），**无** Turnstile 路径 |
| 抽样 | 53 站中触发挑战的 6 站 `cType` 全部 `managed`，未见 Turnstile 类型的独立挑战 |
| 数据点 | 公开报告与参考项目普遍把两者分开处理：`cf-turnstile-token` 这类工具产出的是 `cf-turnstile-response`，不是 `cf_clearance`（`D:\参考项目\cf-turnstile-token\README.md`） |

## 踩的坑

1. **最大的坑是把两者当成同一个东西。** `cf_clearance` 是**站点级 cookie**（绑域+IP+UA，可缓存复用约 30 分钟）；`cf-turnstile-response` 是**一次性 widget token**（绑 action/cdata/页面会话）。拿后者当年底前者的"替代品"会直接走偏。
2. **别把"某工具 1 秒出 token"当成能力可迁移。** 那类工具多是把请求转发到付费 API（其 README 自己写明 `--proxy` 是为了"让求解与你的爬取 IP 一致"），本地没有任何可复用的求解实现。
3. **`sitekey` 与页面绑定。** 同一个站不同页面/不同 action 的 sitekey 可能不同，硬编码一个必然失败。

## 不可行的分支

- ❌ 用 `cf_clearance` 的求解流程去拿 Turnstile token（产物不同）。
- ❌ 把 Turnstile token 塞进 `Cookie` 头当 clearance 用。
- ❌ 在没有真实需求时先实现 Turnstile 支持——本仓库范围明确是"页面级 clearance + 回放"。

## 附：回放层的判断顺序（与 Turnstile 无关，但常被一起问）

当回放返回 403 时，按成本从低到高排查：

1. **cookie 是否过期**（看 `expires`，别拍 30 分钟）；
2. **UA 是否与求解时一致**（不一致必然失效）；
3. **出口 IP 是否变了**（求解与回放必须同出口）；
4. 以上都对，再用同 cookie + 同 UA 换三种 TLS 指纹交叉验证（本仓库实测 steamdb 三指纹均 200，说明该站不严查 JA3；**这是 n=1 的证据，不可外推**）；
5. 只有在"某一种指纹能过、其余不过"时，才说明是 TLS/HTTP2 指纹问题——此时兜底方案要同时覆盖 TLS 与 HTTP/2 两条轴，裸 uTLS 只覆盖前者。
