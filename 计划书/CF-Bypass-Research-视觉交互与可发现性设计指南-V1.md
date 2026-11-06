# CF-Bypass-Research 视觉交互与可发现性设计指南（V1）

> 文档类型：施工级设计指南（视觉体系 / 动效 / 3D / 可发现性 / 安全 / 验收）
> 编写日期：2026-11-06
> 代码基线：`master` @ `8acd9c6`，版本 `6.3.0`（`docs/facts.json`）
> 文档位置：`计划书/CF-Bypass-Research-视觉交互与可发现性设计指南-V1.md`
> 实施状态：V1.1（2026-11-06）已落地并验证；部署按用户选择停在本地（未启用 Pages）。
> 证据口径：**已验证**（实际运行观察）/ **静态确认**（读代码确认）/ **合理推断**（有间接证据）/ **待验证**（缺环境或权限）

与既有文档的关系：

| 文档 | 管什么 | 本文件与它的边界 |
|---|---|---|
| `plans/下一步改进指南.md` | 工程与可靠性施工图（v5.0.1 → v6.0.0） | 仍在生效。本文件不改后端语义、不重开可靠性工作、不动 Python 测试的行为契约（2026-11-06 全量 1021 passed / 7 skipped） |
| `参考的结果计划指南.md` | 参考项目对标、差距审计、渐进改造 | 仍在生效。本文件不重复对标结论，只补它没有覆盖的"公开产品面" |
| `docs/项目规格.md` | 系统是什么（单一事实源） | 本文件引用它，不替代它 |
| `计划书/`（本目录） | 视觉、交互、可发现性的施工图 | 此前不存在 UI 专项计划；本文件是 V1，不与上述文档争夺事实源 |

---

## 0. 一页摘要

### 0.1 这份文档补的是什么

现状是"能用的操作台 + 能读的 README"，不是"可被发现的公开产品"：

- 两个页面（`GET /` 状态页、`GET /console` 只读控制台）是**单文件内联 HTML + CSS + JS**，零构建、零新依赖，功能真实，24 个既有单测（Phase F 后 34）+ 5 状态真 Chrome 验收锁死安全不变量。
- 视觉是 GitHub 风格的深/浅两套色板，**没有品牌落点、没有动效、没有 3D、没有字体层级**；两页的 18 条令牌声明各自复制一份，已经存在漂移风险。
- 公开仓库 `lza6/CF-Bypass-Research` 有 README 和 docs，但**没有公开产品站**：没有 favicon、description、Open Graph、sitemap、robots、结构化数据；`homepageUrl` 为空。
- 服务页是 `Cache-Control: no-store` 且容器以 `--host 0.0.0.0` 暴露，但**没有 `X-Robots-Tag`**；搜索爬虫仍可能收录服务状态页。
- 仓库没有任何 `package.json` 或前端构建链；本机 Node 25.8.1 / npm 11.6.4 可用。

本文件补的就是这一层：**一个独立公开站承载 Framer 动效、3D 与 SEO；现有两个服务页面只做令牌、可访问性、纯 CSS 动效的轻改，E-4 的零构建与 CSP 契约原样保留。**

### 0.2 复核结论（9 条，逐条带证据）

1. **现有 UI 是零构建单文件。** `solver/statuspage.py` 484 行 / 23,442 字节 / 内联 CSS 58 行 / 18 条令牌声明 / 19 处 `#hex` / 24 处 `color:`；`solver/console_html.py` 476 行 / 22,627 字节 / 内联 CSS 50 行 / 18 条令牌声明 / 19 处 `#hex` / 17 处 `color:`。两页的 9 个令牌 × 2 个主题块逐字重复。**已验证**（`wc`、`grep -oE`）。
2. **CSP 是严格白名单。** 两页响应头均为 `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`。**已验证**（`solver/service.py:2119`、`:2141`）。
3. **24 个 UI 单测锁死安全不变量。** `tests/test_console.py` 12 个、`tests/test_statuspage.py` 12 个；覆盖零写入、CSP、页面不含 token、无外部资源、白名单字段、wedged 状态。另有 `scripts/verify_ui_e2e.py` 用真 Chrome 跑 5 种状态 + 按钮交互 + 截图。**已验证**。
4. **现有配色正文全部达 AA，只有 wedged 徽章不达。** 暗色 `--muted #8b98a5` 对 `#0f1419` 为 6.29:1；亮色 `--muted #656d76` 对 `#f6f8fa` 为 4.93:1；亮色 `--accent #0969da` 为 4.88:1。**但 `.badge.wedged` 的白字压 `#f85149` 只有 3.35:1**，12px/600 不属于大字号，按 WCAG AA 需要 4.5:1。**已验证**（脚本计算，见附录 A.4）。
5. **SEO 资产为零。** 两页只有 `<meta charset>`、`viewport`、`<title>`；没有 description、canonical、OG/Twitter、robots、sitemap、favicon、theme-color。`<title>` 只有中文。**已验证**（`grep`）。
6. **服务页可被搜索引擎收录。** `/` 免鉴权、公网可达；`/console` 带 token 但页面本身可被爬取链接；两页均无 `X-Robots-Tag`。**静态确认**（`service.py` 路由 + compose `--host 0.0.0.0`）。
7. **公开站需要新的 Node 构建链，但不需要改 Python。** 仓库 public、Node 25.8.1/npm 11.6.4 可用、没有 `package.json`；把站点放在独立 `site/` 目录即可与 Python/Go 完全隔离。**已验证**（`node -v`、`gh repo view`、`ls`）。
8. **3D 只能按真实字段建模。** 现有数据是：域名、防护类型、求解后端、求解耗时、回放状态、缓存命中、出口 IP/延迟。没有 asset/host/port/边的资产拓扑。任何"攻击路径 3D 图"都会是编造的边。**静态确认**。
9. **两页都没有动效。** `@keyframes` 0 个、`transition` 0 处、`prefers-reduced-motion` 0 处；`@media` 只有 `prefers-color-scheme`。**已验证**（`grep`）。

### 0.3 已拍板决策（2026-11-06）

| # | 问题 | 你的选择 | 对施工的约束 |
|---|---|---|---|
| 1 | 落地范围 | **公开站 + 控制台轻改** | 新站独立 `site/`；`/` 与 `/console` 保持零构建，只做令牌/可访问性/纯 CSS 动效 |
| 2 | 公开站技术栈 | **Next.js App Router** | Next `16.4.5`；静态导出 `output: 'export'`；不用 middleware/Server Actions/运行时服务 |
| 3 | 3D 程度 | **多个 3D 场景** | 本文件收敛为 **2 个真 3D 场景 + 1 个 2D 数据图**；第三个 3D 缺真实数据关系，不做 |
| 4 | 品牌口径与语言 | **产品口径 + 中英双语；H1/title 直接用 Bypass/过盾** | 主站默认中文（根路径），英文在 `/en/`；`hreflang` 双向；H1/title 用 "Cloudflare Bypass / 过盾"，"Challenge Solver" 作为副标题与正文解释词；保留免责声明与非关联声明 |
| 5 | 验证环境 | **只本地静态服务 + 节流** | 不启用 GitHub Pages/Cloudflare Pages；真实部署头不验证，明确披露 |
| 6 | 搜索量 | **只做意图矩阵，不写 volume** | `seo/keywords.json` 只保留意图/页面/放置；上线后 Search Console 回填 |
| 7 | 英文文案 | **模型撰写 + 人工审阅清单** | `site/content/EN-REVIEW.md`；未母语签字，文档标注待审 |

### 0.4 一句话验收

公开站两条语言路径都能被静态构建、每个页面有唯一 title/description/canonical/hreflang 与可解析的 JSON-LD；3D 在 WebGL 关闭、`prefers-reduced-motion: reduce`、移动端三种情况下都能退化成等价表格；现有 `/` 与 `/console` 的 34 个单测 + 5 状态 E2E 全绿，wedged 徽章对比度 6.47:1；Python 1021 / Go 46 不回归；桌面 Lighthouse 100/100/100/100，移动 98/100/100/100，axe 0。

### 0.5 实施后的实测结论（2026-11-06）

- 静态导出 + 多根布局 + `globalNotFound` + `ImageResponse` 全部可用；`sitemap.ts`/`robots.ts`/`opengraph-image.tsx` 必须显式 `dynamic = 'force-static'`，否则构建失败。
- CSP hash 必须写成带单引号的 `'sha256-…'`；不带引号会被浏览器判为 invalid，内联脚本全拦。修正后 17 个 HTML / 18 个 hash，Chromium 0 报错。
- Next `Link` 在静态导出下产生 RSC 预取 404，并在离开 3D 页面时触发 `removeChild`；改用原生 `<a>` 后 0 404 / 0 pageerror，代价是整页导航。
- 移动端（`pointer: coarse`）走 static，不加载 WebGL；移动 TBT 从 230ms 降到 150/140ms。
- 桌面 Lighthouse 100/100/100/100，移动 98/100/100/100；axe 15 页 0 违规；初始 JS 228.4 KB gzip，3D chunk 241.8 KB，CSS 6.7 KB；预算已按实测修正（§3.4、§16.3）。
- 现有两页：34 个单测 + 35/35 E2E；wedged `#B91C1C` 6.47:1；共享令牌、焦点、reduced-motion、noindex 头已落地。

---

## 1. 现状基线

### 1.1 实测与静态确认数据

| 维度 | 现状 | 口径 |
|---|---|---|
| 版本 | `6.3.0`，`docs/facts.json` `_measured_at: 2026-11-05` | 已验证 |
| 状态页 | `solver/statuspage.py` 484 行 / 23,442 B；内联 CSS 58 行；18 条令牌声明；19 处 `#hex`；`color:` 24 处；`@media` 1 个；`@keyframes` 0；`transition` 0 | 已验证 |
| 控制台 | `solver/console_html.py` 476 行 / 22,627 B；内联 CSS 50 行；18 条令牌声明；19 处 `#hex`；`color:` 17 处；`@media` 1 个；`@keyframes` 0；`transition` 0 | 已验证 |
| 令牌 | 9 个语义令牌（bg/panel/line/ink/muted/ok/warn/bad/accent）× 深/浅两套 = 每页 18 条声明；两页逐字重复 | 已验证 |
| UI 单测 | `tests/test_console.py` 12 + `tests/test_statuspage.py` 12 = 24 | 已验证 |
| UI E2E | `scripts/verify_ui_e2e.py` 覆盖 ready / wedged / degraded / 无 token / 服务不可达 5 状态；断言徽章文字/颜色/横幅/面板；截图落 `plans/证据/` | 已验证（脚本 docstring + 代码） |
| CSP | `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'` | 已验证（`service.py:2119-2125`、`:2141-2146`） |
| 缓存头 | 两页 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer` | 已验证 |
| 索引控制 | 无 `X-Robots-Tag`、无 `<meta name="robots">` | 已验证 |
| 部署 | `docker-compose.yml` 3 worker，端口 8878–8880，`--host 0.0.0.0`；本地 `run.py` 默认 `127.0.0.1` | 已验证 |
| 仓库 | `github.com/lza6/CF-Bypass-Research`，visibility `PUBLIC`，`homepageUrl` 为空 | 已验证（`gh repo view`） |
| 前端构建链 | 无 `package.json`、无 `site/`、无 `node_modules` | 已验证 |
| 工具链 | Node 25.8.1、npm 11.6.4；Next 16.4.5 要求 Node ≥ 20.9.0 | 已验证（`node -v`、`npm view next@16.4.5 engines`） |
| 现有测试总量 | Python 971（`facts.json` 基线）；2026-11-06 实跑 1021 passed / 7 skipped；覆盖率 90%、Go 46、findings 54 | 已验证 |
| 公开域名 | 无 | 已验证（`homepageUrl` 为空） |
| 分析/统计 | 无 | 静态确认 |

### 1.2 设计债清单

| ID | 问题 | 证据 | 级别 | 目标阶段 |
|---|---|---|---|---|
| D-01 | `.badge.wedged` 白字压 `#f85149` = 3.35:1，12px/600 不达 AA | 脚本计算（附录 A.4） | **P1** | Phase F |
| D-02 | 两页没有 `:focus-visible` 规则，键盘用户看不到焦点 | `grep focus` 无结果 | **P1** | Phase F |
| D-03 | 320–360px 宽度下表格/头部 token 输入可能横向溢出 | 未实测；`input min-width:220px`、无表格滚动容器 | 待验证 | Phase F |
| D-04 | 服务页可被搜索引擎收录，状态页可能暴露部署形态 | 无 `X-Robots-Tag`/`noindex` | P2 | Phase F |
| D-05 | 两页 18 条令牌声明逐字重复，改一处要改两处 | `grep -oE -- '--[a-z0-9-]+:'` 各 18 | P2 | Phase F |
| D-06 | 无 favicon、无 `theme-color`，浏览器标签与移动端 UI 无品牌 | `grep` 无结果 | P3 | Phase F |
| D-07 | 没有公开产品面；SEO 资产为零 | §1.1 | **P1** | Phase B/C |
| D-08 | 没有动效体系；两页 `transition` 为 0 | §1.1 | P2 | Phase D |
| D-09 | 没有 3D/空间叙事，公开站与 README 观感无差异 | §1.1 | P3 | Phase E |
| D-10 | 公开站没有主题切换（只有系统偏好） | 尚未建站 | P2 | Phase A |
| D-11 | 没有"数字必须带日期/来源/条件"的内容规则 | 尚未建站 | **P1** | Phase C |
| D-12 | 没有可访问性 CI 门禁 | 现有 UI 测试是字符串断言，不是 axe | **P1** | Phase G |
| D-13 | 没有性能预算 | 尚未建站 | **P1** | Phase G |
| D-14 | 没有 Node 供应链门禁（lockfile/audit/ignore-scripts） | 尚未建站 | **P1** | Phase G |

### 1.3 三条需要纠正的预设

1. **"把 Framer 直接装进控制台"不成立。** `plans/README.md` 的 E-4 已经拍板：控制台单文件、零构建、零新依赖、CSP `default-src 'none'`、零写入。引入 React/motion 会同时打破这五条，并把既有单测的安全契约变成需要重写的旧账。**动效与 3D 只放在独立公开站。**
2. **"给 `/` 状态页做 SEO"是错的方向。** 它是服务端点、`no-store`、公网可达；正确的动作是 **noindex**，把 SEO 面收敛到独立静态站。
3. **"3D 越多越好"不成立。** 现有真实数据只够支撑 2 个场景；第三个场景需要资产拓扑边，而数据里没有。用 2D 图表表达缓存/吞吐，信息密度更高、性能更稳。

---

## 2. 目标、角色与非目标

### 2.1 角色与任务

| 角色 | 进来要回答的问题 | 成功信号 | 主要落点 |
|---|---|---|---|
| 运维 | 服务现在是 ready / degraded / wedged？缓存命中多少？出口是谁？ | 10 秒内看到状态与下一步动作 | `/`、`/console`（保持轻量） |
| 集成开发者 | 能不能接？怎么接？失败怎么回放？ | 读到 API/快速开始，复制一段可运行的 curl/Go/Python | 公开站 `/docs`、`/how-it-works` |
| 研究同行 | 结论是不是真的？口径是什么？哪里没做成？ | 看到日期、条件、已知限制、可复现命令 | `/benchmarks`、`/security` |
| 潜在采用者 | 跟 FlareSolverr/无头浏览器比，差异在哪？ | 形成"值得本地跑一次"的判断 | 首页、对比段、GitHub CTA |

### 2.2 公开站的转化目标

1. 读懂能力边界（不是"万能过盾"）。
2. 去 GitHub 读 README / 点 star。
3. 按文档在本地跑起来（`run.ps1 start` 或 `docker compose up`）。
4. 通过 GitHub Issues 反馈。

**不做**注册、登录、支付、在线真实求解、额度、API key 发放。公开站是静态站，不持有任何求解凭证。

### 2.3 非目标

- 不改后端语义、不改 API 契约、不重开可靠性工作。
- 不做在线真实求解 demo（没有公网求解端点，也不应该为此开一个）。
- 不做账号体系、支付、订阅、用量面板。
- 不做原生移动 App；移动端是响应式 Web。
- 不做 CMS、不做博客后台、不做多语言 > 2。
- 不做全站 3D、不做 WebGL 首屏阻塞、不做自动播放视频。
- 不引入第三方字体/图标/分析 CDN（CSP 与供应链双重约束）。

### 2.4 成功指标（验收口径）

| 指标 | 目标 | 测量方式 |
|---|---|---|
| Lighthouse SEO | 100（实测 zh/en 首页） | Lighthouse CI，桌面/移动 |
| Lighthouse Performance | ≥ 90 移动 / ≥ 95 桌面（实测 98 / 100） | Lighthouse CI |
| Lighthouse Accessibility | ≥ 95，axe serious/critical = 0（实测 100 / 0） | Lighthouse + `axe-core` |
| LCP | ≤ 2.0s（实测桌面 0.3s / 移动 1.2s） | Lighthouse CI |
| CLS | ≤ 0.05（实测 0） | Lighthouse CI + Playwright layout-shift 监听 |
| INP | ≤ 200ms（实测 TBT 桌面 0 / 移动 150ms） | 实验室以 TBT 近似；真实字段待 Search Console/CrUX |
| 初始 JS | 实测 228.4 KB gzip；上限 250 KB | `next build` + gzip 测量 |
| 3D chunk | 实测 241.8 KB gzip；上限 260 KB | gzip 测量 |
| 现有回归 | Python 1021 / Go 46 / UI 34 / E2E 35/35 全绿 | 既有命令 |
| 内容准确性 | 每个数字有 `value/unit/measured_at/source/conditions` | 数据文件 + 代码审查 |

---

## 3. 设计原则与硬约束

### 3.1 证据优先：每个数字都要能追到日期、条件和来源

公开站不是宣传页，是"可复核的能力页"。所有性能数字必须来自 `site/data/benchmarks.ts`，字段固定：

```ts
type Benchmark = {
  id: string;
  label: { zh: string; en: string };
  value: number;
  valueMax?: number;        // 区间值的上限；无区间则省略
  unit: 'ms' | 's' | 'qps' | 'bytes' | 'count' | 'percent' | 'mb';
  measuredAt: string;       // ISO date，缺失日期不得上线
  conditions: { zh: string; en: string };
  source: string;           // 仓库内路径或 docs 链接
  status: 'verified' | 'partial' | 'limitation';
};
```

规则：

- 页面组件不得硬编码性能数字；只能从该文件读。
- 每个数字旁边渲染 `measuredAt` 与 `source`（可折叠，但必须在 DOM 里）。
- `status: 'limitation'` 的条目必须显示，不得只挑好数字。
- 发布前用 `npm run test:content` 校验 schema 与来源路径存在。
- 日期在 Phase C 从 `CHANGELOG.md` 与 `plans/证据/` 回填；回填不出来的数字不上线。

### 3.2 安全边界：公开站与内网控制台物理分离

| 面 | 运行形态 | 鉴权 | 可索引 | 允许的脚本 |
|---|---|---|---|---|
| 公开站 `site/` | 静态导出（纯 HTML/CSS/JS） | 无 | 是 | 自有打包产物 + 构建期 hash 白名单 |
| 状态页 `GET /` | Python 进程内联 HTML | 无 | **否** | 内联脚本，CSP `unsafe-inline`（现有契约） |
| 控制台 `GET /console` | Python 进程内联 HTML | Bearer token | **否** | 同上 |

公开站不得调用 `/solve`、`/detect`、`/health` 等任何求解服务端点；它只展示构建期快照与仓库文档。任何"在线试用"入口都指向本地部署文档，不指向真实服务。

### 3.3 可访问性红线（WCAG 2.2 AA，不可谈判）

- 正文与 UI 文本对比度 ≥ 4.5:1；大字号 ≥ 3:1；非文本控件边界/焦点 ≥ 3:1。
- 键盘可达：所有交互有可见 `:focus-visible`；焦点顺序与视觉顺序一致；无键盘陷阱。
- 语义：每页一个 `<h1>`；标题层级不跳级；`header/nav/main/footer` 地标；跳转链接。
- 3D/图表：`aria-hidden` 的 canvas 旁必须有等价文本或表格；不得只靠颜色传达状态。
- 动效：`prefers-reduced-motion: reduce` 下全部静止；不得自动播放、不得无限循环。
- 触摸目标 ≥ 44×44 CSS px（WCAG 2.2 最低 24×24，本项目取更严的 44）。
- 200% 缩放不丢内容、不出现横向滚动（数据表可用局部滚动容器）。
- 语言切换链接带 `hreflang` 与 `lang`；中文页面 `lang="zh-CN"`，英文 `lang="en"`。

### 3.4 性能预算

| 项 | 预算 | 说明 |
|---|---|---|
| 初始路由 JS（gzip） | 实测 228.4 KB；硬上限 250 KB | 首版 150 KB 是估算；Next 16 + React 19 + motion 的实测基线更高，见 §16.3 |
| 3D chunk（gzip） | 实测 241.8 KB；硬上限 260 KB | 已移除 drei；three.js 本身是主要体积 |
| CSS（gzip） | 实测 6.7 KB；上限 25 KB | Tailwind 4 只产用到的工具类 |
| 字体（woff2，总计） | ≤ 120 KB | Inter 子集 + JetBrains Mono 子集；CJK 用系统栈 |
| 图片 | 单张 ≤ 150 KB；优先 AVIF/WebP | 静态导出不能用默认 Image Optimization |
| 首屏第三方请求 | 0 | 无字体/图标/分析 CDN |
| LCP | 桌面 0.3s / 移动 1.2s；上限 2.0s | 3D canvas 不参与 LCP |
| CLS | 0；上限 0.05 | 3D 升级前后预留固定高度 |
| TBT | 桌面 0ms / 移动 150ms；上限 200ms | 移动端不加载 WebGL |

### 3.5 明确不做的风格陷阱

- 全站玻璃拟态/毛玻璃卡片：只在导航栏滚动时用一次 backdrop blur。
- 全屏 3D 背景、自动旋转模型、滚动劫持、鼠标自定义光标。
- 自动播放视频/音频、无限跑马灯、闪烁渐变文字。
- 大面积发光边框、彩虹渐变、无意义的粒子雨。
- 用骨架屏假装加载（静态站没有慢接口，骨架屏只会制造假状态）。
- 用"AI 生成"式空话做 hero 文案；每个句子必须能落到能力或证据。

---

## 4. 视觉体系

### 4.1 令牌三层

单一来源：`site/styles/tokens.css`。Tailwind 4 的 `@theme` 只做工具类映射，不另存一套色值。

```
primitive（原始值，只出现在 tokens.css）
  └─ semantic（bg / surface / ink / muted / primary / …）
       └─ component（button-bg / card-border / badge-ok-bg / focus-ring）
```

约束：

- 组件层只引用 semantic，不直接写 `#hex`。
- 页面层只引用 component 或 Tailwind 语义类（`bg-surface`、`text-muted`）。
- `npm run test:tokens` 扫描 `site/components/**` 与 `site/app/**`，发现 `#hex`/`rgb(`/`hsl(` 直接失败（`tokens.css` 与 3D 材质常量白名单除外）。

### 4.2 色板：Deep Signal

品牌方向：**近黑蓝底 + 电光薄荷主色 + 紫罗兰辅助色**。深色是默认（开发者工具的主场），浅色是完整对等主题，不是"降级版"。

#### 深色（默认）

| 令牌 | 值 | 用途 | 对 `bg` 对比度 |
|---|---|---|---|
| `--bg` | `#06080D` | 页面底 | — |
| `--surface` | `#0C111A` | 卡片/面板 | 1.06（非文本） |
| `--surface-2` | `#121A26` | 悬浮/代码块 | — |
| `--line` | `#223041` | 分隔线/边框 | 1.49（装饰性） |
| `--ink` | `#EAF1F8` | 正文 | 17.59 |
| `--muted` | `#9FB0C3` | 次要文字 | 9.04 |
| `--primary` | `#4DE3C1` | 主 CTA/焦点 | 12.47 |
| `--secondary` | `#A78BFA` | 辅助强调 | 7.36 |
| `--success` | `#4ADE80` | 成功 | 11.50 |
| `--warning` | `#FBBF24` | 警告 | 12.00 |
| `--danger` | `#FB7185` | 危险 | 7.44 |
| `--info` | `#60A5FA` | 信息 | 7.88 |

按钮：底色 `--primary #4DE3C1`，文字 `#04120E`，对比度 **11.91**。

#### 浅色

| 令牌 | 值 | 用途 | 对 `bg` 对比度 |
|---|---|---|---|
| `--bg` | `#F4F7FB` | 页面底 | — |
| `--surface` | `#FFFFFF` | 卡片/面板 | 1.07（非文本） |
| `--surface-2` | `#EDF2F9` | 悬浮/代码块 | — |
| `--line` | `#D5E0EC` | 分隔线/边框 | 1.24（装饰性） |
| `--ink` | `#0B1220` | 正文 | 17.42 |
| `--muted` | `#5B6B7E` | 次要文字 | 5.08 |
| `--primary` | `#0B7F6B` | 主 CTA/焦点 | 4.59 |
| `--secondary` | `#6D4AE0` | 辅助强调 | 5.27 |
| `--success` | `#15803D` | 成功 | 4.67 |
| `--warning` | `#A16207` | 警告 | 4.58 |
| `--danger` | `#BE123C` | 危险 | 5.85 |
| `--info` | `#1D4ED8` | 信息 | 6.24 |

按钮：底色 `--primary #0B7F6B`，文字 `#FFFFFF`，对比度 **4.93**。

焦点环：2px `--primary` + 2px offset；深色对底 12.47:1、浅色 4.59:1，均高于非文本 3:1。边框不承担"唯一状态表达"：状态徽章同时有文字与图标，颜色只是加速识别。

#### 为什么保留深色默认

- 目标用户是开发者，工具类站点的默认深色是 2024–2026 的主流预期。
- 现有服务页已经是深色优先 + `prefers-color-scheme` 浅色兜底，公开站与它保持同一"仪器感"。
- 浅色主题完整实现，不是反色滤镜；两套令牌都经过对比度脚本校验。

### 4.3 排版与尺度

| 项 | 规则 |
|---|---|
| 基准 | 4px 网格：4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64 / 80 / 96 |
| 字号 | 12 / 13 / 14 / 16 / 18 / 20 / 24 / 32 / 40 / 56 / 72；hero 用 `clamp(40px, 6vw, 72px)` |
| 行高 | 正文 1.6；标题 1.12–1.2；代码 1.55 |
| 字重 | 400 正文 / 500 UI / 600 标题 / 700 仅数据强调 |
| 字距 | 标题 `-0.02em`；正文 0；全大写仅英文标签，中文不用 |
| 行宽 | 英文 62–72ch；中文 32–38 字/行 |
| 字体 | 英文 Inter Variable（自托管子集）；中文系统栈 `-apple-system, "PingFang SC", "Microsoft YaHei", "Noto Sans CJK SC"`；代码 JetBrains Mono（自托管子集） |
| 圆角 | 8 / 12 / 16 / 24；徽章 pill 999 |
| 阴影 | 深色用"1px 边框 + 低透明外发光"；浅色用三级柔和阴影；不用重投影 |
| 图标 | 内联 SVG（Lucide 风格，24×24，stroke 1.75），不引入图标 CDN |

字体文件与许可：

- Inter：SIL Open Font License 1.1，放 `site/public/fonts/Inter-Variable-subset.woff2` + `OFL.txt`。
- JetBrains Mono：SIL OFL 1.1，放 `site/public/fonts/JetBrainsMono-subset.woff2` + `OFL.txt`。
- 子集用本机 `fontTools 4.67.0` 的 `pyftsubset` 生成（Latin + 常用标点）；只预加载 Inter 一个文件；`font-display: swap`；`size-adjust` 减少 CLS。
- 中文不下载字体：CJK 字库体积与首屏预算不兼容，系统栈在 Windows/macOS/Android 都有可用字形。

### 4.4 质感：只有三处

1. **Hero 背景 mesh**：两个径向渐变（薄荷 8%、紫罗兰 6%）叠在 `--bg` 上，加 2% 透明度的 SVG 噪点；不做动态流光。
2. **卡片 hover 边框**：`border-color` 从 `--line` 过渡到 `color-mix(in srgb, var(--primary) 45%, var(--line))`，140ms；不做整卡发光。
3. **3D 场景自发光**：材质 `emissive` 使用 `--primary`/`--secondary`，强度低；UI 层不再叠发光。

除此之外保持克制：无玻璃卡片、无彩虹渐变、无大面积 blur。

### 4.5 双主题机制

- `html[data-theme="dark"|"light"]`；默认 `dark`。
- 首屏前内联一小段脚本读取 `localStorage['cf-theme']`，没有则用 `prefers-color-scheme`；脚本必须被 CSP hash 覆盖（见 §9.2）。
- `<meta name="color-scheme" content="dark light">`；`<meta name="theme-color">` 用 `media` 分别给深/浅。
- 导航栏右侧一个 44×44 图标按钮，`aria-label` 随状态变化；`aria-pressed` 表示当前是否深色。
- 切换只改 `data-theme`，不重建组件；3D 材质颜色通过主题上下文读取，切换时 200ms 过渡。
- 不跟随系统实时变化（避免用户手动选择后被覆盖）；首次访问才用系统偏好。

### 4.6 现有两页的令牌收敛与修正

目标：**不引入构建、不打破 CSP、不改零写入**，只做低风险修正。

| 动作 | 具体改动 | 验证 |
|---|---|---|
| 共享令牌 | 新增 `solver/ui_tokens.py`，导出 `TOKENS_CSS`（9 个令牌 × 深浅两块）；`statuspage.py`/`console_html.py` 用模块加载时替换注入，删除各自重复的 `:root` 块 | `grep` 确认两页令牌只来自一处；34 个单测全绿 |
| wedged 对比度 | `.badge.wedged { background:#B91C1C; border-color:#B91C1C; color:#fff }`；白字对比 6.47:1 | 脚本断言 ≥4.5；E2E 截图 |
| 焦点可见 | `:focus-visible { outline:2px solid var(--accent); outline-offset:2px }`；按钮/输入/链接统一 | 键盘 Tab 走一遍；E2E 断言规则存在 |
| 移动端 | 头部 `flex-wrap`；token 输入 `min-width:0; max-width:100%`；表格外包 `.table-scroll { overflow-x:auto }` | 320/360/768 截图 + `scrollWidth <= clientWidth` 断言 |
| 索引控制 | 两页 HTML 加 `<meta name="robots" content="noindex,nofollow">`；`service.py` 两处响应加 `X-Robots-Tag: noindex, nofollow` | 单测断言头与 meta |
| favicon | 内联 SVG data URI（`img-src 'self' data:` 已允许），不新增外部请求 | 单测断言 `rel="icon"` 且无 http(s) 外链 |
| 主题色 | `<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0f1419">` + light 版本 | 单测断言两条 meta |
| 动效 | 仅按钮/徽章 120ms `background-color/border-color` 过渡；加 `@media (prefers-reduced-motion: reduce){ *{transition:none!important} }` | 单测断言 reduced-motion 块存在 |
| 不改的 | CSP、内联脚本、`sessionStorage`、`textContent`/`createElement`、零 form/POST | 24 个既有单测原样通过；新增 10 个 |

新增测试（追加到现有两个测试文件，不替换；5 个断言主题 × 2 个文件 = 10 个测试函数）：

- `test_tokens_are_single_source`：两页 HTML 都包含 `TOKENS_CSS` 生成的令牌，且不再各自出现重复的 `--bg:` 声明。
- `test_wedged_badge_contrast`：解析 `.badge.wedged` 的 `background`/`color`，计算对比度 ≥ 4.5。
- `test_pages_are_noindex`：两页都有 `noindex` meta；`/` 与 `/console` 响应都有 `X-Robots-Tag`。
- `test_focus_visible_rule_exists`：两页 CSS 都含 `:focus-visible`。
- `test_reduced_motion_block_exists`：两页都含 `prefers-reduced-motion`。

---

## 5. 信息架构与页面

### 5.1 路由表（静态导出，默认中文在根路径）

| 路径 | 语言 | 页面 | 索引 | 主 CTA |
|---|---|---|---|---|
| `/` | zh | 首页 | 是 | GitHub / 快速开始 |
| `/how-it-works/` | zh | 工作原理 | 是 | 读 API |
| `/benchmarks/` | zh | 实测数据 | 是 | 看复现命令 |
| `/docs/` | zh | 文档入口 | 是 | 打开 API 文档 |
| `/security/` | zh | 安全与授权使用 | 是 | 读免责声明 |
| `/changelog/` | zh | 版本记录 | 是 | GitHub Releases |
| `/en/` | en | Home | 是 | GitHub / Quickstart |
| `/en/how-it-works/` | en | How it works | 是 | Read the API |
| `/en/benchmarks/` | en | Benchmarks | 是 | Reproduce |
| `/en/docs/` | en | Docs | 是 | API reference |
| `/en/security/` | en | Security & authorized use | 是 | Disclaimer |
| `/en/changelog/` | en | Changelog | 是 | GitHub Releases |
| `/styleguide/` | zh | 内部样式指南 | **否** | — |
| `/en/styleguide/` | en | Internal styleguide | **否** | — |
| `/404.html` | — | 404 | 否 | 回首页 |

URL 规则：

- 默认语言不带前缀，英文带 `/en/`；`hreflang` 映射：`zh-CN` ↔ `/...`，`en` ↔ `/en/...`，`x-default` ↔ `/...`。
- `trailingSlash: true`，静态托管下路径稳定，避免 `/docs` 与 `/docs/` 重复。
- 不生成 `/zh/` 镜像，避免与根路径重复内容。
- 语言切换按钮把当前路径映射到另一语言；映射表由路由清单生成，测试断言每个路由都有对偶。

### 5.2 首页结构（从上到下）

1. **导航**：品牌字标（CF Solver）+ 产品 / 原理 / 数据 / 文档 / 安全 + 语言切换 + 主题切换 + GitHub。
2. **Hero**：H1「Cloudflare 过盾：把挑战变成可复放的凭证」；副标题一句能力边界；双 CTA「在本地跑起来」「读 API」；右侧/背景是 3D 场景 A；下方三个真实数字：`18.43s` 同站最快求解、`2.93ms` 缓存热路径 p99、`0.55s` 无盾站放行（每个都带日期与条件链接）。
3. **它解决什么**：三张卡 —— 一次求解、30 分钟回放；`route=auto` 不白等；缓存命中零 Python 参与。
4. **流水线（2D）**：Domain → Detect → cfsolve → pydoll → Camoufox → FlareSolverr → Token → Replay；hover/focus 显示真实字段；3D 场景 B 在下一屏。
5. **实测数据摘要**：4 行表格（场景、值、条件、日期），链接到 `/benchmarks/`。
6. **3D 场景 B：Pipeline**：可交互流水线，节点可聚焦，右侧同步文本说明。
7. **接入代码**：Go / Python / curl 三个 tab；代码块可复制；每段都有"这是本地调用，不是在线 demo"的说明。
8. **已知限制**：3–5 条，包含 managed challenge 回放未通过、2026-11-03 新挑战脚本的 stage2 卡点、需要真实出口 IP 匹配。
9. **FAQ**：8 条，覆盖"纯协议为什么行/不行"、"和 FlareSolverr 的区别"、"要不要浏览器"、"缓存多久"、"失败怎么办"。
10. **Footer**：MIT、GitHub、免责声明、非 Cloudflare 关联声明、语言/主题、最后更新日期。

首屏 LCP 是 H1 文本，不是 canvas；3D 在 `requestIdleCallback` 后淡入，容器高度固定，CLS 为 0。

### 5.3 各页职责与目标关键词

| 页面 | 职责 | 中文目标词 | 英文目标词 |
|---|---|---|---|
| `/` | 一句话价值 + 三个证据 + 两个 CTA | Cloudflare 过盾、CF 挑战求解、cf_clearance | Cloudflare bypass、Cloudflare challenge solver、cf_clearance |
| `/how-it-works/` | 机制、回落链、失败模式 | CF 五秒盾、jsd 挑战、managed challenge、Turnstile 求解 | jsd challenge、managed challenge、Turnstile solver |
| `/benchmarks/` | 实测方法、数字、条件、限制 | Cloudflare 求解 耗时、纯协议 过盾、缓存 命中 | Cloudflare solver benchmark、pure protocol solver、cache hit |
| `/docs/` | 快速开始、API、Go/Python 示例 | Cloudflare 求解 API、cf_clearance 获取 | Cloudflare solver API、cf_clearance cookie |
| `/security/` | 授权使用、数据处理、披露 | 授权测试、反爬 研究 | authorized testing、responsible disclosure |
| `/changelog/` | 版本与能力变化 | CF 求解 更新 | Cloudflare solver changelog |

标题公式（≤ 60 字符，中文按 30 字以内）：

- 中文：`{能力} — {产品}｜{一句差异}`
- 英文：`{Primary keyword} — {Product} | {Differentiator}`

示例（最终文案在 Phase C 定稿）：

- `/`：`Cloudflare 过盾 — CF Solver｜缓存热路径 <3ms`
- `/en/`：`Cloudflare Bypass — CF Solver | <3ms cached replay`

H1/title 用 "Cloudflare Bypass / 过盾"；"Challenge Solver" 放到副标题与正文解释词。平台政策风险见 §8.3 的说明。

### 5.4 内容页的最小结构

每个内容页必须回答五件事，缺一不可：

1. **问题**：这个机制在什么场景下会挡路。
2. **机制**：它怎么工作（可引用 `docs/` 的结论）。
3. **证据**：至少一个带日期/条件/来源的数字或命令。
4. **限制**：至少一条真实限制，不能只讲成功。
5. **下一步**：一个 CTA（读 API / 本地跑 / 提 issue）。

### 5.5 移动端与 PWA 策略

- 响应式 Web，断点 360 / 480 / 768 / 1024 / 1280 / 1536。
- 不做原生 App，不注册 Service Worker（静态站缓存策略简单，SW 只会引入陈旧内容风险）。
- 移动端 3D 默认 `lite` 或 `static` 档；不做"为了炫技强行 60fps"。
- 导航在 < 768px 用原生 `<dialog>` 做全屏菜单，焦点陷阱由 `<dialog>` 自带；关闭后焦点回到触发按钮。
- 代码块横向滚动，不撑破页面；复制按钮 44×44。

### 5.6 内容来源与单一声明

| 内容 | 来源 | 更新方式 |
|---|---|---|
| 能力矩阵 | `README.md` §这个项目能给你什么 | 发版时人工同步，PR 检查 |
| 实测数字 | `site/data/benchmarks.ts` | 发版时更新；每个值带 `source` |
| API/快速开始 | `docs/API.md`、`docs/openapi.json` | 链接到仓库，不复制整份 |
| 已知限制 | `docs/findings.json`（F-051 等）+ README 当前状态 | 发版时复核 |
| 版本记录 | `CHANGELOG.md` | 发版时取前 3 个版本摘要 |

原则：**站内不维护第二份事实源**。能在仓库里链接的，不复制；必须展示的数字，进 `benchmarks.ts` 并带来源。

---

## 6. 动效体系（motion）

### 6.1 依赖与体积

| 包 | 版本 | 用途 | 备注 |
|---|---|---|---|
| `motion` | `14.9.1` | 动效原语 | `framer-motion` 的后续包；导入 `motion/react` |
| `LazyMotion` + `m` | — | 按需加载 DOM 动画特性 | 不用完整 `motion.div` 默认包，避免把全部 feature 打进首屏 |
| `domAnimation` | — | 基础 transform/opacity/gesture | 不用 `domMax`（layout animation 用不到） |

规则：

- 首屏只 import `m` 与 `LazyMotion`；`loadFeatures` 用动态 import。
- 不引入 `AnimatePresence` 做路由退场（App Router 没有可靠的退出时机，容易卡住）；路由切换用 `template.tsx` 的入场淡入。
- 3D 场景里的动画用 R3F 的 `useFrame`，不经过 motion。

### 6.2 时长与缓动令牌

| 令牌 | 值 | 用途 |
|---|---|---|
| `--dur-micro` | 90ms | 图标/颜色微反馈 |
| `--dur-ui` | 140ms | 按钮、卡片 hover |
| `--dur-panel` | 220ms | 折叠、菜单、提示 |
| `--dur-hero` | 360ms | 首屏文字/元素入场 |
| `--dur-scene` | 600ms | 3D 相机/场景切换 |
| `--ease-out` | `cubic-bezier(0.22, 1, 0.36, 1)` | 入场 |
| `--ease-in` | `cubic-bezier(0.4, 0, 1, 1)` | 退场 |
| `--ease-spring` | stiffness 180 / damping 24 | 3D 相机缓动 |

位移距离只用 8 / 16 / 24px；不做 100px 以上的滑入。

### 6.3 动效清单

| 位置 | 触发 | 属性 | 时长 | 降级 |
|---|---|---|---|---|
| 导航栏 | 滚动超过 24px | 背景不透明度 0→0.82 + blur 8px | 180ms | 直接切换，无过渡 |
| Hero 标题 | 首次进入 | 逐词 y 12→0、opacity 0→1，stagger 40ms | 360ms | 静态 |
| Hero 数字 | 进入视口 | 数字 0→目标值 | 600ms | 直接显示最终值 |
| 区块 | 进入视口（once） | y 16→0、opacity 0→1 | 220ms | 静态 |
| 卡片 | hover/focus | translateY(-2px) + border-color | 140ms | 只改边框颜色 |
| 代码 tab | 切换 | 交叉淡入 120ms | 120ms | 直接切换 |
| FAQ | 展开 | 内容高度/透明度 | 220ms | 用原生 `<details>` 直接展开 |
| 3D 场景 A | idle 后 | canvas 淡入 400ms | 400ms | 静态 SVG |
| 3D 节点 | hover/focus | 高亮 + 标签淡入 | 140ms | 文本列表高亮 |
| 页面切换 | 路由进入 | opacity 0→1 | 120ms | 静态 |

明确不做：滚动劫持、滚动进度条动画、无限循环的粒子/跑马灯、鼠标跟随光斑、自动旋转的 3D 模型。

### 6.4 reduced-motion 的两种实现

1. **CSS**：`@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation-duration: 0.01ms !important; animation-iteration-count: 1 !important; transition-duration: 0.01ms !important; scroll-behavior: auto !important; } }`
2. **JS**：`useReducedMotion()` 为真时，不挂载 3D canvas、不启动数字计数、不注册 pointer parallax；直接渲染静态 poster 与最终数字。

两者必须同时存在：CSS 覆盖全局，JS 阻止无意义的计算与 WebGL 初始化。

### 6.5 与测试的接缝

- Playwright `reducedMotion: 'reduce'` 打开首页：断言 canvas 不存在或 `data-scene="static"`，数字文本等于最终值，`getComputedStyle` 的 `transition-duration` 为 `0s`。
- 断言无 `pageerror`、无 `console.error`。
- 用 `PerformanceObserver` 监听 `layout-shift`，累计 CLS < 0.05。
- 动画不得改变布局：只动 `transform`/`opacity`；高度动画只用于 FAQ 内容区，且容器有固定 `min-height` 预算。

---

## 7. 3D 与空间叙事（R3F）

### 7.1 数据可行性：先看数据，再定场景

| 可用字段 | 来源 | 能支撑的视觉 |
|---|---|---|
| 挑战类型（jsd / managed / turnstile） | `solver/detect.py`、`docs/检测-防护识别.md` | 三面挑战墙 / 三类节点 |
| 求解后端与顺序（cfsolve → pydoll → Camoufox → FlareSolverr） | `solver/backends.py`、`solver/engines.py` | 流水线节点与有向连线 |
| 求解耗时、回放状态、缓存命中 | `benchmarks.ts`、`/health`、`/stats` | 节点大小/颜色/标签 |
| 域名 | 测试域与用户自填 | 节点标签（公开站只用示例域） |
| 出口 IP / 代理池 | `solver/proxypool.py` | **不做地理球**：没有可信 geo 数据，容易编造 |

**不能支撑的视觉**：资产拓扑图、攻击路径图、网络边关系。数据里没有 asset/host/port/edge 字段，任何"边"都是编的。第三个 3D 场景因此不做，缓存/吞吐改用 2D SVG 折线图。

### 7.2 场景 A：Challenge Wall（首页 Hero）

**叙事**：三面悬浮的"挑战墙"（jsd / managed / turnstile），一束细光穿过其中一面，落到一枚发光的"凭证"上。表达"一次求解 → 可复放凭证"，不是"打穿一切"。

**真实数据绑定**：

- 每面墙的标签显示挑战类型与该类型的实测求解时间（来自 `benchmarks.ts`）。
- 凭证标签显示 `cf_clearance`、字节数、回放耗时。
- 墙面下方一行小字：`示例数据，口径见 /benchmarks/`。

**交互**：

- 指针在 canvas 上移动 → 相机最多 3° 视差；离开后回正。
- 三面墙可点击/可键盘聚焦（DOM 按钮覆盖层），选中后右侧说明卡切换到对应类型。
- 不自动旋转；页面不可见时停止渲染。

**实现约束**：

- 3 个平面 + 1 条光束 + 1 个凭证几何体；三角形 < 12k；draw calls < 20。
- 材质用 `meshStandardMaterial` + `emissive`，不用 transmission/refraction（GPU 成本高）。
- 不用 HDR/Environment 贴图（外部资源 + 体积）；只用 1 个 ambient + 2 个 directional + 1 个 point。
- 不开阴影；用一张低透明度径向渐变平面伪造接触阴影。
- 标签用 DOM 覆盖层渲染（不使用 drei），保证字体与对比度可控；canvas 本身 `aria-hidden="true"`。

### 7.3 场景 B：Pipeline（首页第二屏 / `/how-it-works/`）

**叙事**：一条从左到右的求解流水线：`Domain → Detect → Backend chain → Token → Replay`。不是拓扑图，是**真实调用顺序**。

**真实数据绑定**：

- 5 个主节点；Backend chain 展开 4 个子节点（cfsolve / pydoll / Camoufox / FlareSolverr）。
- hover/focus 节点显示真实字段名（`state`、`attempts`、`backend`、`solve_ms`、`cached`）。
- 右侧同步文本面板显示同一信息；不依赖 canvas 才能读。

**交互**：

- 节点 hover/focus 高亮，连线脉冲一次（不循环）。
- 点击节点 → 锚点到下方对应说明或 `/docs/` 的 API 字段。
- 相机固定等轴，指针视差 ≤ 2°；reduced-motion 下完全静止。

**实现约束**：

- 节点用普通 mesh；连线用薄 `boxGeometry` mesh（不引入 drei）。
- 标签用 `<Html>`；不做 3D 文字（避免字体文件与体积）。
- 三角形 < 8k；draw calls < 30。
- 连线颜色：默认 `--line`，高亮 `--primary`；节点状态色同时有文字标签。

### 7.4 场景 C：明确不做

缓存命中/吞吐用 **2D SVG 折线图**（`/benchmarks/`）：数据密度更高、可打印、可被屏幕阅读器读取，且不增加 WebGL 预算。第三个 3D 场景需要不存在的数据关系，不做。

### 7.5 加载、降级与可访问性（三档）

| 档位 | 触发条件 | 表现 |
|---|---|---|
| `full` | WebGL2 可用、`deviceMemory > 4`、`hardwareConcurrency > 4`、非 reduced-motion、非 save-data | DPR ≤ 1.5，60fps 目标，指针视差 |
| `lite` | WebGL 可用、桌面、`deviceMemory ≤ 6` | DPR 1，30fps 上限，无视差，减少粒子 |
| `static` | WebGL 不可用 / reduced-motion / save-data / `pointer: coarse`（移动端） / `deviceMemory ≤ 4` / 加载失败 / JS 关闭 | 静态 SVG 蓝图 + 等价数据表；无 canvas |

> 移动端不加载 WebGL：242 KB 的 three chunk 会把移动 TBT 推到 230ms；改成 static 后实测 150/140ms。

加载顺序：

1. SSR 输出 `static` 档的 SVG + 数据表（LCP 安全，CLS 为 0）。
2. 客户端 hydration 后探测能力；若允许，`dynamic(() => import('./Scene'))` 懒加载。
3. Scene A 在 `requestIdleCallback` 后加载；Scene B 在进入视口前 200px 加载。
4. 加载失败（chunk 404/WebGL context lost）→ 永久 `static`，不重试循环。

可访问性：

- canvas 外层 `role="img"` + `aria-label` 一句话；canvas 本身 `aria-hidden="true"`。
- 场景旁必须有 `<table>` 或 `<dl>` 等价数据；键盘用户通过 DOM 按钮控制场景选中态。
- 焦点顺序：先文本，后场景控制；场景控制按钮 44×44。
- 颜色不是唯一状态：节点有文字标签，状态有图标 + 文本。

### 7.6 性能约束

- `dpr={[1, 1.5]}`；`gl={{ antialias: true, powerPreference: 'high-performance' }}`。
- `frameloop="demand"`：静止时不渲染；hover/相机变化时 `invalidate()`；入场 600ms 用 `always`，之后切 `demand`。
- `document.hidden` 或 canvas 离开视口 → 停止 rAF。
- 无 postprocessing（bloom/DOF/SSAO）；发光用材质 `emissive` + CSS 外发光。
- 3D chunk 单独 split；首屏不 preload；`<link rel="preload">` 只给字体。
- 移动端 `lite` 档默认 30fps；不追求 60fps。

### 7.7 安全

- 不加载外部模型/纹理/HDR/字体；所有几何体在代码里生成。
- 不用 `eval`/`new Function`；不把 URL 参数传进材质或着色器。
- 3D 依赖锁定版本；`three` 的 shader 编译失败只影响场景，不影响页面其余部分（Error Boundary 包裹）。
- CSP `script-src 'self' + hash`；3D chunk 是同源静态文件。

### 7.8 测试

- Playwright 启动参数加 `--disable-webgl` → 断言 `static` 档可见、等价表格存在、无 console error。
- `reducedMotion: 'reduce'` → 断言不挂载 canvas（或 `data-scene="static"`）。
- 正常档 → 断言 canvas 存在、有 `aria-label`、无 pageerror。
- 截图：桌面/移动 × 深/浅 × full/static，共 8 张，落 `plans/证据/ui-v1/`。
- FPS 采样只在本地人工做一次（`requestAnimationFrame` 计数 5s），不作为 CI 门禁；低端设备实测结果写进证据文件。

---

## 8. 可发现性（SEO）

### 8.1 事实边界：哪些页面可索引

| 页面 | 可索引 | 原因 |
|---|---|---|
| 公开站 `/`、`/en/`、内容页 | 是 | 静态、无鉴权、内容对搜索有价值 |
| `/styleguide/` | 否 | 内部设计资产，`noindex` |
| 服务页 `/`（Python 状态页） | **否** | 服务端点，避免暴露部署形态；`X-Robots-Tag: noindex, nofollow` |
| 控制台 `/console` | **否** | 带 token 的运维界面；`X-Robots-Tag: noindex, nofollow` |
| `/404.html` | 否 | 默认 noindex |

公开站与 Python 服务的 `/` 是**两个不同的主机/路径**：公开站部署在静态托管，服务页在求解服务的 `:8878`。文档与部署说明必须写清这一点，避免 canonical 指错。

### 8.2 技术 SEO 清单

| 项 | 做法 | 验收 |
|---|---|---|
| 渲染方式 | 静态导出（SSG），无客户端数据依赖 | `out/**/*.html` 含完整正文 |
| title | 每页唯一，中文 ≤ 30 字 / 英文 ≤ 60 字符 | `test:content` 断言唯一与长度 |
| description | 每页唯一，80–160 字符，含主关键词 | `test:content` 断言长度与唯一 |
| canonical | 每页自指绝对 URL；`NEXT_PUBLIC_SITE_URL` 必填 | 构建后扫描 `<link rel="canonical">` |
| hreflang | `zh-CN` / `en` / `x-default` 三组互指 | 扫描每页三组链接，断言对偶 |
| sitemap | `app/sitemap.ts` 输出所有可索引路由，含 `lastModified` | `out/sitemap.xml` 可解析，URL 全部 200 |
| robots | `app/robots.ts` 允许全站，声明 sitemap | `out/robots.txt` 含 `Sitemap:` |
| 结构化数据 | `SoftwareApplication`、`FAQPage`、`BreadcrumbList`、`WebSite` | JSON 可解析；schema 字段完整 |
| Open Graph | `og:title/description/image/url/locale`；图 1200×630 | 每页标签齐全；图片尺寸校验 |
| Twitter | `summary_large_image` | 标签齐全 |
| 404 | 自定义 404，回首页/文档 | 访问不存在路径返回 404 页 |
| 重定向 | 不做 JS 重定向；语言切换是真实链接 | 无 `meta refresh` |
| URL | 小写、连字符、无参数、无尾部重复 | 路由表与 sitemap 一致 |
| 图片 | `alt` 描述、固定宽高、AVIF/WebP | axe 与 CLS 检查 |
| 内链 | 每页 3–5 条上下文内链 + 面包屑 | 链接检查无 404 |

`NEXT_PUBLIC_SITE_URL` 的处理：

- 生产构建必须设置；未设置时构建失败（`scripts/check-env.mjs`）。
- 本地预览允许 `SITE_ALLOW_PLACEHOLDER=1`，canonical 用 `http://localhost:3000`，并在页脚显示"本地预览"。
- 上线前扫描 `out/` 中是否残留 `localhost`/`example.com`，有则失败。

### 8.3 关键词矩阵

不提供搜索量数字：本项目没有接入付费关键词工具（预算红线为 0），编造 volume 会污染决策。下表只定义**意图 → 页面 → 放置位置**；量化在 Search Console 接入后回填。

| 层级 | 中文词 | 英文词 | 意图 | 目标页 | 放置 |
|---|---|---|---|---|---|
| 头部 | Cloudflare 过盾 | Cloudflare bypass | 商业/工具 | `/` | H1、title、description、正文首段 |
| 头部 | CF 挑战求解 | Cloudflare challenge solver | 商业/工具 | `/` | 副标题、正文解释、FAQ |
| 头部 | cf_clearance 获取 | cf_clearance cookie | 技术 | `/how-it-works/` | H2、代码段 |
| 腰部 | CF 五秒盾 | jsd challenge | 技术 | `/how-it-works/` | H2、机制段 |
| 腰部 | managed challenge 过盾 | managed challenge bypass | 技术 | `/how-it-works/` | H2、限制段 |
| 腰部 | Turnstile 求解 | Turnstile solver | 技术 | `/how-it-works/`、`/benchmarks/` | H2、数据表 |
| 腰部 | FlareSolverr 替代 | FlareSolverr alternative | 对比 | `/` | 对比卡片、FAQ |
| 腰部 | 无头浏览器 过盾 | headless browser Cloudflare | 对比 | `/how-it-works/` | 对比段 |
| 长尾 | Cloudflare 求解 API | Cloudflare solver API | 集成 | `/docs/` | H2、代码标题 |
| 长尾 | 纯协议 过盾 耗时 | pure protocol solver benchmark | 研究 | `/benchmarks/` | H2、数据表标题 |
| 长尾 | cf_clearance 多久过期 | cf_clearance expiry | 支持 | `/how-it-works/` FAQ | FAQ 问句 |
| 长尾 | Turnstile token 纯协议 | Turnstile token pure protocol | 研究 | `/how-it-works/` | H3、机制段 |

> 2026-11-06 用户选择更激进口径：H1/title 直接用 Bypass/过盾。风险是广告/支付平台政策；本版不做广告，保留免责声明与非关联声明。若要收回，改 `content/site.ts` 的 `home.h1` 与 `meta.home.title` 两处即可。

规则：

- 一页一个主词，不在一页里堆 10 个同义词。
- H1/title 用 "Bypass / 过盾"（用户 2026-11-06 拍板）；description 与正文保留 "Challenge Solver" 作为解释词。风险与回退见 §8.3。
- 中文词不直译英文词序；英文文案按母语写，不做机翻。
- 关键词表是 `site/seo/keywords.json` 的唯一来源；页面元数据从它生成，避免手写漂移。

### 8.4 内容策略：用证据建立可引用性

搜索之外，2026 年的发现入口还包括 AI 摘要与开发者社区。能被引用的内容有共同特征：**定义清楚、数字带日期、方法可复现、限制写在明面上**。

| 内容块 | 作用 | 更新 |
|---|---|---|
| `/benchmarks/` 的方法段 | 说明测了什么、没测什么、条件是什么 | 每次发版 |
| `/how-it-works/` 的机制段 | 解释 jsd/managed/turnstile 的差异与回落链 | 能力变化时 |
| `/security/` 的授权使用段 | 明确只做授权测试、不提供目标清单 | 法务/政策变化时 |
| `/changelog/` | 版本能力变化，链到 GitHub Release | 每次发版 |
| FAQ | 截取真实问题，答案 2–4 句 | 每月看 Issue |
| 对比段 | 与 FlareSolverr/无头浏览器做同口径对比 | 有实测才写 |

`llms.txt`（实验性）：在 `site/public/llms.txt` 放一段纯文本摘要 + 关键链接，方便 AI 爬虫读取。它不是标准，不承诺效果；上线后看日志再决定去留。

### 8.5 更新节奏

| 频率 | 动作 | 责任 |
|---|---|---|
| 每次发版 | 更新 `benchmarks.ts` 的数字与日期；更新 changelog 摘要；跑 `test:content` | 发版人 |
| 每月 | 看 Search Console 的查询/索引/CTR；把新问题补进 FAQ | 维护者 |
| 每季度 | 复核 `keywords.json` 的 `lastReviewed`；淘汰 cannibalized 页面；刷新 title/description | 维护者 |
| 每半年 | 复核 3D 性能与低端设备降级；复核依赖大版本 | 维护者 |

`site/seo/keywords.json` 带 `lastReviewed`；`scripts/check-seo-freshness.mjs` 在超过 120 天时让 CI 失败。这是"定期更新关键词"的可执行约束，不是口号。**已落地**：12 行意图矩阵，`test:seo` 与 CI 同步执行。

### 8.6 衡量与提交

| 工具 | 用途 | 授权状态 |
|---|---|---|
| Google Search Console | 索引、查询、CTR、Core Web Vitals | 需域名与验证，待授权 |
| Bing Webmaster Tools | 索引与关键词 | 待授权 |
| 百度搜索资源平台 | 中文收录 | 待授权 |
| Lighthouse CI | 实验室性能/SEO/A11y | 本地与 CI 可直接跑 |
| CrUX / Search Console CWV | 真实字段 | 需流量，待验证 |

默认不装第三方分析。若需要访问统计，优先 **Cloudflare Web Analytics**（托管在 Cloudflare Pages 时）或自托管 Umami；两者都会改 CSP 的 `script-src`/`connect-src`，必须单独评审，不能悄悄加。

### 8.7 反作弊红线

- 不做 cloaking（爬虫看到一套、用户看到另一套）。
- 不做关键词堆砌、隐藏文本、 doorway page。
- 不编造成功率、客户、评价、搜索量。
- 不用 `FAQPage` 标记不存在的问题。
- 不把 README 的结论复制成 10 个薄页面；每个页面必须有独立增量（方法、限制、对比、示例）。

### 8.8 与 GitHub README 的关系

README 是仓库的高权重入口，公开站是更深的解释层。两者不互为重复内容：

- README 保留快速开始与完整目录；公开站只放"为什么/怎么选/实测/限制"。
- 公开站每页链回 README 对应小节；README 在顶部加一个指向公开站的链接（Phase G 再做，属于跨仓库改动，需单独确认）。
- canonical 指向公开站自身；README 不需要 canonical。

---

## 9. 安全与合规

### 9.1 公开站威胁模型

| 面 | 风险 | 处置 |
|---|---|---|
| 静态 HTML/JS | 供应链注入、依赖漏洞 | lockfile + `npm ci` + audit + 构建期 CSP hash |
| 无用户输入 | 无表单/无登录/无上传 | 不引入表单；联系走 GitHub Issues |
| 外链 | 反向标签页劫持 | 外链 `rel="noopener noreferrer"`；新窗口用 `target="_blank"` 时同时加 rel |
| JSON-LD | `</script>` 注入 | `JSON.stringify` 后把 `<` 转成 `<` |
| 3D | 外部资源/着色器注入 | 不加载外部模型/纹理；几何体代码生成 |
| 部署头 | GitHub Pages 不支持自定义头 | 优先 Cloudflare Pages；GitHub Pages 用 meta CSP，记录限制 |
| 隐私 | 主题偏好写入 localStorage | 只存 `cf-theme`，不存标识；隐私页说明 |

公开站没有服务端、没有数据库、没有 cookie、没有账号。这是它"无漏洞"的最大结构优势；不要为了"在线 demo"把服务端加回来。

### 9.2 CSP：静态导出下的 hash 方案

`next.config.ts` 的 `headers()` 在 `output: 'export'` 下不支持（官方 Unsupported Features 明确列出 Headers/Rewrites/Redirects/Proxy/ISR/Server Actions）。因此 CSP 在**托管层**下发：

1. `npm run build` 产出 `out/`。
2. `scripts/gen-csp.mjs` 扫描 `out/**/*.html`，提取所有**可执行**内联 `<script>`（无 `src`，type 为空/`text/javascript`/`module`），计算 `sha256-<base64>`。
3. 生成 `out/_headers`：

```
/*
  Content-Security-Policy: default-src 'self'; script-src 'self' 'sha256-...' 'sha256-...'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; upgrade-insecure-requests
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=()
  Cross-Origin-Opener-Policy: same-origin
  Cross-Origin-Resource-Policy: same-origin
```

4. `scripts/check-csp.mjs` 反向校验：
   - `script-src` 不含 `unsafe-inline`/`unsafe-eval`；
   - 每个 HTML 内联脚本的 hash 都在 `_headers` 里；
   - 没有未列入的外链脚本/样式/字体；
   - `frame-ancestors 'none'`、`object-src 'none'`、`base-uri 'none'` 存在。

实测补丁：hash 源必须带单引号。`gen-csp.mjs` 现在输出 `'sha256-…'`；不带引号时 Chromium 报 "invalid source" 并拦掉所有内联脚本。修正后 17 个 HTML / 18 个 hash，0 报错。meta CSP 已注入每个 HTML；`_headers` 已生成。真实托管头未验证（按用户选择只做本地）。

已知代价：

- `style-src` 需要 `'unsafe-inline'`，因为 Next/R3F 会写内联 `style` 属性。Phase G 实测未收紧：R3F 的 canvas 容器必须内联样式；保留，并记录为已知代价（无用户可控内容）。
- GitHub Pages 忽略 `_headers`；只能用 `<meta http-equiv="Content-Security-Policy">`，且 meta 不支持 `frame-ancestors`/`report-uri`。部署未执行；若部署，首选支持 `_headers` 的托管（Cloudflare Pages）；GitHub Pages 作为只读镜像时，必须在文档写明这个限制。
- 本地 `next dev` 不经过 `_headers`；开发环境不是安全边界。

### 9.3 供应链

| 措施 | 具体 |
|---|---|
| 锁定 | `package-lock.json` 提交；`package.json` 运行时依赖用精确版本，不用 `^` |
| 安装 | CI 用 `npm ci`；若 `--ignore-scripts` 不影响构建则加上（Phase 0 验证），否则记录例外 |
| 审计 | `npm audit --audit-level=high`；高危阻断合并 |
| 依赖更新 | Dependabot 每周，分组；大版本人工评审 |
| 脚本权限 | 不在 `postinstall` 执行网络脚本；不引入来源不明的包 |
| Action 权限 | `permissions: contents: read`；第三方 Action 固定 SHA（至少固定大版本） |
| 产物 | 不产出 source map（`productionBrowserSourceMaps: false`）；`poweredByHeader: false` |

新增依赖清单（版本为 2026-11-06 npm 实测）：

| 包 | 版本 |
|---|---|
| `next` | `16.4.5` |
| `react` / `react-dom` | `19.3.1` |
| `motion` | `14.9.1` |
| `three` | `0.187.0` |
| `@react-three/fiber` | `9.8.2` |
| `tailwindcss` / `@tailwindcss/postcss` | `4.3.4` |
| `typescript` | `5.9.3`（最新 7.0.2 不在本版采用） |
| `eslint` | `10.14.0` |
| `eslint-config-next` | `16.4.5` |
| `@playwright/test` | `1.65.0` |
| `axe-core` | `4.14.0` |
| `@lhci/cli` | `0.16.0` |
| `@types/node` | `24.19.2` |
| `@types/react` / `@types/react-dom` | `19.3.0` |
| `@types/three` | `0.187.0` |

`@react-three/drei` 已移除：两个场景只需要 HTML 标签和一条连线，drei 带来的体积大于收益；标签改 DOM 覆盖层，连线改 mesh。`@axe-core/playwright`、`@next/bundle-analyzer`、`schema-dts` 未使用，已移除；`axe-core` 直接作为 devDependency 供 `verify:site` 注入。

TypeScript 固定 5.9.3 的原因：7.0.2 是新一代编译器，Next 16 生态（ESLint 插件、类型声明）尚未在本项目验证；先锁 5.9.3，升级作为独立任务。

### 9.4 数据与隐私

- 无 cookie、无登录、无分析默认。
- `localStorage` 只存 `cf-theme`（功能偏好）；不存用户标识、不存 token。
- 不做指纹采集；3D 能力探测只用 `navigator.deviceMemory`/`hardwareConcurrency`/`matchMedia`，不落盘、不上报。
- 隐私页说明：托管商可能记录访问日志（Cloudflare/GitHub 的标准日志），本项目不自建追踪。
- 公开站不接收任何求解凭证；示例代码里的 token 全部是占位符。

### 9.5 合规文案（产品口径下的必要边界）

- 页脚固定声明：`本项目仅用于安全研究与授权测试；使用者需遵守目标站点条款与当地法律。`
- 固定声明：`Not affiliated with Cloudflare, Inc. Cloudflare is a trademark of Cloudflare, Inc.`
- 不提供目标站点清单、不提供批量扫描入口、不提供绕过付费墙/访问控制的教程。
- "bypass/过盾" 是搜索词与能力描述，不是"帮助你未授权访问"的承诺；`/security/` 明确写授权边界。
- 若未来投放广告：广告平台对 "bypass" 类词敏感，落地页需改成 "challenge solver / research" 口径；本版不做广告，只做自然搜索。

### 9.6 现有控制台安全不变量（E-4，必须原样保留）

1. 零写入：无 `<form>`、无 POST、无改状态入口。
2. 零新依赖：HTML/CSS/JS 全在一个字符串常量里，无 npm/CDN。
3. token 保护：页面不含 token；`prompt()` 现问，只存 `sessionStorage`；DOM 不出现 token 值。
4. CSP：`default-src 'none'`；DOM 只用 `textContent`/`createElement`，禁止 `innerHTML`（清空表格的 `innerHTML = ""` 是既有例外，测试已锁）；无内联事件属性。
5. 数据脱敏：只渲染白名单字段；服务端健康视图的错误原文前端不读。

Phase F 的改动必须逐条对照这五条；新增测试只加不减。任何一条做不到，就退回"不改控制台"。

### 9.7 3D 与第三方资源

- 不加载外部模型、纹理、HDR、字体、分析脚本。
- 所有几何体在代码里生成；颜色来自令牌。
- `three`/R3F 的版本锁定；升级单独评审（shader/材质 API 变化可能影响画面）。
- 3D 代码用 Error Boundary 包裹：场景崩了只降级为静态图，不让整页白屏。

---

## 10. 施工计划

### 10.1 总览

| 阶段 | 交付物 | 工期 | 依赖 | 验证 |
|---|---|---|---|---|
| Phase 0 | `site/` 骨架、精确依赖、构建基线、CI 骨架 | 0.5 人日 | 决策已定 | `next build` 成功；记录初始 JS/LCP |
| Phase A | 令牌、双主题、字体、UI 原语、`/styleguide/` | 2 人日 | 0 | 对比度脚本全绿；6 张截图 |
| Phase B | 路由骨架、双语内容、metadata、sitemap/robots/JSON-LD/OG、404 | 3 人日 | A | `test:content` 全绿；`out/` 结构核对 |
| Phase C | 首页 + 5 个内容页的真实文案与数据 | 4 人日 | B | 内容审查；Playwright 路由/axe |
| Phase D | 动效层 + reduced-motion + CLS 测试 | 3 人日 | C | reduced-motion E2E；CLS < 0.05 |
| Phase E | 3D 场景 A/B + 三档降级 + 性能 | 5 人日 | C | WebGL 关闭/低端/reduced-motion 三路验收 |
| Phase F | 现有两页轻改 + 新增 10 个测试 | 2 人日 | 可与 C–E 并行 | 34 个单测；5 状态 E2E + 320/360；35/35 |
| Phase G | 全量验收、CSP 校验、Lighthouse、发布准备 | 2 人日 | 全部 | §11 验收矩阵全绿；证据落盘 |

合计约 **21.5 人日**（首版估算）。V1.1 已落地可验证子集；实际结果见 §16，未做的是部署、真机低端与母语审校。

### 10.2 Phase 0：骨架与基线

产物：

- `site/package.json`、`package-lock.json`、`tsconfig.json`、`next.config.ts`、`postcss.config.mjs`、`eslint.config.mjs`、`playwright.config.ts`、`lighthouserc.json`。
- `site/app/(zh)/layout.tsx`、`site/app/(zh)/page.tsx`、`site/app/(en)/en/layout.tsx`、`site/app/(en)/en/page.tsx`（占位）。
- `.github/workflows/site-ci.yml`（`paths: site/**`，不碰 Python CI）。
- 根 `.gitignore` 增加 `site/node_modules/`、`site/.next/`、`site/out/`、`site/test-results/`、`site/playwright-report/`。

关键配置：

```ts
// site/next.config.ts
import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  output: 'export',
  trailingSlash: true,
  images: { unoptimized: true },
  poweredByHeader: false,
  reactStrictMode: true,
  productionBrowserSourceMaps: false,
  basePath: process.env.SITE_BASE_PATH || '',
};

export default nextConfig;
```

注意：

- `output: 'export'` 下不能用 `headers()`/`rewrites()`/`redirects()`/middleware/Server Actions/ISR/默认图片优化。
- 多根布局：`app/(zh)/layout.tsx` 与 `app/(en)/en/layout.tsx` 各自渲染 `<html lang>`；语言切换触发整页加载，这是可接受的。
- 404：优先用 `experimental.globalNotFound` + `app/global-not-found.tsx`；若静态导出不支持，回退到 `scripts/gen-404.mjs` 把构建出的 `/404-preview/` 复制成 `out/404.html`，并把该预览页设为 `noindex`（待验证）。

验证：

- `npm ci && npm run build` 成功；`out/index.html`、`out/en/index.html` 存在。
- 记录初始 JS gzip、LCP、构建耗时，写进 `plans/证据/ui-v1/phase0-baseline.md`。
- 确认 `npm ci --ignore-scripts` 是否可行；不可行则记录原因与例外。

回滚：`site/` 是全新目录，删除即回滚；不影响 Python/Go。

### 10.3 Phase A：令牌、主题、字体、原语

产物：

- `site/styles/tokens.css`（primitive/semantic/component 三层）。
- `site/styles/globals.css`（Tailwind 4 `@import` + 基础样式 + reduced-motion）。
- `site/components/ui/`：Button、IconButton、Badge、Card、Container、Section、CodeBlock、Table、SkipLink。
- `site/components/theme/ThemeToggle.tsx`、`ThemeScript.tsx`。
- `site/public/fonts/`：Inter 子集、JetBrains Mono 子集 + OFL 文本。
- `site/app/(zh)/styleguide/page.tsx`（`noindex`，展示色板、字号、按钮、焦点态、深/浅切换）。

验证：

- `npm run test:tokens`：组件/页面无硬编码色值（白名单外）。
- `npm run test:contrast`：解析 `tokens.css`，输出每对前景/背景的比值；阈值 4.5（文本）/3（非文本）。
- Playwright 截图：`/styleguide/` 深/浅 × 桌面/移动 = 4 张；键盘 Tab 走一遍焦点环。
- axe 无 serious/critical。

Gate 1（你过目）：色板、字号、按钮/卡片质感、深/浅切换。默认推荐薄荷主色；备选紫罗兰主色（见 §14.3）。

### 10.4 Phase B：路由、双语、SEO 骨架

产物：

- 路由：§5.1 全部页面（占位文案）。
- `site/content/{types,zh,en}.ts`：字典与页面文案；键结构一致。
- `site/lib/metadata.ts`：title/description/canonical/hreflang/OG 生成器。
- `site/lib/routes.ts`：路由清单 + 语言对偶映射。
- `site/app/sitemap.ts`、`site/app/robots.ts`。
- `site/components/seo/JsonLd.tsx`（`<` 转义）。
- `site/scripts/og.mjs`：Playwright 渲染 1200×630 模板，输出 `public/og/zh.png`、`en.png`；CI 校验 PNG 头尺寸。
- `site/app/global-not-found.tsx` 或回退方案。
- `site/tests/content/*.test.mjs`：键对齐、title/description 唯一与长度、路由对偶、benchmark schema、来源路径存在。

验证：

- `npm run build`；检查 `out/sitemap.xml`、`out/robots.txt`、`out/en/.../index.html`。
- 扫描每页 canonical/hreflang/OG/JSON-LD；JSON-LD 用 `JSON.parse` 验证。
- `NEXT_PUBLIC_SITE_URL` 缺失时构建失败；`localhost` 残留扫描为 0。
- 语言切换：每个路由都能跳到对偶，且对偶 200。

Gate 2（你过目）：路由表、导航、每页 title/description、关键词落位。

### 10.5 Phase C：首页与内容页

产物：

- 首页 §5.2 的 10 个区块（3D 先留静态占位）。
- `/how-it-works/`：流水线 2D、回落链、失败模式、代码示例。
- `/benchmarks/`：方法、数据表、条件、限制、复现命令。
- `/docs/`：快速开始、API 链接、Go/Python/curl 示例。
- `/security/`：授权使用、数据处理、披露、免责声明、非关联声明。
- `/changelog/`：最近 3 个版本摘要。
- `site/data/benchmarks.ts`：所有数字 + 日期 + 来源 + 条件。
- `site/seo/keywords.json`：关键词矩阵 + `lastReviewed`。

验证：

- `test:content` 全绿（schema、日期、来源路径、标题长度、唯一性）。
- 每个数字在页面上有 `measuredAt` 与 `source`（DOM 断言）。
- 已知限制在首页与 `/benchmarks/` 都出现（文本断言）。
- Playwright：6 个路由 × 2 语言 = 12 页 200；无 console error；axe 0 serious。
- 链接检查：站内 404 = 0；外链只允许 GitHub/docs。

Gate 3（你过目）：文案口径、数字、限制段、CTA。

### 10.6 Phase D：动效层

产物：

- `site/components/motion/`：`Reveal`、`Stagger`、`CountUp`、`PageFade`。
- `site/components/nav/`：滚动背景、移动端 `<dialog>` 菜单。
- 按 §6.3 接入各区块；所有动效走令牌。
- reduced-motion 的 CSS + JS 双实现。

验证：

- `reducedMotion: 'reduce'`：canvas 不挂载、数字为最终值、transition 0s。
- CLS < 0.05；无 `pageerror`。
- 动画只改 transform/opacity（代码审查 + DevTools Performance 抽查）。
- 移动端 360/768 截图。

Gate 4（你过目）：动效视频/截图、节奏是否过慢、是否有"炫技但无用"的动画。

### 10.7 Phase E：3D 场景

产物：

- `site/components/three/SceneAChallengeWall.tsx`、`SceneBPipeline.tsx`。
- `site/components/three/SceneShell.tsx`：能力探测、三档、懒加载、Error Boundary、静态 SVG fallback。
- `site/components/three/fallback/`：两张静态 SVG（与场景同构图）。
- `site/components/three/data.ts`：从 `benchmarks.ts` 派生的场景数据。
- 节点 DOM 控制层（键盘可达）。

验证：

- `--disable-webgl`：`static` 档可见、表格存在、无 console error。
- `reducedMotion: 'reduce'`：不挂载 canvas。
- 正常档：canvas 有 `aria-label`，DOM 控制按钮可 Tab。
- bundle：3D chunk ≤ 260 KB gzip（实测 241.8 KB）；首屏 JS 不增加（实测 228.4 KB）。
- 本地 FPS 采样 5s（桌面/移动各一次），结果写证据；不作为 CI 门禁。
- 截图 8 张（桌面/移动 × 深/浅 × full/static）。

Gate 5（你过目）：3D 观感、性能、降级是否等价、是否过度。

### 10.8 Phase F：现有两页轻改

产物（全部在 `solver/` 与 `tests/`，不碰 `site/`）：

- `solver/ui_tokens.py`：共享令牌 CSS。
- `solver/statuspage.py`、`solver/console_html.py`：注入令牌、wedged 修正、焦点、移动端、favicon、theme-color、noindex、120ms 过渡、reduced-motion。
- `solver/service.py`：两处响应加 `X-Robots-Tag`。
- `tests/test_console.py`、`tests/test_statuspage.py`：各新增 5 个测试（§4.6），合计 10 个。
- `scripts/verify_ui_e2e.py`：增加 320/360 宽度与焦点检查（可只加一个状态，避免脚本膨胀）。

验证：

- `pytest tests/test_console.py tests/test_statuspage.py -q`：34 个单测全绿。
- `python scripts/verify_ui_e2e.py`：5 状态全绿；截图落 `plans/证据/ui-v1/`。
- 320/360/768 宽度无横向溢出。
- CSP 原样不变；`grep` 确认无新外部资源。

Gate 6（你过目）：状态页/控制台截图、wedged 徽章、手机宽度。

### 10.9 Phase G：验收与发布准备

产物：

- `plans/证据/ui-v1/`：Phase 0 基线、对比度输出、Lighthouse 报告、axe 报告、截图、CSP 校验输出、Python/Go/UI 测试输出。
- `site/README.md`：本地构建、预览、验证命令、部署说明（部署本身待授权）。
- 发布清单：域名、托管、Search Console、Bing/百度、分析（可选）。

验证：

- §11 验收矩阵逐项通过。
- `npm run verify:csp`、`npm run test:content`、`npm run test:e2e`、`npm run lighthouse` 全绿。
- Python 1021 / Go 46 / UI 34 / E2E 35/35 全绿。
- `git status` 只包含预期文件；不自动 commit/push。

发布动作（需你授权）：创建托管项目、绑定域名、设置 DNS、提交 sitemap、开启 Search Console/Bing/百度。授权前只做本地静态构建，不对外发布。

---

## 11. 验收矩阵

| ID | 验收项 | 方法 | 通过标准 | 证据落点 |
|---|---|---|---|---|
| AC-01 | 双语路由完整 | 构建后遍历 `out/` | 12 个可索引页面 + 404 均存在；`lang` 正确 | `plans/证据/ui-v1/routes.txt` |
| AC-02 | title/description 唯一 | `test:content` | 无重复；长度在范围内；含主词 | CI 日志 |
| AC-03 | canonical/hreflang | 构建后扫描 HTML | 每页 canonical 自指；三组 hreflang 互指 | `seo-scan.txt` |
| AC-04 | sitemap/robots | 解析 `out/sitemap.xml`、`out/robots.txt` | sitemap 覆盖全部可索引路由；robots 声明 sitemap | `seo-scan.txt` |
| AC-05 | JSON-LD | `JSON.parse` + 字段断言 | `SoftwareApplication`/`FAQPage`/`BreadcrumbList`/`WebSite` 合法 | `jsonld.txt` |
| AC-06 | OG/Twitter | 扫描 meta + PNG 头 | 1200×630；每页标签齐全 | `og-check.txt` |
| AC-07 | Lighthouse SEO | Lighthouse CI | 100 | `lighthouse/` |
| AC-08 | Performance | Lighthouse CI | 移动 ≥ 90，桌面 ≥ 95；LCP ≤ 2.0s；CLS ≤ 0.05；TBT ≤ 200ms | `lighthouse/` |
| AC-09 | 初始 JS / 3D chunk | zlib gzip + chunk 扫描 | 初始 ≤250KB（实测 228.4KB）；3D ≤260KB（实测 241.8KB） | `bundle.json` |
| AC-10 | axe | `axe-core`（`verify:site` 注入） | serious/critical = 0 | `axe.json` |
| AC-11 | 键盘 | Playwright Tab 序列 | 所有交互可达；焦点环可见；无陷阱 | `keyboard.md` |
| AC-12 | reduced-motion | Playwright `reducedMotion: 'reduce'` | 无 canvas；无过渡；数字为终值 | `reduced-motion.md` |
| AC-13 | 3D 降级 | Playwright `--disable-webgl` | static 档 + 等价表格；无 console error | `3d-fallback.md` |
| AC-14 | 运行时错误 | Playwright 监听 | 12 页无 `pageerror`/`console.error` | CI 日志 |
| AC-15 | CSP | `scripts/check-csp.mjs` | script-src 无 unsafe-inline/eval；内联脚本 hash 全覆盖 | `csp.txt` |
| AC-16 | 供应链 | `npm audit` | high/critical = 0；lockfile 存在 | CI 日志 |
| AC-17 | 现有 UI | `pytest` + `verify_ui_e2e.py` | 34 个单测全绿；wedged 6.47:1；35/35 E2E + 320/360 截图 | `plans/证据/ui-v1/` |
| AC-18 | 全量回归 | `pytest`、`go test` | Python 1021 passed / 7 skipped、Go 46 全绿 | CI 日志 |
| AC-19 | 内容准确性 | 代码审查 + `grep` | 页面组件无硬编码性能数字；数字均有日期/来源 | 审查记录 |
| AC-20 | 合规文案 | 文本断言 | 免责声明、非关联声明、授权边界三处存在 | `content-check.txt` |
| AC-21 | 静态导出 / 404 / OG | `npm run build` + 产物扫描 | `out/404.html`、`sitemap.xml`、`robots.txt`、两张 OG PNG；`force-static` 生效 | `site-verify.json` |
| AC-22 | 浏览器 CSP | `verify:site` | 17 HTML / 18 hash；0 CSP 报错 | `site-verify.json` |
| AC-23 | 3D 帧率 / 降级 | SwiftShader + CPU 节流 | 1x/4x/10x 60fps；20x 21.8fps 或 static；reduced/no-WebGL static | `3d-fps.json` |
| AC-24 | Lighthouse | LHCI desktop/mobile | 桌面 100/100/100/100；移动 98/100/100/100 | `lighthouse-summary.json` |
| AC-25 | axe | `verify:site` | 14 路由 + 404 = 15 页，0 违规 | `site-verify.json` |
| AC-26 | 全量回归 | pytest / go test | Python 1021 passed / 7 skipped；Go ok | `python-tests.txt`、`go-tests.txt` |

---

## 12. 风险与回滚

| ID | 风险 | 概率/影响 | 缓解 | 回滚 |
|---|---|---|---|---|
| R-01 | Next 16 静态导出与多根布局/404/OG 的组合有未验证边界 | 中/中 | **已验证可用**：多根布局 + `globalNotFound` 产出 `out/404.html`；`sitemap/robots/opengraph-image` 必须 `force-static`；OG 用 `ImageResponse` 可用 | 仍保留单一根布局回退方案 |
| R-02 | CSP hash 在每次构建后变化，遗漏即白屏 | 中/高 | **已验证**：hash 必须带单引号；双脚本 + Chromium 0 报错；CI 阻断 | 不退回 `unsafe-inline`；构建失败即停 |
| R-03 | 3D 在低端设备掉帧/发热 | 高/中 | **已验证到合成环境**：SwiftShader 1x/4x/10x 60fps，20x 21.8fps 或 static；移动端直接 static；真机未测 | 关闭 3D，保留静态 SVG |
| R-04 | Node 工具链污染 Python 仓库 | 低/中 | 独立 `site/`；独立 CI；根 `.gitignore`；不把 Node 依赖写进 Python 流程 | 删除 `site/` 与 `site-ci.yml` |
| R-05 | 域名/托管未授权，站点无法上线 | 高/低 | **用户选择只本地**；本地静态构建 + 预览；部署清单待授权；真实头未验证 | 只交付 `out/` 与文档 |
| R-06 | "bypass/过盾" 关键词触发平台/支付政策 | 中/中 | 标题用 solver；bypass 放正文；免责声明；不做广告投放 | 切换为研究口径（§14.3） |
| R-07 | 公开数字与 README/CHANGELOG 漂移 | 中/高 | `benchmarks.ts` 单一来源；发版时更新；`test:content` 校验来源路径 | 回退到上一版 `benchmarks.ts` |
| R-08 | 字体许可/子集出错 | 低/低 | 只用 OFL 字体；OFL 文本随文件；子集脚本固定 | 回退系统字体栈 |
| R-09 | Playwright 截图在不同机器有像素差 | 中/低 | CI 固定 Linux + 固定浏览器版本；截图只做人工审阅，不做像素断言 | 取消截图门禁，保留 axe/路由断言 |
| R-10 | Next/React/R3F 大版本升级破坏 | 中/中 | 精确版本；Dependabot 只提 PR；升级单独任务 | 锁回上一组版本 |
| R-11 | 内容被指"过度承诺" | 中/高 | 已知限制必显；每个数字带条件；不写成功率 | 下线对应段落，保留表格 |

---

## 13. 明确不做

- 不改 Python 服务语义、不改 API 契约、不改缓存/回落链。
- 不把 React/motion/3D 塞进 `/` 或 `/console`；E-4 五条不变量不动。
- 不做在线真实求解、不做账号/支付/额度/API key。
- 不做原生 App、不做 Service Worker、不做离线缓存。
- 不做 CMS、不做博客后台、不做多语言 > 2。
- 不做全站 3D、不做 3D 首屏阻塞、不做自动旋转/粒子雨/滚动劫持。
- 不引入第三方字体/图标/分析/广告 CDN。
- 不编造搜索量、成功率、客户、评价、资产拓扑。
- 不把 `/` 状态页做成可索引的营销页。
- 不为"看起来高级"增加无数据支撑的图表。

---

## 14. 决策状态与待授权

### 14.1 已定（2026-11-06）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 范围 | 公开站 + 控制台轻改 |
| 2 | 技术栈 | Next.js App Router（16.4.5，静态导出） |
| 3 | 3D | 多个场景；本次 2 个真 3D + 1 个 2D |
| 4 | 口径/语言 | 产品口径 + 中英双语；H1/title 直接用 Bypass/过盾 |
| 5 | 验证环境 | 只本地静态服务 + 节流 |
| 6 | 搜索量 | 只做意图矩阵，不写 volume |
| 7 | 英文文案 | 模型撰写 + 人工审阅清单 |

### 14.2 默认假设（可推翻，不阻塞开工）

| 项 | 默认 | 理由 |
|---|---|---|
| 样式方案 | Tailwind 4.3.4 + CSS 变量 | 与 Next 16 生态匹配；只产用到的类 |
| 字体 | Inter + JetBrains Mono 自托管子集 | OFL；无第三方请求；体积可控 |
| 中文默认路径 | `/`；英文 `/en/` | 静态导出无 middleware；避免 `/zh/` 重复内容 |
| 404 | `globalNotFound` 优先，postbuild 回退 | 多根布局下没有单一 layout |
| 部署 | **本地-only（用户 2026-11-06 选择）** | 不启用 Pages；真实头未验证；若要上线，优先支持 `_headers` 的托管 |
| 分析 | 默认无；需要时再评审 | 隐私与 CSP 成本 |
| 联系 | GitHub Issues | 无表单、无邮件服务 |
| 文档 | 链接仓库 `docs/`，站内只做入口页 | 不维护第二份事实源 |
| 内容语言 | 英文模型撰写 + `EN-REVIEW` 审阅清单；未母语签字 | 可立即交付，但地道度有上限；已在 §14.5 披露 |

### 14.3 还需要你拍板的设计选项

| 选项 | A（推荐） | B | C | 取舍 |
|---|---|---|---|---|
| 主色 | 电光薄荷 `#4DE3C1` | 紫罗兰 `#A78BFA` | 保留 GitHub 蓝 `#58A6FF` | 薄荷最像"信号/凭证"，与 CF 的橙色形成差异；紫罗兰更潮但更像通用 SaaS；保留蓝最稳但无品牌记忆 |
| Hero 3D 位置 | 右侧独立画布（不挡文字） | 全屏背景（文字压在上面） | 第二屏才出现 | 右侧对 LCP/可读性最稳；全屏最沉浸但首屏风险高；第二屏最保守但浪费首屏 |
| 部署 | Cloudflare Pages | GitHub Pages | 先不部署，只出 `out/` | Cloudflare 能下发 CSP 头；GitHub Pages 零成本但只能 meta CSP；不部署最稳但无法验收 SEO |
| 文档形态 | 站内入口页 + 链 GitHub | 站内 MDX 全量迁移 | 只链 GitHub | 入口页成本低、不漂移；MDX 体验好但维护双份；只链最省但跳出率高 |
| 分析 | 无 | 自托管 Umami | Cloudflare Web Analytics | 无最隐私；Umami 要维护服务；CF Analytics 免费但改 CSP |

V1.1 实施采用 **A + A + C + A + A**（主色薄荷、右侧 Hero 3D、只本地不部署、文档入口页、无分析）；这是用户 2026-11-06 "只本地"选择的落地组合。其余 A 选项未单独拍板，按 A 执行；要改按上表回退。

### 14.4 需要授权的外部动作

| 动作 | 需要的授权 | 影响 |
|---|---|---|
| 新增 Node 生产依赖（Next/motion/three/R3F 等） | 已执行；版本见 §9.3 | 已进入 `site/package.json` 与 lockfile |
| 部署到 Cloudflare Pages / GitHub Pages | **未授权**（用户选择只本地） | 公开可访问 |
| 绑定自定义域名 | 域名所有权/DNS | canonical/OG/hreflang 的最终值 |
| Search Console / Bing / 百度验证 | 账号与 DNS/HTML 验证 | 收录与关键词数据 |
| 分析工具 | 账号/服务 | 改 CSP 与隐私页 |
| GitHub README 加公开站链接 | 仓库写权限 | 跨仓库改动，单独确认 |
| 发版/commit/push | 用户 2026-11-06 明确要求 | 已执行 commit/push；部署仍未授权 |

### 14.5 未做的验证（披露）

- 真实部署头（`_headers`、HSTS、`frame-ancestors` 响应头）未验证：用户选择只本地；GitHub Pages 未启用、Cloudflare 无 token。
- 真实低端设备 FPS 未测；只有 Chromium + SwiftShader 的合成数据（`3d-fps.json`）。
- 搜索量、竞争度未测；`keywords.json` 只有意图矩阵（用户选择）。
- 英文母语审校未做；`site/content/EN-REVIEW.md` 待签字。
- `npm ci --ignore-scripts` 未测；`npm ci` 正常。
- `create-next-app` 未用；站点为手动骨架，已验证。
- 公开站真实索引未做；部署后提交 sitemap。

---

## 15. 文档自检

本文件写作后执行过的校验：

| 检查 | 命令 | 结果 |
|---|---|---|
| 现有页面体量 | `wc -l -c solver/statuspage.py solver/console_html.py` | 484/23,442；476/22,627 |
| 令牌/颜色计数 | `grep -oE -- '--[a-z0-9-]+:'` / `grep -oE 'color:'` | 各 18 条令牌；24/17 处 `color:` |
| 动效计数 | `grep -c '@keyframes'` / `grep -c 'transition'` | 两页均 0/0 |
| UI 测试数 | `grep -c 'def test_' tests/test_console.py tests/test_statuspage.py` | 12 + 12 |
| 对比度 | Node WCAG 脚本（附录 A.4） | 现有令牌全达 AA；wedged 3.35 不达；新色板全达 |
| 依赖版本 | `npm view <pkg> version` | 见 §9.3 |
| 静态导出边界 | `curl` Next.js 官方 `static-exports.mdx` | Headers/Rewrites/Redirects/Proxy/ISR/Server Actions/默认图片优化不支持 |
| 多根布局 | `curl` 官方 `route-groups.mdx` | 支持多 root layout；跨布局导航整页加载 |
| 仓库状态 | `gh repo view` | public；`homepageUrl` 为空 |
| 文档链接 | 计划内路径标注为"计划新增"，未冒充已存在文件 | — |

自检结论：本文件中的现状数字均可由仓库命令复现；计划新增的文件全部标注了路径与阶段，没有把"计划"写成"已完成"。

---

## 16. 实施记录（V1.1，2026-11-06）

### 16.1 实际交付

| 面 | 文件 | 说明 |
|---|---|---|
| 公开站骨架 | `site/` | Next 16.4.5 静态导出；14 个可访问路由 + `out/404.html` |
| 视觉/主题 | `site/app/globals.css`、`site/content/site.ts` | Deep Signal 令牌；深/浅；中英内容 |
| 动效 | `site/components/Reveal.tsx`、`CodeBlock.tsx`、`SiteHeader.tsx` | `motion` + 原生锚点（静态导出取舍） |
| 3D | `site/components/three/*`、`SceneShell.tsx` | 2 个 R3F 场景；DOM 标签；三档降级 |
| SEO | `site/lib/metadata.ts`、`jsonld.ts`、`sitemap.ts`、`robots.ts`、`opengraph-image.tsx` | canonical/hreflang/JSON-LD/OG |
| CSP | `site/scripts/gen-csp.mjs`、`check-csp.mjs` | meta + `_headers`；hash 带引号 |
| 验收 | `site/scripts/verify-site.mjs`、`tests/content/content.test.mjs` | 路由/axe/降级/内容 |
| 现有 UI | `solver/ui_tokens.py`、`statuspage.py`、`console_html.py`、`service.py` | 共享令牌、wedged、焦点、noindex |
| CI | `.github/workflows/site-ci.yml` | `site/**` 独立门禁 |

### 16.2 验证结果

| 门禁 | 命令 | 结果 |
|---|---|---|
| 静态构建 | `cd site && npm run build` | 20 条静态路由；`out/404.html` 存在 |
| CSP | `npm run csp && npm run verify:csp` | 17 HTML / 18 hash；无 `unsafe-inline`/`eval` |
| 路由/axe/降级 | `npm run verify:site` | 14 路由；axe 0；reduced/no-WebGL = static；404 title/noindex |
| 内容 | `npm run test:content` | 7/7 |
| 令牌/对比度/SEO | `test:tokens` / `test:contrast` / `test:seo` | 34 文件 0 硬编码色；文本 ≥4.5；12 行 0 天 |
| Lighthouse 桌面 | `lhci autorun --config=lighthouserc.json` | 100/100/100/100；LCP 0.3s、CLS 0、TBT 0ms |
| Lighthouse 移动 | `lhci autorun --config=lighthouserc.mobile.json` | 98/100/100/100；LCP 1.2/1.1s、TBT 150/140ms |
| 包体 | `zlib.gzipSync(level=9)` | 初始 228.4 KB；3D 241.8 KB；CSS 6.7 KB |
| 3D 帧率 | SwiftShader + CPU 节流 | 1x/4x/10x 60fps；20x 21.8fps 或 static |
| Python 全量 | `python -m pytest -q` | 1021 passed, 7 skipped |
| UI 单测 | `pytest tests/test_console.py tests/test_statuspage.py -q` | 34 passed |
| UI E2E | `python scripts/verify_ui_e2e.py` | 35/35 |
| Go | `go test ./...` | ok 6.428s |

证据落 `plans/证据/ui-v1/`（`site-verify.json`、`3d-fps.json`、`bundle.json`、`lighthouse-summary.json`、测试摘要与 320/360 截图）。

### 16.3 预算修正

- 初始 JS 实测 228.4 KB，首版估 150 KB；3D chunk 实测 241.8 KB，首版估 220 KB；CSS 6.7 KB。主因是 Next 16 + React 19 + motion 的框架基线与 three.js 体积。预算改为 250 / 260 KB，Lighthouse 仍为桌面 100、移动 98。
- 移动端改 static 后 TBT 150/140ms，桌面 0ms；首版 200ms 上限保留。

### 16.4 与计划的偏差

- OG 用 `ImageResponse` 静态生成，而不是 Playwright 截图；在 `dynamic = 'force-static'` 下可用。
- 站点导航用原生 `<a>`，不用 Next `Link`；原因是静态导出下 RSC 预取 404 + 离开 3D 页面时 `removeChild`。
- 移除 `@react-three/drei`：3D chunk 只降 4.7 KB（three 主导），但依赖面更小。
- 移动端（`pointer: coarse`）直接 static，不是 lite；TBT 优先。
- 第三个 3D 场景不做，用 2D 图表替代；数据里没有可支撑的第三条关系。
- Python 两页不加 favicon；既有"无 `<link>`/无外部资源"不变量优先，公开站单独有 favicon。

### 16.5 剩余缺口

- 真实部署头、真实低端设备、搜索量、英文母语审校未完成（§14.5）。
- `site/package-lock.json` 已提交。CI 已跑通：`site-ci` run `42147228247`（Install/Lint/Typecheck/Build/CSP/Content/Browser/verify-site/Lighthouse/Upload 全绿）。首次 run `42146655742` 因 CI 无沙箱导致 Lighthouse 失败，已用 `--no-sandbox` 修复（commit `77558b7`）。

### 16.6 交付记录

| 项 | 值 |
|---|---|
| 功能提交 | `bd63629` feat(site): 公开站 V1.1 + 现有 UI 可访问性收口 |
| CI 修复 | `77558b7` fix(site-ci): Lighthouse 在无沙箱 CI 环境加 --no-sandbox |
| CI 结果 | `site-ci` run `42147228247` 全绿 |
| 推送 | `origin/master` |

---

## 附录 A：实测命令与原始输出

### A.1 页面体量

```text
$ wc -l -c solver/statuspage.py solver/console_html.py
  484  23442 solver/statuspage.py
  476  22627 solver/console_html.py

$ # 内联 CSS 行数（<style> 到 </style>）
statuspage.py: 58
console_html.py: 50

$ # 令牌声明 / color: 使用
statuspage.py: 18 / 24
console_html.py: 18 / 17

$ # 动效
statuspage.py: @keyframes 0, transition 0, @media 1
console_html.py: @keyframes 0, transition 0, @media 1
```

### A.2 CSP 原文（`solver/service.py`）

```text
default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline';
connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none';
frame-ancestors 'none'
```

### A.3 依赖版本（2026-11-06 npm 实测）

```text
next                   16.4.5
react                  19.3.1
react-dom              19.3.1
motion                 14.9.1
three                  0.187.0
@react-three/fiber     9.8.2
tailwindcss            4.3.4
@tailwindcss/postcss   4.3.4
typescript             5.9.3 (latest 7.0.2, not used)
eslint                 10.14.0
eslint-config-next     16.4.5
@playwright/test       1.65.0
axe-core               4.14.0
@lhci/cli              0.16.0
@types/node            24.19.2
@types/react           19.3.0
@types/react-dom       19.3.0
@types/three           0.187.0
```

### A.4 对比度计算

现有页面（深色）：

```text
ink on bg          15.67
muted on bg         6.29
ok on bg            5.49
warn on bg          7.33
bad on bg           5.52
accent on bg        7.33
wedged #fff on #f85149  3.35  ← 不达 AA
```

现有页面（浅色）：

```text
ink on bg          14.84
muted on bg         4.93
ok on bg            4.77
warn on bg          4.57
bad on bg           5.03
accent on bg        4.88
```

新色板（Deep Signal）：

```text
dark  ink 17.59 / muted 9.04 / primary 12.47 / secondary 7.36 / success 11.50 / warn 12.00 / danger 7.44 / info 7.88
light ink 17.42 / muted 5.08 / primary 4.59 / secondary 5.27 / success 4.67 / warn 4.58 / danger 5.85 / info 6.24
dark button #04120E on #4DE3C1 11.91
light button #FFFFFF on #0B7F6B 4.93
focus dark #4DE3C1 on #06080D 12.47
focus light #0B7F6B on #F4F7FB 4.59
wedged fix #FFFFFF on #B91C1C 6.47
```

### A.5 Next.js 静态导出不支持项（官方原文，2026-11-06 抓取）

```text
Dynamic Routes with dynamicParams: true
Dynamic Routes without generateStaticParams()
Route Handlers that rely on Request
Cookies
Rewrites
Redirects
Headers
Proxy
Incremental Static Regeneration
Image Optimization with the default loader
Draft Mode
Server Actions
Intercepting Routes
```

结论：CSP/安全头必须在托管层（`_headers`）下发；`next.config.ts` 的 `headers()` 不可用。

---

## 附录 B：计划新增/修改文件索引

### 新增（`site/`）

```text
site/package.json
site/package-lock.json
site/next.config.ts
site/tsconfig.json
site/postcss.config.mjs
site/eslint.config.mjs
site/playwright.config.ts
site/lighthouserc.json
site/styles/tokens.css
site/styles/globals.css
site/app/(zh)/layout.tsx
site/app/(zh)/page.tsx
site/app/(zh)/how-it-works/page.tsx
site/app/(zh)/benchmarks/page.tsx
site/app/(zh)/docs/page.tsx
site/app/(zh)/security/page.tsx
site/app/(zh)/changelog/page.tsx
site/app/(zh)/styleguide/page.tsx
site/app/(en)/en/layout.tsx
site/app/(en)/en/page.tsx
site/app/(en)/en/how-it-works/page.tsx
site/app/(en)/en/benchmarks/page.tsx
site/app/(en)/en/docs/page.tsx
site/app/(en)/en/security/page.tsx
site/app/(en)/en/changelog/page.tsx
site/app/sitemap.ts
site/app/robots.ts
site/app/global-not-found.tsx
site/components/ui/*
site/components/theme/*
site/components/motion/*
site/components/nav/*
site/components/three/*
site/components/seo/JsonLd.tsx
site/content/types.ts
site/content/zh.ts
site/content/en.ts
site/lib/metadata.ts
site/lib/routes.ts
site/data/benchmarks.ts
site/seo/keywords.json
site/scripts/gen-csp.mjs
site/scripts/check-csp.mjs
site/scripts/check-env.mjs
site/scripts/check-seo-freshness.mjs
site/scripts/og.mjs
site/tests/content/*.test.mjs
site/tests/e2e/*.spec.ts
site/public/fonts/Inter-Variable-subset.woff2
site/public/fonts/Inter-OFL.txt
site/public/fonts/JetBrainsMono-subset.woff2
site/public/fonts/JetBrainsMono-OFL.txt
site/public/og/zh.png
site/public/og/en.png
site/public/llms.txt
site/README.md
```

### 新增（根）

```text
.github/workflows/site-ci.yml
计划书/CF-Bypass-Research-视觉交互与可发现性设计指南-V1.md
plans/证据/ui-v1/            （验收证据）
```

### 修改（现有）

```text
solver/ui_tokens.py          新增：共享令牌 CSS
solver/statuspage.py         注入令牌、wedged、焦点、移动端、noindex、favicon、theme-color
solver/console_html.py       同上
solver/service.py            两处响应加 X-Robots-Tag
tests/test_console.py        追加 5 个测试
tests/test_statuspage.py     追加 5 个测试
scripts/verify_ui_e2e.py     追加 320/360 宽度与焦点检查
.gitignore                   增加 site/ 构建产物
```

---

## 附录 C：术语

| 术语 | 含义 |
|---|---|
| cf_clearance | Cloudflare 挑战通过后下发的 cookie，绑定域名 + 出口 IP + UA |
| jsd | Cloudflare 的 JS 挑战（俗称"五秒盾"） |
| managed challenge | 由 CF 判定是否展示交互的托管挑战 |
| Turnstile | Cloudflare 的验证码产品，可纯协议求解 |
| cfsolve | 本项目的 Rust 纯协议求解器（JS VM + 环境快照回放） |
| E-4 | `plans/README.md` 中"只读控制台：单文件、零构建、零写入、token 保护、CSP"的决策 |
| LCP / CLS / INP | Core Web Vitals：最大内容绘制 / 累积布局偏移 / 交互到下一次绘制 |
| R3F | `@react-three/fiber`，React 的 three.js 渲染器 |
| 静态导出 | `output: 'export'`，构建为纯静态文件，无 Node 运行时 |
| hash CSP | 用内联脚本内容的 SHA-256 白名单替代 `unsafe-inline` |