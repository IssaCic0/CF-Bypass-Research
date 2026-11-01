# v4.5 验收记录：compose 三 worker + 镜像瘦身 + 真机预算用尽 + service.py 拆分第一步

> 2026-11-02 03:0x · Docker 29.9.2(linux) · 镜像 `cf-bypass-solver:4.4.0`(4.63 GB) / `:slim`(3.27 GB)

---

## 1. `docker compose up -d`：3 个 worker 真起

| 检查 | 结果 |
|---|---|
| `docker compose up -d --build` | ✅ 3 容器全部 `Started` |
| 就绪耗时 | ✅ **20 s**（3/3 healthy；修复 fetch 位置后才可能这么快，之前单容器要 110 s） |
| `docker compose ps` | ✅ `cf-solver-1/2/3` 全部 `Up (healthy)` |
| 共享缓存 | ✅ 三个都报 `cache_backend=sqlite:/data/cache` |
| 共享 token | ✅ 三个 `ua_fingerprint` 全为 `64898c51c64b`（同一 persona + 同一 token 派生的同一密钥） |
| 内存 | ✅ 801 + 621 + 838 = **2.26 GB**（3 worker） |

### 跨容器去重（零预算，最硬的证据）

在**三个容器里**分别对同一个 key 抢 `claims`（共享卷 `/data`）：

```
container-1 try_claim -> True      ← 抢到
container-2 try_claim -> False     ← 被挡
container-3 try_claim -> False     ← 被挡
container-1 release   -> done
container-3 after release -> True  ← 释放后能再抢
```

⇒ "多个 worker 同时抢同一新域只解一次"在**真实多容器 + 共享卷**下成立。

---

## 2. 镜像瘦身：4.63 GB → **3.27 GB**（−1.36 GB），且已验活

大头分析：`/home/solver/.cache/camoufox` = **2.4 GB**，其中 **`fonts/` 2.1 GB**
（Camoufox 自带的字体包，用于字体枚举指纹的真实性；`fonts/M` 里的 CJK 字体就占 810 MB）。

新增 `--build-arg SLIM_FONTS`：

| 值 | 行为 | 实测 |
|---|---|---|
| `0`（默认） | 不删，保真优先 | 4.63 GB |
| `1` | 只删 CJK 字体包 `fonts/M` | **3.27 GB**（−1.36 GB），**已验活**：10 s 就绪、`browser_alive=true`、`/detect` 真出站 200 |
| `2` | 删全部字体 | 未构建（更小，但指纹真实性损失更大） |

### 两个只有动手才会遇到的坑（都写进了 Dockerfile 注释）

1. **删除必须与 `fetch` 在同一个 `RUN` 里**。单独写一个 `RUN rm -rf`，只是在新层加 whiteout，
   镜像**一点都不会变小**（实测：分两层时 slim 仍是 4.97 GB）。
2. **BuildKit 缓存会给你一个"没有浏览器"的镜像**：第一次限时构建时 fetch 层被缓存复用，
   镜像缩到 0.99 GB 但 `installed_verstr()` 直接抛 `CamoufoxNotInstalled`。
   ⇒ **瘦身必须验活**（`du` + `installed_verstr()` + 起容器打 `/health`），不能只看体积数字。

---

## 3. 剩余真机预算（4 次，累计 60/60 用完）

| 站点 | 探测 | 决策 | 结果 |
|---|---|---|---|
| patreon.com | 未命中防护 | **skip**（不求解） | ✅ **1.2 s** 返回 `no_challenge=true`（旧行为会去跑 20 s 浏览器） |
| epicgames.com | Cloudflare / managed | solve | ✅ **8.7 s / 462 字节** |

回放侧：我用**完整 cookie_list**（`cf_clearance` + `__cf_bm`）重放 epicgames 仍 **403**。

**结论要谨慎**：这**不能**判成服务缺陷 —— 我的重放脚本是 Python `urllib`（OpenSSL TLS、
缺 `Sec-Fetch-*` 等 Firefox 导航头），而 Go 客户端会带上完整头部并用自己的连接池。
**已知限制**在这里显形：回放依赖"UA + 完整 cookie + 头部/TLS 一致性"，越严格的站点越挑。
预算已用尽（60/60），按 §15 纪律**不再打真机**；下一步若要验证这一点，应换出口后用
Go 客户端（`gocfsolve.Do`）而非脚本重放。

---

## 4. `service.py` 拆分：2452 → **2310** 行（−142，−5.8%）

只搬不改，拆出两个**独立能力**模块：

| 新模块 | 内容 | 为什么这两块先拆 |
|---|---|---|
| `solver/probe.py`（135 行） | `probe_protection` + 逐跳重定向守卫相关的 3 个常量 | 探测只发一次 GET、不碰浏览器/缓存/队列，是**独立能力**；且它是 SSRF 面的一部分，安全审查要能一眼看完 |
| `solver/auth.py`（77 行） | `load_or_create_token` / `_read_token_file` / `token_fingerprint` + 2 个常量 | 凭证入口，安全审查焦点；原逻辑（env→文件→原子生成）值得单独可测 |

`service.py` 侧用 `from .probe import ...` / `from .auth import ...` **原样再导出**，
所以 `svc.probe_protection` 这类既有引用（含测试里的 `monkeypatch.setattr`）全部照旧。

**为什么不继续拆**：剩下的都是 `Solver`（worker/队列/缓存/准入状态机）与 `Handler`（HTTP 状态机），
把它们切开等于**大重构**——与"严禁大幅重构"直接冲突，且这部分刚刚经历了三轮治理修复。
结论：本轮是**第一步**，余下的降为 P3 并记录理由，不做。
