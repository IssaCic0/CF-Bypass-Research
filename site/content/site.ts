export type Locale = 'zh' | 'en';
export type PageKey = 'home' | 'how' | 'benchmarks' | 'docs' | 'security' | 'changelog' | 'styleguide';

export type NavKey = Exclude<PageKey, 'styleguide'>;

export type Dict = {
  locale: Locale;
  htmlLang: string;
  meta: Record<NavKey, { title: string; description: string }>;
  nav: {
    home: string;
    how: string;
    benchmarks: string;
    docs: string;
    security: string;
    changelog: string;
    language: string;
    theme: string;
    github: string;
    menu: string;
  };
  home: {
    h1: string;
    lede: string;
    ctaPrimary: string;
    ctaSecondary: string;
    metricsTitle: string;
    metrics: { value: string; label: string; note: string }[];
    capabilitiesTitle: string;
    capabilities: { title: string; body: string }[];
    pipelineTitle: string;
    pipelineLede: string;
    stages: { name: string; body: string }[];
    sceneTitle: string;
    sceneLede: string;
    limitationsTitle: string;
    limitations: string[];
    faqTitle: string;
    faqs: { q: string; a: string }[];
    codeTitle: string;
    codeLede: string;
    ctaTitle: string;
    ctaBody: string;
  };
  how: {
    title: string;
    lede: string;
    sections: { title: string; body: string }[];
    codeTitle: string;
    codeLede: string;
  };
  benchmarks: {
    title: string;
    lede: string;
    methodTitle: string;
    method: string[];
    limitationsTitle: string;
    limitations: string[];
    tableTitle: string;
  };
  docs: {
    title: string;
    lede: string;
    quickstartTitle: string;
    quickstart: string[];
    endpointsTitle: string;
    endpoints: { path: string; body: string }[];
    linksTitle: string;
    links: { label: string; href: string }[];
  };
  security: {
    title: string;
    lede: string;
    sections: { title: string; body: string }[];
  };
  changelog: {
    title: string;
    lede: string;
    items: { version: string; date: string; body: string }[];
  };
  styleguide: {
    title: string;
    lede: string;
    paletteTitle: string;
    typeTitle: string;
    componentsTitle: string;
  };
  footer: {
    disclaimer: string;
    trademark: string;
    updated: string;
    license: string;
  };
};

export const SITE = {
  name: 'CF Solver',
  repo: 'https://github.com/lza6/CF-Bypass-Research',
  license: 'MIT',
  owner: 'lza6',
} as const;

export const PAGES: Record<PageKey, { zh: string; en: string; index: boolean; nav: boolean }> = {
  home: { zh: '/', en: '/en/', index: true, nav: true },
  how: { zh: '/how-it-works/', en: '/en/how-it-works/', index: true, nav: true },
  benchmarks: { zh: '/benchmarks/', en: '/en/benchmarks/', index: true, nav: true },
  docs: { zh: '/docs/', en: '/en/docs/', index: true, nav: true },
  security: { zh: '/security/', en: '/en/security/', index: true, nav: true },
  changelog: { zh: '/changelog/', en: '/en/changelog/', index: true, nav: true },
  styleguide: { zh: '/styleguide/', en: '/en/styleguide/', index: false, nav: false },
};

export const NAV_KEYS: NavKey[] = ['home', 'how', 'benchmarks', 'docs', 'security', 'changelog'];

const zh: Dict = {
  locale: 'zh',
  htmlLang: 'zh-CN',
  meta: {
    home: {
      title: 'Cloudflare 过盾 — CF Solver｜缓存热路径 <3ms',
      description:
        '纯协议优先的 Cloudflare 过盾服务：一次求解，30 分钟内回放；缓存热路径 p99 2.93ms；支持 cf_clearance、Turnstile token 与 route=auto。仅用于授权测试。',
    },
    how: {
      title: '工作原理 — CF Solver｜检测、求解、凭证、回放',
      description:
        '从 33 厂商特征检测到 cfsolve / pydoll / Camoufox / FlareSolverr 回落链，再到 cf_clearance 与同 UA 回放，逐段说明每一步的真实行为与失败模式。',
    },
    benchmarks: {
      title: '实测数据 — CF Solver｜每个数字都有日期与条件',
      description:
        '数据表覆盖同站 A/B 18.43s、缓存热路径 p99 2.93ms、无盾站 0.55s 放行与测试基线；每个数字标注日期、条件、来源与已知限制，不写搜索量与成功率。',
    },
    docs: {
      title: '文档 — CF Solver｜快速开始与 API',
      description:
        '本地启动、/solve、/detect、/health、/status、/logs、/stats、/metrics 的入口；Go、Python、curl 示例；完整 API 链接到仓库 docs。',
    },
    security: {
      title: '安全与授权使用 — CF Solver',
      description:
        '仅用于安全研究与授权测试。说明授权边界、数据处理、披露方式、与 Cloudflare, Inc. 的非关联声明与免责声明；不提供目标清单、批量扫描入口或绕过访问控制的教程。',
    },
    changelog: {
      title: '版本记录 — CF Solver｜6.3.0',
      description:
        '6.3.0 出口实测与流量录制、6.2.2 看门狗、6.2.0 YesCaptcha 兼容层与 V1 契约；每个版本的真实能力变化与已知限制。完整历史见仓库 CHANGELOG.md。',
    },
  },
  nav: {
    home: '首页',
    how: '原理',
    benchmarks: '实测',
    docs: '文档',
    security: '安全',
    changelog: '版本',
    language: 'English',
    theme: '切换深浅主题',
    github: 'GitHub',
    menu: '打开菜单',
  },
  home: {
    h1: 'Cloudflare 过盾：把挑战变成可复放的凭证',
    lede: '一次求解，30 分钟内用同一对 cookie + UA 回放；缓存热路径 p99 2.93ms。纯协议 cfsolve 打头，pydoll / Camoufox / FlareSolverr 按序兜底。',
    ctaPrimary: '在本地跑起来',
    ctaSecondary: '读 API',
    metricsTitle: '三个能复核的数字',
    metrics: [
      { value: '18.43s', label: '同站 A/B 最快', note: 'cfsolve 纯协议 · 2026-11-01' },
      { value: '2.93ms', label: '缓存热路径 p99', note: '5589 QPS · 2026-11-03' },
      { value: '0.55s', label: '无盾站放行', note: 'route=auto · 0 次求解' },
    ],
    capabilitiesTitle: '它解决什么',
    capabilities: [
      {
        title: '一次求解，30 分钟回放',
        body: 'cf_clearance 与域名 + 出口 IP + UA 绑定。服务保存这一对，回放不再启动浏览器；热路径零 Python 参与。',
      },
      {
        title: 'route=auto 不白等',
        body: '33 厂商特征预检测：无盾直接过，不支持的站直接拒绝，已识别的盾自动分发到对应后端。',
      },
      {
        title: '多进程共享缓存',
        body: 'SQLite/WAL 单文件缓存跨进程共享；失败摘除 2s、回归 28s；403 自愈重解一次，调用方无感。',
      },
    ],
    pipelineTitle: '一条真实的回落链',
    pipelineLede: '不是拓扑图，是调用顺序。每个节点都可以在 /docs 找到对应字段；鼠标或键盘聚焦可看说明。',
    stages: [
      { name: 'Domain', body: '目标域名与 URL；先用 /detect 判断有没有盾。' },
      { name: 'Detect', body: '33 厂商特征；无盾站直接放行，不认识的目标拒绝。' },
      { name: 'cfsolve', body: 'Rust 纯协议：JS VM + 环境快照回放，无浏览器进程。' },
      { name: 'pydoll', body: 'CDP 直连 Chrome，零 WebDriver；纯协议不适用时的第一层浏览器。' },
      { name: 'Camoufox', body: 'C++ 层指纹篡改的 Firefox；代价是启动慢、内存高。' },
      { name: 'FlareSolverr', body: '最后兜底；兼容既有生态。' },
      { name: 'Token', body: 'cf_clearance + UA 成对返回；绑定出口 IP。' },
      { name: 'Replay', body: '同 UA、同 cookie、同出口 IP；缓存命中直接回放。' },
    ],
    sceneTitle: '把流水线放进空间里',
    sceneLede: '这是装饰性 3D 场景，真实信息在旁边的文本列表里；WebGL 不可用、低端设备或 reduced-motion 时自动退化为静态图。',
    limitationsTitle: '已知限制（必须读）',
    limitations: [
      'managed challenge 的回放仍可能是挑战页：2026-11-04 udemy 实测求解成功 18.32s / 462B，但 Python 与 Go 回放都返回挑战页；本版不声称所有 managed challenge 都能回放。',
      '2026-11-03 的新挑战脚本：反混淆与 46 字段 payload 已打穿，stage2 响应程序分析未收敛；回落链会自动落 Camoufox。',
      'clearance 绑定出口 IP：代理池登记值与实测值不一致会静默 403；先用 /detect 与出口实测校验。',
      '可用性站点相关：steamdb / udemy / quora 对当前 persona 不弹挑战，glassdoor.com 会国家跳转失败。',
    ],
    faqTitle: '常见问题',
    faqs: [
      {
        q: '纯协议为什么行 / 不行？',
        a: '现场编造环境不行：CF 校验的是环境真实性，22 项能力缺 12 项。回放真实浏览器录制的环境快照可行：cfsolve 同站 A/B 18.43s，快于 Camoufox 19.56s。',
      },
      {
        q: '和 FlareSolverr 有什么区别？',
        a: 'FlareSolverr 是浏览器兜底。本项目纯协议优先，失败才回落浏览器，并把缓存与回放做成主路径；热路径不需要 Python 参与。',
      },
      {
        q: '需要浏览器吗？',
        a: '--pure-only 是 72MB、无浏览器进程的模式；其他模式按回落链启动，只有纯协议失败才起浏览器。',
      },
      {
        q: '缓存多久？',
        a: '默认 30 分钟窗口；--prewarm 可以在过期前主动续期，把贵操作挪出请求路径。',
      },
      {
        q: '失败了怎么办？',
        a: '403 自愈会重解一次；仍失败则按回落链切换后端。每次尝试都写入 /health 的 attempts 轨迹。',
      },
    ],
    codeTitle: '在本地跑起来',
    codeLede: '下面是本地调用，不是在线 demo。公开站不持有任何求解凭证，也不提供在线求解入口。',
    ctaTitle: '先读证据，再决定要不要跑',
    ctaBody: '实测数字、失败模式、API 契约都在仓库里。公开站只做入口，不复制事实源。',
  },
  how: {
    title: '工作原理：检测 → 求解 → 凭证 → 回放',
    lede: '每一步都能在仓库里找到实现与实测；这里给的是链路和失败模式，不是营销话术。',
    sections: [
      {
        title: '1. 检测：先判断有没有盾',
        body: '33 厂商特征；无盾站 0.55s 放行、0 次求解。已识别的盾自动分发到对应后端；不支持的站直接拒绝，避免白等 45s。',
      },
      {
        title: '2. 求解：纯协议优先',
        body: 'cfsolve 用 JS VM + 录制环境快照回放，同站 A/B 18.43s，快于 Camoufox 19.56s。纯协议失败才按 pydoll → Camoufox → FlareSolverr 回落。',
      },
      {
        title: '3. 凭证：cf_clearance + UA 成对保存',
        body: 'cf_clearance 与域名 + 出口 IP + UA 绑定。服务保存 cookie + UA 成对值；UA 不进缓存键，回放用这一对。实测 463B。',
      },
      {
        title: '4. 回放：同 UA、同 cookie、同出口',
        body: '缓存命中热路径零 Python 参与：5589 QPS / p99 2.93ms / 0 错。回放 200、1.3–1.7s；二次调用 cached=true。',
      },
      {
        title: '5. 失败模式：写清楚，不藏',
        body: 'managed challenge 回放可能是挑战页（udemy 2026-11-04）；新脚本 stage2 未收敛；出口 IP 不匹配会静默 403；可用性站点相关。',
      },
    ],
    codeTitle: '三个最小调用',
    codeLede: 'curl、Go、Python 都是本地调用；参数与响应以 docs/openapi.json 为准。',
  },
  benchmarks: {
    title: '实测数据：日期、条件、来源',
    lede: '没有搜索量、没有成功率承诺。每个数字都能追到一次真实运行；限制与失败样本同样列出。',
    methodTitle: '方法',
    method: [
      '同站 A/B：同一目标、同一时间窗，比较 cfsolve / Camoufox / pydoll。',
      '缓存热路径：离线压测，Go 客户端直接回放，不启动 Python。',
      'route=auto：真机 example.com，无盾站直接放行。',
      '所有数字来自本机真实运行；日期与来源在表格里。',
    ],
    limitationsTitle: '这些数字不代表什么',
    limitations: [
      '不代表所有站点都能过：可用性站点相关，managed challenge 回放仍有失败样本。',
      '不代表任意网络都成立：clearance 绑定出口 IP，代理质量会改变结果。',
      '不代表长期稳定：CF 会轮换挑战脚本，2026-11-03 的新脚本仍有 stage2 卡点。',
    ],
    tableTitle: '数据表',
  },
  docs: {
    title: '文档入口',
    lede: '站内只做入口；完整 API、字段、错误码在仓库 docs/ 与 openapi.json。',
    quickstartTitle: '快速开始',
    quickstart: [
      '.\\run.ps1 start  # 首次启动自动生成 token 并写入 ./.env',
      'curl http://127.0.0.1:8878/health  # 期望 state=ready',
      'python run.py e2e  # 端到端：求解 → 回放 → 二次调用 cached=true',
    ],
    endpointsTitle: '端点',
    endpoints: [
      { path: 'POST /solve', body: '求解；want=clearance / turnstile；route=auto 自动探测。' },
      { path: 'GET /detect', body: '只探测防护类型，不求解。' },
      { path: 'GET /health', body: '状态、缓存、研究资产；带 token 时含更多字段。' },
      { path: 'GET /status', body: '在途请求与最近事件；需要 token。' },
      { path: 'GET /logs', body: '审计 JSONL 尾部；需要 --audit-log。' },
      { path: 'GET /stats', body: '计数与延迟。' },
      { path: 'GET /metrics', body: 'Prometheus 文本格式。' },
      { path: 'GET /console', body: '只读控制台；带 token。' },
    ],
    linksTitle: '仓库里的完整文档',
    links: [
      { label: 'docs/API.md', href: 'https://github.com/lza6/CF-Bypass-Research/blob/master/docs/API.md' },
      { label: 'docs/openapi.json', href: 'https://github.com/lza6/CF-Bypass-Research/blob/master/docs/openapi.json' },
      { label: 'README.md', href: 'https://github.com/lza6/CF-Bypass-Research' },
    ],
  },
  security: {
    title: '安全与授权使用',
    lede: '这是一个安全研究工具，不是“帮你未授权访问”的服务。边界写在这里，使用前先读。',
    sections: [
      {
        title: '授权边界',
        body: '仅用于你拥有或获得明确授权的系统；遵守目标站点条款与当地法律。项目不提供目标清单、不提供批量扫描入口、不提供绕过付费墙或访问控制的教程。',
      },
      {
        title: '数据处理',
        body: '公开站是静态站：无账号、无 cookie、无表单、无第三方分析。主题偏好只写 localStorage。求解服务本身的数据处理见仓库 docs/API.md。',
      },
      {
        title: '披露',
        body: '安全问题请通过 GitHub Issues 或仓库公开的联系方式报告；不要附带真实凭证、目标数据或未脱敏日志。',
      },
      {
        title: '非关联声明',
        body: '本项目与 Cloudflare, Inc. 无关联。Cloudflare 是 Cloudflare, Inc. 的商标，仅作描述性使用。',
      },
      {
        title: '风险',
        body: 'CF 会轮换挑战脚本；clearance 绑定出口 IP；managed challenge 回放仍有失败样本。任何“成功率”都必须带日期、条件与来源。',
      },
    ],
  },
  changelog: {
    title: '版本记录',
    lede: '只列能力变化，不列营销数字。完整变更见仓库 CHANGELOG.md。',
    items: [
      {
        version: '6.3.0',
        date: '2026-11-05',
        body: '出口 IP 实测校验、审计日志可看、真机流量录制、出口进 UI。',
      },
      {
        version: '6.2.2',
        date: '2026-11-05',
        body: '看门狗：worker 卡死判定 wedged，进程以 75 退出，supervisor 拉起干净进程。',
      },
      {
        version: '6.2.0',
        date: '2026-11-05',
        body: 'YesCaptcha 兼容层 19 种 task type + 生态错误码词表；主链路 7 种 + 嫁接层 12 种。',
      },
    ],
  },
  styleguide: {
    title: '内部样式指南',
    lede: '色板、字号、按钮、焦点态、深/浅主题。此页不索引。',
    paletteTitle: '色板',
    typeTitle: '排版',
    componentsTitle: '组件',
  },
  footer: {
    disclaimer: '仅用于安全研究与授权测试；使用者需遵守目标站点条款与当地法律。',
    trademark: '与 Cloudflare, Inc. 无关联。Cloudflare 是 Cloudflare, Inc. 的商标。',
    updated: '最后更新',
    license: 'MIT License',
  },
};

const en: Dict = {
  locale: 'en',
  htmlLang: 'en',
  meta: {
    home: {
      title: 'Cloudflare Bypass — CF Solver | <3ms cached replay',
      description:
        'Cloudflare challenge solver: solve once, replay for 30 minutes; p99 2.93ms cached. cf_clearance, Turnstile, route=auto. Authorized testing only.',
    },
    how: {
      title: 'How it works — CF Solver | Detect, solve, token, replay',
      description:
        'From 33-vendor detection to the cfsolve / pydoll / Camoufox / FlareSolverr chain, then cf_clearance and same-UA replay. Failure modes and limits.',
    },
    benchmarks: {
      title: 'Benchmarks — CF Solver | Dated and conditioned',
      description:
        'Same-site A/B 18.43s, cached hot path p99 2.93ms, no-shield pass 0.55s. All numbers come from real local runs, with date, conditions, source, and known limits.',
    },
    docs: {
      title: 'Docs — CF Solver | Quickstart and API',
      description:
        'Local start, /solve, /detect, /health, /status, /logs, /stats, /metrics entry points; Go, Python, and curl examples; full API links to the repository docs.',
    },
    security: {
      title: 'Security and authorized use — CF Solver',
      description:
        'For security research and authorized testing only. Authorization boundaries, data handling, disclosure, non-affiliation, and disclaimer.',
    },
    changelog: {
      title: 'Changelog — CF Solver | 6.3.0',
      description:
        '6.3.0 egress verification and traffic capture, 6.2.2 watchdog, 6.2.0 YesCaptcha compatibility and V1 contract. Real capability changes per release.',
    },
  },
  nav: {
    home: 'Home',
    how: 'How it works',
    benchmarks: 'Benchmarks',
    docs: 'Docs',
    security: 'Security',
    changelog: 'Changelog',
    language: '中文',
    theme: 'Toggle theme',
    github: 'GitHub',
    menu: 'Open menu',
  },
  home: {
    h1: 'Cloudflare Bypass: turn challenges into replayable clearance',
    lede: 'Solve once, replay for 30 minutes with the same cookie + UA pair; p99 2.93ms on the cached hot path. Pure-protocol cfsolve first, then pydoll / Camoufox / FlareSolverr.',
    ctaPrimary: 'Run it locally',
    ctaSecondary: 'Read the API',
    metricsTitle: 'Three numbers you can check',
    metrics: [
      { value: '18.43s', label: 'Fastest same-site A/B', note: 'cfsolve pure protocol · 2026-11-01' },
      { value: '2.93ms', label: 'Cached hot-path p99', note: '5589 QPS · 2026-11-03' },
      { value: '0.55s', label: 'No-shield pass', note: 'route=auto · 0 solves' },
    ],
    capabilitiesTitle: 'What it solves',
    capabilities: [
      {
        title: 'Solve once, replay for 30 minutes',
        body: 'cf_clearance is bound to domain + egress IP + UA. The service stores that pair; replay no longer starts a browser, and the hot path has zero Python.',
      },
      {
        title: 'route=auto does not wait blindly',
        body: '33-vendor detection: no shield passes directly, unsupported targets are rejected, and known shields are dispatched to the right backend.',
      },
      {
        title: 'Shared multi-process cache',
        body: 'A single SQLite/WAL cache is shared across processes; failure eviction is 2s and return is 28s; a 403 heals with one re-solve.',
      },
    ],
    pipelineTitle: 'A real fallback chain',
    pipelineLede: 'Not a topology graph — the actual call order. Every node maps to a field in /docs; hover or focus to read the note.',
    stages: [
      { name: 'Domain', body: 'Target domain and URL; call /detect first to see whether a shield exists.' },
      { name: 'Detect', body: '33 vendor signatures; no-shield sites pass, unknown targets are rejected.' },
      { name: 'cfsolve', body: 'Rust pure protocol: JS VM + recorded environment replay, no browser process.' },
      { name: 'pydoll', body: 'CDP straight to Chrome, no WebDriver; the first browser layer when pure protocol does not fit.' },
      { name: 'Camoufox', body: 'Firefox with C++-level fingerprint changes; the cost is slow startup and high memory.' },
      { name: 'FlareSolverr', body: 'Last-resort fallback; keeps ecosystem compatibility.' },
      { name: 'Token', body: 'cf_clearance + UA returned as a pair; bound to the egress IP.' },
      { name: 'Replay', body: 'Same UA, same cookie, same egress IP; a cache hit replays directly.' },
    ],
    sceneTitle: 'Put the pipeline in space',
    sceneLede: 'This is a decorative 3D scene; the real information is in the text list beside it. Without WebGL, on low-end devices, or under reduced-motion, it falls back to a static image.',
    limitationsTitle: 'Known limits (must read)',
    limitations: [
      'Managed challenge replay can still return the challenge page: on 2026-11-04, udemy solved in 18.32s / 462B, but both Python and Go replay returned the challenge page. This release does not claim all managed challenges replay.',
      'The 2026-11-03 challenge script: deobfuscation and the 46-field payload are through, but stage2 response analysis has not converged; the fallback chain drops to Camoufox.',
      'Clearance is bound to the egress IP: a mismatch between the registered and measured proxy IP is a silent 403. Verify with /detect and an egress check first.',
      'Availability is site-dependent: steamdb / udemy / quora do not challenge the current persona, and glassdoor.com fails on a country redirect.',
    ],
    faqTitle: 'FAQ',
    faqs: [
      {
        q: 'Why does pure protocol work — or not?',
        a: 'Fabricating an environment on the spot does not work: Cloudflare checks environment authenticity, and 12 of 22 capabilities are missing. Replaying a recorded real-browser environment does: cfsolve is 18.43s in the same-site A/B, faster than Camoufox at 19.56s.',
      },
      {
        q: 'How is this different from FlareSolverr?',
        a: 'FlareSolverr is a browser fallback. This project is pure-protocol first, falls back to a browser only on failure, and makes cache + replay the main path; the hot path does not involve Python.',
      },
      {
        q: 'Do I need a browser?',
        a: '--pure-only is a 72MB mode with no browser process. Other modes start the fallback chain and only launch a browser when pure protocol fails.',
      },
      {
        q: 'How long is the cache?',
        a: 'A 30-minute window by default; --prewarm renews before expiry and moves the expensive work out of the request path.',
      },
      {
        q: 'What happens on failure?',
        a: 'A 403 heals with one re-solve; if that still fails, the fallback chain switches backends. Every attempt is recorded in the /health attempts trace.',
      },
    ],
    codeTitle: 'Run it locally',
    codeLede: 'These are local calls, not an online demo. The public site holds no solver credentials and offers no online solve entry.',
    ctaTitle: 'Read the evidence, then decide whether to run it',
    ctaBody: 'Benchmarks, failure modes, and the API contract live in the repository. This site is an entry point, not a second source of truth.',
  },
  how: {
    title: 'How it works: detect → solve → token → replay',
    lede: 'Every step maps to code and a real run in the repository. This page gives the chain and the failure modes, not marketing copy.',
    sections: [
      {
        title: '1. Detect: is there a shield?',
        body: '33 vendor signatures; a no-shield site passes in 0.55s with 0 solves. Recognized shields are dispatched to the right backend; unsupported targets are rejected instead of waiting 45s.',
      },
      {
        title: '2. Solve: pure protocol first',
        body: 'cfsolve replays a recorded environment in a JS VM: 18.43s in the same-site A/B, faster than Camoufox at 19.56s. Only on failure does it fall back to pydoll → Camoufox → FlareSolverr.',
      },
      {
        title: '3. Token: cf_clearance + UA as a pair',
        body: 'cf_clearance is bound to domain + egress IP + UA. The service stores the cookie + UA pair; UA is not part of the cache key, but is used for replay. Measured at 463B.',
      },
      {
        title: '4. Replay: same UA, same cookie, same egress',
        body: 'A cache hit has zero Python on the hot path: 5589 QPS / p99 2.93ms / 0 errors. Replay returns 200 in 1.3–1.7s; a second call is cached=true.',
      },
      {
        title: '5. Failure modes: written down, not hidden',
        body: 'Managed challenge replay can return the challenge page (udemy, 2026-11-04); the new script stage2 has not converged; an egress IP mismatch is a silent 403; availability is site-dependent.',
      },
    ],
    codeTitle: 'Three minimal calls',
    codeLede: 'curl, Go, and Python are all local calls; parameters and responses follow docs/openapi.json.',
  },
  benchmarks: {
    title: 'Benchmarks: date, conditions, source',
    lede: 'No search volume, no success-rate promises. Every number traces to a real run; limits and failure samples are listed too.',
    methodTitle: 'Method',
    method: [
      'Same-site A/B: same target, same window, comparing cfsolve / Camoufox / pydoll.',
      'Cached hot path: offline load test, Go client replays directly without Python.',
      'route=auto: real example.com, a no-shield site passes directly.',
      'All numbers come from real local runs; date and source are in the table.',
    ],
    limitationsTitle: 'What these numbers do not mean',
    limitations: [
      'They do not mean every site passes: availability is site-dependent, and managed challenge replay still has failures.',
      'They do not hold on any network: clearance is bound to the egress IP, and proxy quality changes the result.',
      'They are not permanent: Cloudflare rotates challenge scripts, and the 2026-11-03 script still has a stage2 blocker.',
    ],
    tableTitle: 'Data table',
  },
  docs: {
    title: 'Docs entry',
    lede: 'This site is an entry point only; the full API, fields, and error codes live in the repository docs/ and openapi.json.',
    quickstartTitle: 'Quickstart',
    quickstart: [
      '.\\run.ps1 start  # first start generates a token and writes ./.env',
      'curl http://127.0.0.1:8878/health  # expect state=ready',
      'python run.py e2e  # solve → replay → second call cached=true',
    ],
    endpointsTitle: 'Endpoints',
    endpoints: [
      { path: 'POST /solve', body: 'Solve; want=clearance / turnstile; route=auto detects automatically.' },
      { path: 'GET /detect', body: 'Detect the shield type only; no solve.' },
      { path: 'GET /health', body: 'State, cache, research assets; more fields with a token.' },
      { path: 'GET /status', body: 'In-flight requests and recent events; requires a token.' },
      { path: 'GET /logs', body: 'Audit JSONL tail; requires --audit-log.' },
      { path: 'GET /stats', body: 'Counters and latency.' },
      { path: 'GET /metrics', body: 'Prometheus text format.' },
      { path: 'GET /console', body: 'Read-only console; requires a token.' },
    ],
    linksTitle: 'Full docs in the repository',
    links: [
      { label: 'docs/API.md', href: 'https://github.com/lza6/CF-Bypass-Research/blob/master/docs/API.md' },
      { label: 'docs/openapi.json', href: 'https://github.com/lza6/CF-Bypass-Research/blob/master/docs/openapi.json' },
      { label: 'README.md', href: 'https://github.com/lza6/CF-Bypass-Research' },
    ],
  },
  security: {
    title: 'Security and authorized use',
    lede: 'This is a security research tool, not a service that helps you access systems without authorization. Read the boundary before use.',
    sections: [
      {
        title: 'Authorization boundary',
        body: 'Use it only on systems you own or have explicit authorization to test; follow the target site terms and local law. The project provides no target list, no batch-scan entry, and no tutorial for bypassing paywalls or access controls.',
      },
      {
        title: 'Data handling',
        body: 'The public site is static: no accounts, no cookies, no forms, no third-party analytics. The theme preference is written to localStorage only. Solver data handling is documented in docs/API.md.',
      },
      {
        title: 'Disclosure',
        body: 'Report security issues through GitHub Issues or the public repository contact. Do not attach real credentials, target data, or unredacted logs.',
      },
      {
        title: 'Non-affiliation',
        body: 'This project is not affiliated with Cloudflare, Inc. Cloudflare is a trademark of Cloudflare, Inc., used descriptively only.',
      },
      {
        title: 'Risk',
        body: 'Cloudflare rotates challenge scripts; clearance is bound to the egress IP; managed challenge replay still has failures. Any success rate must carry a date, conditions, and a source.',
      },
    ],
  },
  changelog: {
    title: 'Changelog',
    lede: 'Capability changes only, not marketing numbers. The full history is in CHANGELOG.md.',
    items: [
      { version: '6.3.0', date: '2026-11-05', body: 'Egress IP verification, readable audit logs, real traffic capture, egress in the UI.' },
      { version: '6.2.2', date: '2026-11-05', body: 'Watchdog: a wedged worker exits with code 75 and the supervisor starts a clean process.' },
      { version: '6.2.0', date: '2026-11-05', body: 'YesCaptcha compatibility for 19 task types + ecosystem error codes; 7 main-chain and 12 grafted types.' },
    ],
  },
  styleguide: {
    title: 'Internal styleguide',
    lede: 'Palette, type, buttons, focus states, dark/light themes. This page is not indexed.',
    paletteTitle: 'Palette',
    typeTitle: 'Typography',
    componentsTitle: 'Components',
  },
  footer: {
    disclaimer: 'For security research and authorized testing only; follow the target site terms and local law.',
    trademark: 'Not affiliated with Cloudflare, Inc. Cloudflare is a trademark of Cloudflare, Inc.',
    updated: 'Last updated',
    license: 'MIT License',
  },
};

export const DICT: Record<Locale, Dict> = { zh, en };

export function dict(locale: Locale): Dict {
  return DICT[locale];
}