# 录制 cfsolve profile 操作手册（给人看的）

> ⚠️ **2026-11-01 更新（重要）**：本机已实测，`capture-proxy` 对**当前 CF 版本**提取失败，
> 明确报错 `expected one custom base64 alphabet, found 0` —— CF 已不再下发静态码表（改运行时构造）。
> 也就是说：**这份手册现在大概率走不通**，保留下来供「CF 回退 / 上游修复后」再用。
> 详见 `docs/纯协议-cfsolve.md` 二点五节。下面的操作步骤本身是对的，卡点在工具与 CF 的版本错配。

> 目标：产出一份**与本机出口环境一致**的 `profiles/<name>.json`（+ 同名目录），让纯协议路线（cfsolve）真正可用。
> 全程约 5 分钟。

---

## 一、先说结论：**不需要 F12，也不需要登录任何账号**

很多人会以为要"用 F12 把 cookie 或某段 JS 抄出来"。**抄不出来，也没意义**，原因：

cfsolve 的 profile 不是 cookie，而是 **浏览器与 `challenges.cloudflare.com` 之间的多轮加密上报**。
我们实测看它的结构是：

```
profiles/safari-26-ios.json
├── engine: "webkit"
├── transport: { profile: "safari_ios_26.2", headers: { User-Agent: "…iPhone…Safari/604.1" } }
├── browser / timeline / intervals / orchestrator / interaction / resource_timing
├── answers: 26 条               ← 挑战脚本问"你的 WebGL/音频/时序是什么"，浏览器当时的回答
└── challenge_page_answers: 41 条 ← 挑战页那一轮的回答
```

这些内容在 F12 里看到的是**密文**（自定义 base64 + XOR + DEFLATE），抄下来既不可读也不可重放。
正确做法是让 **capture-proxy 在中间把明文录下来**。

**所以你要做的只有一件事：让一个真实浏览器走我机器上的 capture-proxy，把那次挑战过一次。**

---

## 二、用哪个浏览器、过哪个站

### 浏览器：**首选 iPhone 的 Safari**

原因很实际：cfsolve 目前只内置了 `safari_ios_26.2` 这一套 TLS 指纹。
- 用 **iPhone Safari** 录 → 客户端（Safari）与代理对 CF 自称的身份（Safari iOS）**一致** → 最可能成功；
- 用**桌面 Chrome** 录 → 客户端是 Chrome，而代理对 CF 自称 Safari iOS → **自相矛盾**，比直连更容易被拦（我们已经实测失败过一次）。

### 站点：按"客户端能过的概率"排序

| 优先 | 站点 | 说明 |
|---|---|---|
| ① | `https://www.udemy.com/` | 本机 Chrome 实测能过（88s），对 iPhone 应该更容易 |
| ② | `https://steamdb.info/` | 本机 Chrome 实测能过（61s） |
| ③ | `https://www.glassdoor.com/` | 对 IP 最硬（本机 Chrome 过不了），前两个都失败再试它 |

**怎么判断"正在被挑战"**：打开后页面显示 **"Just a moment…"** 或 **"请稍候…"**，且地址栏域名不带 `/cdn-cgi/`。
**判断"过了"**：页面变成正常内容（能看到真实网页），最多等 30–60 秒。

> ⚠️ 过盾期间**不要点任何东西**（这类静默挑战没有可点的验证框），等它自己走完。

---

## 三、操作步骤（我做一半，你做一半）

### 第 1 步：我准备代理（我做）

我会在**本机**（LAN IP `192.168.1.2`）上启动：

```bash
capture-proxy --fingerprint profiles/safari-26-ios.json \
              --site https://www.udemy.com/ \
              --listen 0.0.0.0:<PORT> \
              --out ./captures --ca-dir ./proxy-ca
```

然后给你两个值：**代理地址** `192.168.1.2:<PORT>`、**CA 下载地址** `http://proxy.ca/ssl`。

### 第 2 步：你手机设置代理 + 装 CA（你做）

**前提：手机和这台电脑连同一个 Wi-Fi。**

1. iPhone →「设置 → 无线局域网」→ 点当前 Wi-Fi 右边的 **ⓘ** → 拉到底「**配置代理**」→ **手动**
   - 服务器：`192.168.1.2`
   - 端口：`<PORT>`
   - 存储
2. **装 CA 证书**（HTTPS 才能被解密）：
   - Safari 打开 `http://proxy.ca/ssl` → 允许下载描述文件
   - 「设置 → 已下载描述文件」→ 安装 → 输入锁屏密码
   - **关键一步**：「设置 → 通用 → 关于本机 → **证书信任设置**」→ 把该证书的开关**打开**
3. Safari 打开 `https://www.udemy.com/`（或上面 ②③）→ 等它自己过挑战（看到真实页面即成功）
4. 成功后，**把代理关掉**（设置 → Wi-Fi → ⓘ → 配置代理 → 关闭），避免影响你日常上网

### 第 3 步：我验证（我做）

- 我看 `D:\cfsolve\captures\` 有没有写入（有内容 = 录到了）
- 我用新 profile 跑一次 `challenge --page <目标> --solve --fingerprint <新profile>`
- 成功 → 我接进服务，并把真机耗时/成功率补进 `docs/AB实测-三种过盾方法.md`

> 如果 captures 是空的，说明"挑战没走完"或"没真正被挑战"——换 ②/③ 再试。

---

## 四、把东西给我，能一直用吗？（限制，必须知道）

**不能当成永久资产。** 逐条说清楚：

| 项 | 事实 |
|---|---|
| **有效期** | 没有承诺。profile 记录的是"**那一次**挑战 CF 问了什么、你的浏览器怎么答的"。CF 会轮换质询集合与加密方式。本项目已实测过同类失效：2023 年的 jsd 纯协议在 2026 年**结构性失效** |
| **强绑三样** | ① 浏览器版本/UA（profile 里的 `transport.headers`）② 出口 IP ③ CF 当前挑战脚本版本。任一变化都可能失效 |
| **换 IP 就废** | 这是最脆的一条：手机在 Wi-Fi 下录的 profile，拿到办公室/别的网络用，成功率会掉 |
| **不是"一次录永久用"** | 作者自己也说 "Producing these files requires some manual (or AI) effort"，没给有效期 |
| **实测参考** | 作者自带的 safari-26-ios profile 在**我们这台机器**上挑战页 **0/3** —— 说明 profile 和环境强绑定 |
| **✅ 隐私上是安全的** | profile 里**不含**任何账号、cookie、登录态；只有浏览器属性与时序。你可以放心把文件给我 |

**正确心态**：把 profile 当**易耗缓存**——
- 用的时候监控成功率，成功率掉了就重录；
- 或者干脆保持现在的默认策略：**纯协议先试 → 失败自动回落 Camoufox**（已经跑通，20.58s/463B），profile 只是"能省一次浏览器"的优化，不是必需品。

---

## 五、如果手机不方便

| 备选 | 可行性 |
|---|---|
| 桌面 Chrome 走代理 | 可行但**成功率低**（transport 是 safari，指纹不自洽）；要试就得先让我把代理的 `--fingerprint` 换成 Chrome 类型，而 cfsolve 目前只内置 safari |
| 你在**另一台能过盾的机器**上自己录 | ✅ 推荐：我把 `capture-proxy.exe` 给你，步骤同上（把 `--listen` 改成本机回环，浏览器指 `127.0.0.1:<port>`），录完把 `profiles/<name>.json` + 同名目录发我 |
| 不录 | ✅ 就用现有策略（纯协议优先 + 浏览器兜底），也能跑，只是每次都付 16s 浏览器成本 |

---

## 六、失败排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 手机连不上代理 | 不同 Wi-Fi / 防火墙拦了 | 确认同一网段；临时允许 `capture-proxy.exe` 通过防火墙 |
| 打开 https 报证书错 | CA 没装或没**信任** | 重做第 2 步第 2、4 小点（信任开关最容易漏） |
| 页面一直"请稍候" | 该站对当前 IP 太硬 | 换 ②/③ |
| 页面直接正常显示 | 该站**没触发挑战** → 没东西可录 | 换一个会弹挑战的站 |
| captures 有文件但我这边解不出 | profile 与 transport 不匹配（比如你用 Chrome 录的） | 用 iPhone Safari 重录 |
