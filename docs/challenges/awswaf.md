# 挑战类型：`AWS WAF`

> 一句话：**和 `cf_clearance` 同构**——真浏览器过盾后会下发 `aws-waf-token` cookie，
> 之后纯协议回放。本仓库用 `want=awswaf` 支持。

## 最短可行解法

```bash
curl -s -X POST http://127.0.0.1:8878/solve -H "Authorization: Bearer $TOKEN"   -H 'Content-Type: application/json'   -d '{"url":"https://target/","want":"awswaf"}'
# → {"ok":true,"want":"awswaf","aws_waf_token":"...","cookies":{...},"cacheable":true}
```

回放时把 `aws_waf_token`（连同同域其它 cookie）和 UA 一起带上，并且**必须同一出口 IP**。

## 实测证据

| 项 | 状态 |
|---|---|
| 实现 | ✅ `want=awswaf`，与 `clearance` 走同一条"等 cookie"路径 |
| 真机验证 | ❌ **未做**（V-1 的 50 请求预算已用在 clearance 上；没有现成的 AWS-WAF 目标站） |
| 离线测试 | ✅ `tests/test_challenges.py::test_awswaf_returns_cookie_and_is_cacheable` |

## 踩的坑

1. **别把它当 Turnstile**：AWS WAF 通常先下发一个 JS 挑战（403 + `aws-waf-token` 待生成），
   浏览器跑完 JS 才会 `Set-Cookie`。所以做法与 `cf_clearance` 一致：导航 → 轮询 cookie。
2. **CloudFront 的硬封锁不是 WAF 挑战**：如果页面标题/内容是 CloudFront 的封禁页，
   换多少浏览器都没用 —— 那是 IP 或账号级封禁，只能换出口。
3. cookie 名可能带后缀（不同 distribution 略不同），本实现按 `aws-waf-token` 精确匹配；
   若遇到变体需要放宽匹配规则。

## 不可行的分支

- ❌ 用 `cf_clearance` 的求解流程去等 `aws-waf-token`（cookie 名不同，会一直等到超时）。
- ❌ 只带 `aws-waf-token` 不带其它同域 cookie / 换 UA / 换出口 —— 会静默 403。
