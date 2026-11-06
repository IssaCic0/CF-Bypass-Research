"""只读页面共享的视觉令牌（`GET /` 与 `GET /console`）。

**为什么单独一个模块**：两个页面此前各自内联了同一份 9 令牌 × 深浅两块的调色板，
一共 18 条声明逐字重复。改一次主色要改两处，漂移时没人拦得住。这里把
"令牌 + 基础排版 + 焦点 + reduced-motion + 小屏修正"收敛成一份，两页在模块加载时
用 `__UI_TOKENS__` 占位符注入。

**约束不变**：仍然是零构建、零新依赖、单文件 HTML、CSP 白名单。这里只换 CSS 文本，
不引入任何外部资源；favicon 走 `img-src 'self' data:` 已允许的 data URI。
"""

from __future__ import annotations

#: 状态页与控制台共用的令牌与基础规则。缩进按 2 空格，与页面内联 CSS 对齐。
TOKENS_CSS = """  :root {
    --bg: #0f1419; --panel: #171d24; --line: #2a333d; --ink: #e6edf3;
    --muted: #8b98a5; --ok: #2ea043; --warn: #d29922; --bad: #f85149; --accent: #58a6ff;
    /* wedged 徽章的实心底色：白字压 #B91C1C 为 6.47:1，压 #f85149 只有 3.35:1 */
    --bad-solid: #b91c1c;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f8fa; --panel:#fff; --line:#d0d7de; --ink:#1f2328; --muted:#656d76;
            --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; --accent:#0969da; --bad-solid:#b91c1c; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--ink);
         font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  :focus-visible { outline:2px solid var(--accent); outline-offset:2px; border-radius:4px; }
  button { transition: background-color 120ms ease, border-color 120ms ease, color 120ms ease; }
  @media (max-width: 480px) {
    body { padding:12px; }
    header { gap:8px; }
    input[type=password], input[type=text] { min-width:0; max-width:100%; }
    #audit, #inflight-table { overflow-x:auto; }
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { transition-duration:.01ms !important; animation-duration:.01ms !important; }
  }
"""

