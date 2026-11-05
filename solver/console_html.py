"""只读控制台（`GET /console`）的前端资产（T-78）。

**它是什么**：一页只读控制台 —— 四个区块（服务状态 / 缓存与命中 / 研究资产 / 最近错误），
数据全部来自 `/health`（带 token）与 `/health.research`（T-76）。

**与 `statuspage.py`（`GET /`）的分工**：
  · `/` 是免鉴权状态页（公开端点），页面本身能打开；
  · `/console` 是**带 token 的端点**（与其它数据端点同一套 Bearer 门），页面拿到手
    就已经过鉴权 —— 页面里再保存 token 只是为了前端 fetch 数据端点时能带上。

**五条硬约束（test_console.py 逐条锁死）**：
  1. 零写入：无 form、无 POST、无任何改状态入口 —— 叫"只读控制台"不叫"仪表盘"；
  2. 零新依赖：HTML/CSS/JS 全部内联在这**一个字符串常量**里，无 npm/webpack/CDN；
  3. token 保护：页面不含 token；前端 `prompt()` 现问，只存 `sessionStorage`
     （不写 localStorage —— 多标签页共享会留痕）；DOM 里绝不出现 token 值；
  4. CSP：响应头 `default-src 'none'`（在 service.py 的 `_send_console_page` 里下发）；
     DOM 只用 `textContent`/`createElement`，**禁止 `innerHTML`**；
     不用内联事件属性，统一 `addEventListener`；
  5. 数据脱敏：只渲染**白名单字段**（state/solves/cache_hits/failures/...），
     服务端健康视图里的错误原文等敏感字段**前端根本不读**
     （服务端白名单是第一道，前端不读是第二道）。

**安全说明（须随契约文档公开）**：单文件零构建 ⇒ 内联脚本必须 `script-src 'unsafe-inline'`，
这是已知代价；若要更严，下一版迁静态文件 + `script-src 'self'`。
"""

from __future__ import annotations

#: 页面单文件（HTML + CSS + JS）。刻意不用模板引擎、不用构建步骤。
CONSOLE_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CF 求解服务 · 只读控制台</title>
<style>
  :root {
    --bg: #0f1419; --panel: #171d24; --line: #2a333d; --ink: #e6edf3;
    --muted: #8b98a5; --ok: #2ea043; --warn: #d29922; --bad: #f85149; --accent: #58a6ff;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f6f8fa; --panel:#fff; --line:#d0d7de; --ink:#1f2328; --muted:#656d76;
            --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; --accent:#0969da; }
  }
  * { box-sizing: border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--ink);
         font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }
  header { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:16px; }
  h1 { font-size:17px; margin:0; font-weight:600; letter-spacing:.2px; }
  .spacer { flex:1 1 auto; }
  .badge { padding:3px 9px; border-radius:999px; font-size:12px; font-weight:600;
           border:1px solid var(--line); background:var(--panel); }
  .badge.ready { color:var(--ok); border-color:var(--ok); }
  .badge.degraded { color:var(--warn); border-color:var(--warn); }
  .badge.starting, .badge.down, .badge.unauthorized { color:var(--bad); border-color:var(--bad); }
  /* wedged（v6.2）：worker 卡死、不会自愈 —— 实心反色，必须比 down 更刺眼 */
  .badge.wedged { background:var(--bad); border-color:var(--bad); color:#fff; }
  button { padding:6px 12px; border-radius:6px; border:1px solid var(--line);
           background:var(--panel); color:var(--ink); font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .card h2 { font-size:13px; margin:0 0 10px; font-weight:600; color:var(--muted);
             text-transform:uppercase; letter-spacing:.06em; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:13px; }
  .kv dt { color:var(--muted); }
  .kv dd { margin:0; font-variant-numeric:tabular-nums; word-break:break-all; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  .empty, .error { color:var(--muted); font-size:13px; padding:8px 0; }
  .error { color:var(--bad); }
  .foot { margin-top:16px; color:var(--muted); font-size:12px; }
  code { background:rgba(127,127,127,.15); padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>CF 求解服务 · 只读控制台</h1>
  <span id="state" class="badge starting">未知</span>
  <span class="spacer"></span>
  <button id="refresh" type="button">刷新</button>
  <button id="forget" type="button">清除 token</button>
</header>

<div id="banner" class="empty" role="status" aria-live="polite" hidden></div>

<div class="grid">
  <section class="card">
    <h2>服务状态</h2>
    <dl class="kv" id="svc"><dt>—</dt><dd></dd></dl>
  </section>
  <section class="card">
    <h2>缓存与命中</h2>
    <dl class="kv" id="cache"><dt>—</dt><dd></dd></dl>
  </section>
  <section class="card">
    <h2>研究资产</h2>
    <dl class="kv" id="research"><dt>—</dt><dd></dd></dl>
  </section>
  <section class="card">
    <h2>最近错误</h2>
    <div id="err" class="empty">未加载</div>
  </section>
</div>

<div class="foot">
  本页只读（零写入）：数据只来自 <code>/health</code>（含 <code>research</code> 段），
  没有任何表单或提交入口。token 用 <code>prompt()</code> 现问、只存
  <code>sessionStorage</code>（关标签页即失效），不写进这个页面、不落 localStorage。
</div>

<script>
(function () {
  "use strict";
  var TOKEN_KEY = "cfsolve_token";
  var stateEl = document.getElementById("state");
  var bannerEl = document.getElementById("banner");
  var svcEl = document.getElementById("svc");
  var cacheEl = document.getElementById("cache");
  var researchEl = document.getElementById("research");
  var errEl = document.getElementById("err");
  var refreshBtn = document.getElementById("refresh");
  var forgetBtn = document.getElementById("forget");

  function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }

  function setBanner(msg, isError) {
    if (!msg) { bannerEl.hidden = true; return; }
    bannerEl.hidden = false;
    bannerEl.textContent = msg;
    bannerEl.className = isError ? "error" : "empty";
  }

  function setState(text, cls) {
    stateEl.textContent = text;
    stateEl.className = "badge " + (cls || "");
  }

  // 只用 textContent / createElement —— DOM 写入面收敛在这两个 API 上（约束 4）。
  function setKV(el, pairs) {
    while (el.firstChild) { el.removeChild(el.firstChild); }
    // 必须把 class 还原成 kv：setEmpty() 会把它改成 "empty"，不还原就丢掉
    // `display:grid` → 键值从两列退化成上下堆叠（真 Chrome 实测过）。
    el.className = "kv";
    pairs.forEach(function (p) {
      var dt = document.createElement("dt"); dt.textContent = p[0];
      var dd = document.createElement("dd"); dd.textContent = p[1] == null ? "—" : String(p[1]);
      if (p[2]) dd.className = p[2];
      el.appendChild(dt); el.appendChild(dd);
    });
  }

  // 时间格式化：服务端可能给 ISO 字符串（assertions 矩阵）或 epoch 秒（其它路径）。
  // 统一在这里收口 —— 原来只按 *1000 处理，ISO 字符串会渲染成 "Invalid Date"。
  function _fmtTime(v) {
    if (v == null || v === "") { return "从未"; }
    var d = (typeof v === "number") ? new Date(v * 1000) : new Date(String(v));
    return isNaN(d.getTime()) ? String(v) : d.toLocaleString();
  }

  function setEmpty(el, msg) {
    while (el.firstChild) { el.removeChild(el.firstChild); }
    el.className = "empty";
    el.textContent = msg;
  }

  function getHealth() {
    var headers = {};
    var t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    return fetch("/health", { headers: headers, cache: "no-store" }).then(function (r) {
      return r.json().then(function (body) { return { status: r.status, body: body }; });
    });
  }

  function renderEmptyStates() {
    setEmpty(svcEl, "未收到 /health 响应：检查服务是否启动");
    setEmpty(cacheEl, "缓存为空：尚未解过任何域");
    setEmpty(researchEl, "corpus 为空：需一次性抓取授权（见 plans/下一步改进指南.md §6）");
    setEmpty(errEl, "无错误");
  }

  function render(b) {
    // 数据脱敏（约束 5）：只读白名单字段。服务端的错误原文不进前端 ——
    // 「最近错误」区块只显示"有没有错 / 何时"，不显示错误内容本身。
    var st = b.state || "unknown";
    setState(st, st);
    var svcRows = [
      ["state", st, st === "ready" ? "ok" : (st === "degraded" ? "warn" : "bad")],
      ["浏览器", b.browser_alive ? "alive" : "dead", b.browser_alive ? "ok" : "bad"],
      ["worker", b.worker_alive ? "alive" : "dead", b.worker_alive ? "ok" : "bad"],
      ["队列深度", b.queue_depth == null ? "—" : b.queue_depth,
        (b.queue_depth || 0) > 0 ? "warn" : ""],
      ["运行时长", b.uptime_sec == null ? "—" : b.uptime_sec + " s"]
    ];
    // wedged 必须把原因与处置写在面板里：这是唯一"等不会好"的状态。
    if (st === "wedged") {
      svcRows.push(["卡死原因", b.wedged_reason || "worker 未返回（原因未记录）", "bad"]);
      svcRows.push(["处置", b.wedge_restart ? "进程会以 75 退出，由 supervisor 拉起"
                                            : "请手动重启该 worker", b.wedge_restart ? "warn" : "bad"]);
    }
    if (Array.isArray(b.captcha_types) && b.captcha_types.length) {
      // 只给计数：11 项全铺在这张窄卡里会换 4 行，把四列布局撑歪（看截图才发现）。
      // 完整清单在状态页 `/` 的「万盾扩展类型」全宽卡上。
      svcRows.push(["扩展类型", b.captcha_types.length + " 种（清单见状态页）"]);
    }
    setKV(svcEl, svcRows);
    var solves = b.solves || 0;
    setKV(cacheEl, [
      ["求解", solves],
      ["命中", b.cache_hits || 0],
      ["失败", b.failures || 0, (b.failures || 0) > 0 ? "bad" : ""],
      ["缓存域数", b.cached_domains || 0]
    ]);
    var r = b.research;
    if (!r) {
      setEmpty(researchEl, "corpus 为空：需一次性抓取授权（见 plans/下一步改进指南.md §6）");
    } else {
      var skipped = r.assertions_skipped || 0;
      var total = r.assertions_total || 0;
      setKV(researchEl, [
        ["语料变体", r.corpus_variants || 0, (r.corpus_variants || 0) === 0 ? "warn" : "ok"],
        ["断言总数", total],
        ["断言 skip", skipped, skipped > 0 ? "warn" : "ok"],
        ["上次快照", _fmtTime(r.last_snapshot_at)],
        ["note", r.note || "—"]
      ]);
    }
    if (b.has_last_error) {
      // 刻意只给"有过错 + 时间"，不给错误原文（原文可能带主机名/代理地址）
      errEl.className = "error";
      var when = b.last_error_at ? _fmtTime(b.last_error_at) : "未知时间";
      errEl.textContent = "最近一次错误发生于 " + when +
        "（详情见服务端日志，本页刻意不显示原文）";
    } else {
      setEmpty(errEl, "无错误");
    }
  }

  function refresh() {
    refreshBtn.disabled = true;
    renderEmptyStates();
    if (!token()) {
      var t = window.prompt("输入 Bearer token（.env 的 CF_SOLVE_TOKEN= 后面的值）", "");
      if (t == null) {
        setState("未鉴权", "unauthorized");
        setBanner("未提供 token：所有区块都没有数据。点「刷新」可重新输入。", false);
        refreshBtn.disabled = false;
        return;
      }
      sessionStorage.setItem(TOKEN_KEY, t.trim());
    }
    getHealth().then(function (r) {
      if (r.status === 401) {
        sessionStorage.removeItem(TOKEN_KEY);
        setState("token 无效", "unauthorized");
        setBanner("token 被服务端拒绝（401）：点「刷新」重新输入。", true);
        renderEmptyStates();
        return;
      }
      // 503 既可能是 starting/degraded（会自愈），也可能是 wedged（不会自愈）。
      // 只要响应体里带了 state 就必须按 state 渲染 —— 否则运维看到的是"异常"，
      // 而真正该采取的动作（重启）完全看不出来。
      var st = (r.body && r.body.state) || "";
      if (r.status !== 200 && !st) {
        setState("异常", "down");
        setBanner("/health 返回异常状态：" + r.status, true);
        renderEmptyStates();
        return;
      }
      if (!r.body || !st) {
        setState("异常", "down");
        setBanner("/health 返回异常状态：" + r.status, true);
        renderEmptyStates();
        return;
      }
      // 横幅只能设一次：先按状态决定，**最后不要无条件清空**（否则刚设的 wedged 提示被抹掉）
      if (st === "wedged") {
        setBanner("worker 卡死（wedged）：服务不会自愈。" + (r.body.wedged_reason || "") +
                  (r.body.wedge_restart ? "；进程将以退出码 75 退出并由 supervisor 拉起。"
                                        : "；wedge_restart 未开启，需手动重启。"), true);
      } else if (r.status !== 200) {
        setBanner("/health 返回 " + r.status + "（state=" + st + "），服务正在自愈或启动中。", false);
      } else {
        setBanner("");
      }
      render(r.body);
    }).catch(function (e) {
      setState("连不上", "down");
      setBanner("连不上服务：" + (e && e.message ? e.message : "请求失败") +
                "（服务是否已启动？）", true);
    }).then(function () { refreshBtn.disabled = false; });
  }

  // 不用内联事件属性，全部 addEventListener（约束 4）
  refreshBtn.addEventListener("click", refresh);
  forgetBtn.addEventListener("click", function () {
    sessionStorage.removeItem(TOKEN_KEY);
    setState("未鉴权", "unauthorized");
    setBanner("已清除 sessionStorage 中的 token。", false);
    renderEmptyStates();
  });

  renderEmptyStates();
  refresh();
})();
</script>
</body>
</html>
"""

#: 预计算长度：响应要写 `Content-Length`，每次请求重新 encode 是浪费。
CONSOLE_PAGE_HTML_BYTES = CONSOLE_PAGE_HTML.encode("utf-8")
