# CF Solver public site

Static product site for `CF-Bypass-Research`. Next.js App Router, static export,
Framer-style motion (`motion`), and two WebGL scenes with a static fallback.

This directory is independent of the Python solver. It never calls `/solve`,
`/detect`, `/health`, or any other service endpoint. It is an entry point plus a
dated evidence surface; the repository `docs/` remains the source of truth.

## Stack

| Item | Version |
|---|---|
| Next.js | 16.4.5 |
| React / React DOM | 19.3.1 |
| motion | 14.9.1 |
| three / @react-three/fiber | 0.187.0 / 9.8.2 |
| Tailwind CSS | 4.3.4 |
| TypeScript | 5.9.3 |

`@react-three/drei` was removed: the scenes only needed HTML labels and one line
primitive, and the dependency pulled more than it saved. Labels are DOM overlays;
connectors are mesh boxes.

## Commands

```bash
npm ci
npm run dev            # local dev
npm run build          # static export to out/
npm run preview        # python -m http.server 3000 --directory out
npm run lint
npm run typecheck
npm run test:content   # locale parity, title/description limits, benchmark schema, keyword freshness
npm run test:tokens    # no hardcoded colors outside the token / 3D whitelist
npm run test:contrast  # WCAG contrast for dark/light tokens
npm run test:seo       # keyword rows + 120-day freshness
npm run csp            # add CSP meta to every HTML, write out/_headers
npm run verify:csp     # reject unsafe-inline/eval, missing hashes, external resources
npm run verify:site    # Playwright routes, hreflang, JSON-LD, axe, reduced-motion, no-WebGL
npm run lighthouse     # desktop preset
```

`verify:site` expects a server at `SITE_BASE_URL` (default `http://127.0.0.1:3100`).
Set `CHROME_PATH` to a Chromium binary if Playwright's own browser is not installed.
It writes `../plans/证据/ui-v1/site-verify.json`.

## Environment

| Variable | Use |
|---|---|
| `NEXT_PUBLIC_SITE_URL` | Canonical/OG/hreflang origin. Defaults to `http://localhost:3000` for local builds; set it before any public build. |
| `SITE_BASE_PATH` | Sub-path hosting (for example a GitHub Pages project path). Empty by default. |
| `SITE_BASE_URL` | Base URL used by `verify:site`. |
| `CHROME_PATH` | Chromium executable for Playwright/Lighthouse when not installed by the package. |

## Security model

- Static export: no server, no cookies, no accounts, no form, no third-party scripts.
- CSP is generated after the build. `scripts/gen-csp.mjs` hashes every inline script
  and injects a meta policy into each HTML file, then writes `out/_headers` for hosts
  that support response headers.
- `script-src` is `'self'` plus `'sha256-…'` hashes, with single quotes. An unquoted
  hash is ignored by the browser and blocks every inline script; `verify:csp` checks
  for the quoted form.
- `style-src` keeps `'unsafe-inline'` because Next/R3F write inline style attributes.
  No user-controlled content reaches the DOM.
- The two Python pages (`GET /`, `GET /console`) keep their own CSP and their
  "no `<link>`, no external resource" invariant. A favicon was deliberately not added
  there; the public site has one.

## Deployment status

The site is verified locally only. GitHub Pages is not enabled, and no Cloudflare
Pages token is configured. That means the hosting-specific header behaviour
(`_headers`, HSTS, `frame-ancestors` as a response header) is **not** verified.
GitHub Pages would ignore `_headers`; if it is used, the meta CSP is the only
policy and `frame-ancestors` is not enforced. Prefer a host that supports response
headers.

## Content rules

- Every number on `/benchmarks/` comes from `data/benchmarks.ts`, with `measuredAt`,
  `conditions`, and `source`. Do not hardcode a number in a page component.
- Known limitations stay visible: managed challenge replay can return the challenge
  page; the 2026-11-03 script has a stage2 blocker; clearance is bound to the egress IP.
- English copy is model-written. `content/EN-REVIEW.md` is the human review sheet; it
  has not been signed off by a native speaker.
- No search-volume numbers are claimed. `seo/keywords.json` is an intent matrix;
  Search Console backfill is pending.

## Verification snapshot (2026-11-06)

| Gate | Result |
|---|---|
| `npm run build` | 20 static routes, `out/404.html` present |
| `npm run verify:csp` | 17 HTML files, 18 inline-script hashes |
| `npm run test:content` | 7/7 |
| `npm run test:tokens` | 34 files, 0 hardcoded colors |
| `npm run test:contrast` | dark/light text ≥ 4.5, focus ≥ 3 |
| `npm run verify:site` | 14 routes, axe 0, reduced-motion static, no-WebGL static |
| Lighthouse desktop | Perf 100 / A11y 100 / BP 100 / SEO 100; LCP 0.3s, CLS 0, TBT 0ms |
| Lighthouse mobile | Perf 98 / A11y 100 / BP 100 / SEO 100; LCP 1.2s/1.1s, TBT 150/140ms |
| Initial JS gzip | 228.4 KB |
| 3D chunk gzip | 241.8 KB |
| CSS gzip | 6.7 KB |

The initial-JS and 3D-chunk numbers exceed the first draft's 150/220 KB estimates.
The measured framework baseline (Next 16 + React 19 + motion) and three.js make those
caps unattainable without dropping the chosen stack; the budgets were revised to
250/260 KB with no regression allowed.

## Known gaps

- Real low-end hardware was not tested. The 3D fallback was exercised with Chromium's
  SwiftShader software renderer and CPU throttling; see `plans/证据/ui-v1/3d-fps.json`.
- Hosting headers were not tested because deployment was intentionally left local.
- Search volumes were not purchased or scraped; only the intent matrix exists.
- The English copy has not had a native-speaker review.