# ui-v1 验收证据

日期：2026-11-06
范围：公开站 `site/` + 现有 `/` 与 `/console` 的 UI 轻改
口径：**已验证**（本机实际运行）/ **静态确认**（读代码）/ **未验证**（缺环境或按用户选择未做）

## 结果摘要

| 门禁 | 命令 | 结果 | 证据 |
|---|---|---|---|
| 静态构建 | `cd site && npm run build` | 20 条静态路由；`out/404.html` 存在 | 本文件 |
| CSP 生成/校验 | `npm run csp && npm run verify:csp` | 17 个 HTML、18 个内联脚本 hash；`script-src` 无 `unsafe-inline`/`unsafe-eval` | `site-verify.json` |
| 站点验收 | `npm run verify:site` | 14 路由、axe 0、reduced-motion static、no-WebGL static、404 title/noindex | `site-verify.json` |
| 内容一致性 | `npm run test:content` | 7/7 | 命令输出 |
| 令牌/对比度/SEO | `npm run test:tokens` / `test:contrast` / `test:seo` | 34 文件 0 硬编码色；文本 ≥4.5，焦点 ≥3；12 行关键词，0 天未复核 | 命令输出 |
| Lighthouse 桌面 | `npx lhci autorun --config=lighthouserc.json` | Perf/A11y/BP/SEO = 100/100/100/100；LCP 0.3s、CLS 0、TBT 0ms | `lighthouse-summary.json` |
| Lighthouse 移动 | `npx lhci autorun --config=lighthouserc.mobile.json` | 98/100/100/100；LCP 1.2s/1.1s、TBT 150/140ms | `lighthouse-summary.json` |
| 包体 | `zlib.gzipSync(level=9)` | 初始 JS 228,360 B；3D chunk 241,826 B；CSS 6,669 B | `bundle.json` |
| 3D 降级/帧率 | Chromium + SwiftShader + CPU 节流 | 1x/4x/10x = 60fps；20x = 21.8fps 或自动 static；reduced/no-WebGL = static | `3d-fps.json` |
| Python 全量 | `python -m pytest -q` | 1021 passed, 7 skipped | `python-tests.txt` |
| UI 单测 | `python -m pytest tests/test_console.py tests/test_statuspage.py -q` | 34 passed（24 基线 + 10 新增） | `ui-unit.txt` |
| UI E2E | `python scripts/verify_ui_e2e.py` | 31/31 PASS | `ui-e2e.txt` |
| Go | `go test ./...` | ok 6.428s | `go-tests.txt` |
| CI | GitHub Actions `site-ci` run `42147228247` | 全绿（含 Lighthouse desktop） | https://github.com/lza6/CF-Bypass-Research/actions/runs/42147228247 |

## 关键实现结论（写进计划书的增量）

1. `output: 'export'` 下 `sitemap.ts`、`robots.ts`、`opengraph-image.tsx` 都必须显式 `export const dynamic = 'force-static'`，否则构建直接失败。**已验证**。
2. 多根布局（`app/(zh)/layout.tsx` + `app/(en)/en/layout.tsx`）+ `experimental.globalNotFound` 在静态导出下可用，产出 `out/404.html`，`lang` 分别为 `zh-CN` / `en`。**已验证**。
3. CSP hash 源必须写成带单引号的 `'sha256-…'`。不带引号时浏览器把整条源判为 invalid，内联脚本全部被拦；修正后 Chromium 0 报错。**已验证**。
4. Next `Link` 在静态导出下会请求不存在的 `__next.*.txt`（RSC 预取 404），且离开含 R3F Canvas 的页面时触发 `removeChild` 运行时错误。改用原生 `<a>` 后，导航为整页加载，0 404、0 pageerror。**已验证**；这是静态站的取舍。
5. 移动端（`pointer: coarse`）直接走 `static` 档，不加载 WebGL；桌面才加载 3D。移动 TBT 从 230ms 降到 150/140ms。**已验证**。
6. `@react-three/drei` 移除；标签改 DOM 覆盖层，连线改 mesh。3D chunk 从 246.5 KB 降到 241.8 KB（three 本身是主要体积）。**已验证**。
7. Python 两页的共享令牌、wedged `#B91C1C`（6.47:1）、`:focus-visible`、`prefers-reduced-motion`、noindex 头已落地；favicon 没有加进这两页，因为既有不变量禁止 `<link>`/外部资源，公开站单独有 favicon。**已验证**。

## 未验证（按用户选择或环境限制）

| 项 | 原因 | 下一步 |
|---|---|---|
| 真实部署头（`_headers`、HSTS、`frame-ancestors` 响应头） | 用户选择“只本地静态服务”，GitHub Pages 未启用、Cloudflare 无 token | 部署时用 `curl -I` 复验；GitHub Pages 只能 meta CSP，`frame-ancestors` 不生效 |
| 真实低端设备 FPS | 本机无低端机 | 用真机跑一次 `plans/证据/ui-v1/3d-fps.json` 同口径采样 |
| 搜索量/竞争度 | 预算红线为 0，未接付费工具 | 上线后 Search Console 回填；`seo/keywords.json` 只保留意图矩阵 |
| 英文母语审校 | 无母语审校资源 | 按 `site/content/EN-REVIEW.md` 人工过一遍并签字 |
| 公开站真实索引 | 未部署 | 部署后提交 sitemap，观察 Search Console/Bing/百度 |

## 复现顺序

```bash
cd site
npm ci
npm run build
npm run csp
npm run verify:csp
npm run test:content && npm run test:tokens && npm run test:contrast && npm run test:seo
python -m http.server 3100 --directory out &
SITE_BASE_URL=http://127.0.0.1:3100 npm run verify:site
npx lhci autorun --config=lighthouserc.json
npx lhci autorun --config=lighthouserc.mobile.json

cd ..
python -m pytest -q
python -m pytest tests/test_console.py tests/test_statuspage.py -q
python scripts/verify_ui_e2e.py
cd gocfsolve && go test ./...
```