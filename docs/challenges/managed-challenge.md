# 挑战类型：`managed challenge`（当前主流）

> 一句话：**卡点不是算力，是"环境真实性"。** 脚本我们能跑通，但拿不到 `cf_clearance` —— 因为 CF 校验的是"这个环境像不像一台真的浏览器"。

## 最短可行解法

用**在 C++ 实现层篡改指纹**的浏览器（Camoufox）求一次 clearance，然后交给 Go 回放：

```bash
# 1) 起求解服务（首次启动自动生成 token 写 ./.env）
python solver/service.py --port 8878

# 2) 一条命令验证「求解 → 回放 → 200」
cd gocfsolve && go run ./examples/endtoend -target https://steamdb.info/
```

四条**硬边界**（违反即失效）：同出口 IP · 同 UA · 约 30 分钟时效 · 按域隔离。

## 实测证据

| 路线 | 能拿到 `cf_clearance` | 实测 | 成本 |
|---|---|---|---|
| 纯算（离线算 token） | ❌ | 挑战 VM 每轮重混淆、绑运行时、在隐藏 realm 采集 | — |
| Node + jsdom + 自写 shim（A1） | ❌ | `_cf_chl_opt` 键数 25（确实执行到注入阶段），但仍报错、无 cookie | 自建浏览器 |
| Go + goja（A2） | ❌ | 575,321 字节脚本 67 ms 执行成功，`_cf_chl_opt` 键数 20 | 极低 |
| Go + v8go（A3） | ❌（未测成） | 本机无 gcc，`CGO_ENABLED=1` 不可用；即便可用仍是"JS 引擎无 DOM"，与 A2 同墙 | — |
| **Camoufox（B）** | ✅ **2/2** | steamdb 19.6 s / glassdoor 14.5 s | ≈700 MB/实例 |
| vanilla Playwright（对照） | ❌ 0/4 | headless 与 headed 均 403、25–32 s 不放行 | 400–550 MB |

细节与原始输出：`docs/Cloudflare过盾方案可行性侦察报告.md` §8–§11、`docs/最终方案与实现结果.md`。

单实例内存实测（进程树差量法）：空闲 191 MB → 1 个空白页 **403 MB** → 挑战页 **551 MB**。

## 踩的坑

1. **`navigator.webdriver` 抹掉没有用。** Playwright 即使抹了它，仍然被判自动化——CF 看的是更深的行为/指纹面，不是单个字段。
2. **这是个"静默型"挑战：没有可点的 checkbox。** 诊断结果：可见 iframe 0、checkbox 0、可见 button 0，只有 `challenges.cloudflare.com` 的 frame。指望"点一下验证框"的脚本在这里必然空转。
3. **goja/jsdom 跑完 ≠ 过盾。** 那条实验证明的是"语法层可执行 + 采集点可枚举"，不是环境真实。别拿它当"纯 js 能过盾"的证据。
4. **求解和回放必须同出口 IP。** 求解在 A IP、回放走 B IP（代理池/换网络）→ 静默 403，且最难排查。缓存键必须带出口标识。
5. **Camoufox 默认每次启动随机 OS persona。** 实测同一脚本三次分别拿到
   `X11; Linux x86_64` / `Macintosh; Intel Mac OS X 10.15` / `Windows NT 10.0` 三种 UA。
   随机 persona 会让 UA、时区、WebGL、宿主平台与出口 IP 的地理互相矛盾——而这些正是
   CF 判定"环境真实性"的输入。历史那次 2/2 通过恰好抽到 Windows。
   → 生产上建议用 `--browser-os windows`（或与出口 IP 地理一致的值）**固定** persona，
   但收益必须 A/B 实测后再定，不要凭直觉改默认值。
6. **别用固定 TTL 拍 dead line。** `cf_clearance` 的 cookie 自带 `expires`，以它为准（服务已按此实现，上限 2×配置 TTL）。

## 不可行的分支

- ❌ 用纯 JS 引擎（goja / v8 / jsdom）替代真浏览器 —— 已 A/B 实测证伪，换引擎不改变结论。
- ❌ 只抹 `navigator.webdriver` 的 stealth 补丁 —— Chrome 系打不过当前 managed challenge（本仓库 0/4）。
- ❌ 把"纯协议"写成注册失败路径的兜底 —— 对 managed 100% 无效，只会造出"以为能兜底、实际必然失败"的假能力。
- ⚠️ 上 uTLS 以为能解决 —— TLS 只是回放层的一个轴；没有 cookie 时，再像的 JA3 也没用（详见 `docs/challenges/turnstile.md` 末尾与报告 §11）。
