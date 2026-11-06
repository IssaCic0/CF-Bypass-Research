"""只读状态页（`GET /`）与它的前端资产（v5.0.1 UI）。

**它是什么**：一页只读的运维面板 —— 把 `/health`、`/status`、`/logs` 三个端点摊在屏幕上，
让人**不用 ssh 上去翻 JSONL** 就能回答两个问题："它还活着吗"、"刚才那几条为什么失败"。

**它不是什么**（这些边界是有意的）：
  · 不是控制台：没有"求解"按钮、没有"重启"按钮 —— 这个服务是**组件**，不是给终端用户操作的产品；
  · 不是 SPA：单文件 HTML + 原生 JS，零依赖零构建（守 policy.md 的"零新依赖"）；
  · 不引入任何新数据面：页面只读既有端点，不新增后端接口。

**鉴权怎么处理**（这是个必须说清的设计）：
  页面本身**免鉴权**（否则浏览器打不开），但页面里的数据全部来自**需要 token** 的端点。
  token 由用户在页面里输入，存在 `sessionStorage`（关标签页即失效，不落盘、不下发）。
  页面自己**不持有**任何 token；`/logs` 的一条 `lines` 里也永远不含凭证（那是服务端的白名单保证）。

**为什么要单独一个模块**：HTML/JS 内联在 `service.py` 里会让那个文件更难读，
而且前端资产改一次就要动后端文件。这里只导出两个常量：`STATUS_PAGE_HTML` 与它的长度。
"""

from __future__ import annotations

try:  # package import (python -m solver.service)
    from .ui_tokens import TOKENS_CSS
except ImportError:  # script import (python solver/service.py)
    from ui_tokens import TOKENS_CSS

#: 页面单文件（HTML + CSS + JS）。刻意不用模板引擎、不用构建步骤。
STATUS_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<meta name="theme-color" media="(prefers-color-scheme: dark)" content="#0f1419">
<meta name="theme-color" media="(prefers-color-scheme: light)" content="#f6f8fa">
<title>CF 求解服务 · 状态</title>
<style>
__UI_TOKENS__
  header { display:flex; flex-wrap:wrap; gap:12px; align-items:center; margin-bottom:16px; }
  h1 { font-size:17px; margin:0; font-weight:600; letter-spacing:.2px; }
  .spacer { flex:1 1 auto; }
  .badge { padding:3px 9px; border-radius:999px; font-size:12px; font-weight:600;
           border:1px solid var(--line); background:var(--panel); }
  .badge.ready { color:var(--ok); border-color:var(--ok); }
  .badge.degraded { color:var(--warn); border-color:var(--warn); }
  .badge.starting, .badge.down { color:var(--bad); border-color:var(--bad); }
  /* wedged（v6.2）：worker 卡死、不会自愈 —— 用**实心反色**，比 down 更刺眼。
     这是刻意的：degraded 会自愈（黄=等等看），wedged 只能重启（红底白字=必须动手）。 */
  .badge.wedged { background:var(--bad-solid); border-color:var(--bad-solid); color:#fff; }
  input[type=password], input[type=text] {
    padding:6px 9px; border-radius:6px; border:1px solid var(--line);
    background:var(--panel); color:var(--ink); font-size:13px; min-width:220px; }
  button { padding:6px 12px; border-radius:6px; border:1px solid var(--line);
           background:var(--panel); color:var(--ink); font-size:13px; cursor:pointer; }
  button:hover { border-color:var(--accent); color:var(--accent); }
  button[aria-pressed=true] { border-color:var(--accent); color:var(--accent); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .grid { display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }
  .card h2 { font-size:13px; margin:0 0 10px; font-weight:600; color:var(--muted);
             text-transform:uppercase; letter-spacing:.06em; }
  .kv { display:grid; grid-template-columns:auto 1fr; gap:4px 12px; font-size:13px; }
  .kv dt { color:var(--muted); }
  .kv dd { margin:0; font-variant-numeric:tabular-nums; word-break:break-all; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th, td { text-align:left; padding:5px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; }
  td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  .sub { color:var(--muted); font-size:12px; font-weight:600; margin:10px 0 4px; }
  .card.wide { grid-column:1 / -1; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; }
  .chip { padding:2px 8px; border:1px solid var(--line); border-radius:999px;
          font-size:12px; color:var(--muted); }
  .chip.x { color:var(--accent); border-color:var(--accent); }
  .empty, .error { color:var(--muted); font-size:13px; padding:8px 0; }
  .error { color:var(--bad); }
  /* 连不上服务时：保留上次数据（排障有用）但**必须视觉降级**，不许继续用绿色说"一切正常" */
  .card.stale { opacity:.55; }
  .card.stale .kv dd.ok, .card.stale .kv dd.warn { color:var(--muted); }
  .card.stale .ok { color:var(--muted); }
  .stamp { display:inline-block; margin-left:8px; font-size:11px; font-weight:600;
           color:var(--bad); border:1px solid var(--bad); border-radius:999px; padding:1px 7px; }
  .foot { margin-top:16px; color:var(--muted); font-size:12px; }
  code { background:rgba(127,127,127,.15); padding:1px 5px; border-radius:4px; font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>CF 求解服务 · 状态</h1>
  <span id="state" class="badge starting">未知</span>
  <span class="spacer"></span>
  <input id="token" type="password" placeholder="Bearer token（来自 .env）" autocomplete="off">
  <button id="save" type="button">保存</button>
  <button id="refresh" type="button">刷新</button>
  <button id="auto" type="button" aria-pressed="false">自动刷新</button>
</header>

<div id="banner" class="empty" role="status" aria-live="polite" hidden></div>

<div class="grid">
  <section class="card">
    <h2>健康</h2>
    <dl class="kv" id="health"><dt>—</dt><dd></dd></dl>
  </section>
  <section class="card">
    <h2>在途请求</h2>
    <dl class="kv" id="inflight-summary"><dt>—</dt><dd></dd></dl>
    <div id="inflight-table"></div>
  </section>
  <section class="card">
    <h2>最近事件</h2>
    <div id="logs" class="empty">未加载</div>
  </section>
  <section class="card wide">
    <h2>万盾扩展类型（captchas/ 受理清单）</h2>
    <div id="captcha-types" class="chips empty">未加载</div>
  </section>
  <section class="card wide">
    <h2>出口（代理池）</h2>
    <div id="proxies" class="empty">未加载</div>
  </section>
</div>

<div class="foot">
  本页只读：数据全部来自 <code>/health</code> <code>/status</code> <code>/logs</code>。
  token 只存在浏览器 <code>sessionStorage</code>（关标签页即失效），不会写进这个页面。
</div>

<script>
(function () {
  "use strict";
  var TOKEN_KEY = "cfsolve_token";
  var stateEl = document.getElementById("state");
  var bannerEl = document.getElementById("banner");
  var healthEl = document.getElementById("health");
  var inflightSummaryEl = document.getElementById("inflight-summary");
  var inflightTableEl = document.getElementById("inflight-table");
  var logsEl = document.getElementById("logs");
  var chipsEl = document.getElementById("captcha-types");
  var proxiesEl = document.getElementById("proxies");
  var tokenInput = document.getElementById("token");
  var saveBtn = document.getElementById("save");
  var refreshBtn = document.getElementById("refresh");
  var autoBtn = document.getElementById("auto");
  var timer = null;
  var lastTotal = null;

  function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }

  var lastOkAt = null;

  function markStale(on) {
    Array.prototype.forEach.call(document.querySelectorAll("section.card"), function (card) {
      card.classList.toggle("stale", !!on);
      var old = card.querySelector(".stamp");
      if (old) { old.remove(); }
      if (on && lastOkAt) {
        var secs = Math.max(0, Math.round((Date.now() - lastOkAt) / 1000));
        var stamp = document.createElement("span");
        stamp.className = "stamp";
        stamp.textContent = "数据已过期 " + secs + " 秒";
        var h2 = card.querySelector("h2");
        if (h2) { h2.appendChild(stamp); }
      }
    });
  }

  var bannerTimer = null;
  function setBanner(msg, isError, autoHideMs) {
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    if (!msg) { bannerEl.hidden = true; return; }
    bannerEl.hidden = false;
    bannerEl.textContent = msg;
    bannerEl.className = isError ? "error" : "empty";
    if (autoHideMs) {
      bannerTimer = setTimeout(function () { bannerEl.hidden = true; }, autoHideMs);
    }
  }

  function setState(text, cls) {
    stateEl.textContent = text;
    stateEl.className = "badge " + (cls || "");
  }

  function setKV(el, pairs) {
    el.innerHTML = "";
    el.className = "kv";      // 与 console 同源纪律：setKV 必须自己保证 class 正确
    pairs.forEach(function (p) {
      var dt = document.createElement("dt"); dt.textContent = p[0];
      var dd = document.createElement("dd"); dd.textContent = p[1] == null ? "—" : String(p[1]);
      if (p[2]) dd.className = p[2];
      el.appendChild(dt); el.appendChild(dd);
    });
  }

  function getJSON(path) {
    var headers = {};
    var t = token();
    if (t) headers["Authorization"] = "Bearer " + t;
    return fetch(path, { headers: headers, cache: "no-store" }).then(function (r) {
      return r.text().then(function (text) {
        var body;
        try { body = text ? JSON.parse(text) : {}; } catch (e) { body = { raw: text }; }
        return { status: r.status, body: body };
      });
    });
  }

  function renderHealth(r) {
    var b = r.body || {};
    var state = b.state || "unknown";
    setState(state, state);
    var rows = [
      ["state", state, state === "ready" ? "ok" : (state === "degraded" ? "warn" : "bad")],
      ["ok", b.ok ? "true" : "false", b.ok ? "ok" : "bad"],
      ["worker", b.worker_alive ? "alive" : "dead", b.worker_alive ? "ok" : "bad"],
      ["browser", b.browser_alive ? "alive" : "dead", b.browser_alive ? "ok" : "bad"]
    ];
    // wedged 是**唯一需要人工/自动重启**的状态：必须把原因和"该做什么"直接写在面板里，
    // 否则运维看到的是一个陌生的英文词，唯一能做的只有猜。
    if (state === "wedged") {
      rows.push(["卡死原因", b.wedged_reason || "worker 未返回（原因未记录）", "bad"]);
      rows.push(["处置",
        b.wedge_restart
          ? "进程会以退出码 75 自行退出，由 supervisor（docker restart / run-pool）拉起"
          : "wedge_restart 未开启：请手动重启该 worker；否则服务会一直停在这里",
        b.wedge_restart ? "warn" : "bad"]);
    }
    // 未鉴权时 /health 只回最小集（服务端刻意防泄露）。这时**不要**把内部字段铺成一片"—"，
    // 那会让人以为"服务坏了/没数据"，而实际上只是没给 token。
    if (b.backends !== undefined) {
      rows.push(["后端链", (b.backends || []).join(" → ") || "—"]);
      rows.push(["缓存", b.cache_backend || "—"]);
      rows.push(["运行时长", b.uptime_sec != null ? b.uptime_sec + " s" : "—"]);
      rows.push(["求解 / 命中 / 失败",
        (b.solves || 0) + " / " + (b.cache_hits || 0) + " / " + (b.failures || 0) +
        "（只计真实发起的求解；参数错/准入拒绝不计）"]);
      rows.push(["最近错误", b.last_error || "—", b.last_error ? "bad" : ""]);
      // v6.1 嫁接层：这里只留计数，清单本身放全宽卡（挤在这张卡里会把布局撑歪）
      if (Array.isArray(b.captcha_types) && b.captcha_types.length) {
        rows.push(["扩展类型", b.captcha_types.length + " 种（清单见下方卡片）"]);
      }
    } else {
      rows.push(["内部字段", "需要 token 才能看（下面两栏同理）", "warn"]);
    }
    setKV(healthEl, rows);
  }

  function renderCaptchaTypes(b) {
    var list = Array.isArray(b.captcha_types) ? b.captcha_types : [];
    if (!list.length) { setEmptyEl(chipsEl, "服务端未上报扩展类型（需要 token 查看）"); return; }
    chipsEl.className = "chips";
    chipsEl.innerHTML = "";
    list.forEach(function (t) {
      var span = document.createElement("span");
      // 带 _x 后缀的是"避让主链路命名"的变体，用不同配色区分，避免误以为是另一套能力
      span.className = "chip" + (t.slice(-2) === "_x" ? " x" : "");
      span.textContent = t;
      chipsEl.appendChild(span);
    });
  }

  // 空的占位：class 由调用方给（扩展类型卡用 "chips empty"，出口卡用 "empty"），
  // 硬编码成 chips 会把出口卡也套上 flex 布局（那个 class 是给标签列用的）。
  function setEmptyEl(el, msg, cls) {
    el.className = (cls || "chips") + " empty";
    el.textContent = msg;
  }

  // 出口区块（v6.3）。为什么值得单独一块：出口选错 = 凭证绑在别的 IP 上 = 静默 403，
  // 而"哪个在冷却、哪个没登记 IP"此前只能靠手打 /health | jq 才看得到。
  function renderProxies(b) {
    // ⚠️ 三种"没有 proxies"必须分开说，否则就是**谎报**（看截图发现）：
    //   ① 未带 token → /health 只回最小集，我们**读不到**，不能声称"没配代理池"
    //   ② 带 token 且确实没配 → 才是"未配置"
    //   ③ 带了 token 但服务端没这个字段（老版本）→ 说清楚版本不匹配
    if (b.backends === undefined) {
      setEmptyEl(proxiesEl, "需要 token 才能看出口状态（/health 未鉴权时只回最小集）",
                 "empty");
      return;
    }
    var pool = b.proxies;
    if (!pool) {
      setEmptyEl(proxiesEl, "未配置代理池（启动时没有 --proxy）；本进程出口 = " +
                            (b.egress_id || "default"), "empty");
      return;
    }
    proxiesEl.className = "";
    proxiesEl.innerHTML = "";
    var head = document.createElement("div");
    head.className = "sub";
    head.textContent = pool.size + " 个出口，健康 " + pool.healthy +
                       "；本进程默认出口 " + (b.egress_id || "default");
    proxiesEl.appendChild(head);
    var table = document.createElement("table");
    var thead = document.createElement("thead");
    var htr = document.createElement("tr");
    ["出口", "egress_id", "评分", "连续失败", "冷却剩余", "成功/失败", "出口 IP"].forEach(function (t) {
      var th = document.createElement("th");
      th.textContent = t;
      htr.appendChild(th);
    });
    thead.appendChild(htr); table.appendChild(thead);
    var tbody = document.createElement("tbody");
    (pool.entries || []).forEach(function (e) {
      var tr = document.createElement("tr");
      addTd(tr, e.proxy, "");
      addTd(tr, e.egress_id, "");
      addTd(tr, String(e.score), "num");
      addTd(tr, String(e.consecutive_failures), e.consecutive_failures > 0 ? "bad" : "num");
      var cd = e.cooldown_left_sec || 0;
      addTd(tr, cd > 0 ? cd + " s" : "—", cd > 0 ? "bad" : "");
      addTd(tr, (e.success || 0) + " / " + (e.failure || 0), "num");
      // 未登记 IP 是个**警告**：不算错，但同一 IP 的不同入口不能共享缓存
      addTd(tr, e.egress_ip_known ? "已登记" : "未登记", e.egress_ip_known ? "ok" : "warn");
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    proxiesEl.appendChild(table);
  }

  function addTd(tr, text, cls) {
    var td = document.createElement("td");
    td.textContent = text == null ? "—" : String(text);
    if (cls) td.className = cls;
    tr.appendChild(td);
  }

  function renderStatus(r) {
    if (r.status !== 200) {
      setKV(inflightSummaryEl, [["提示", r.status === 401 ? "填入 token 后自动显示" : "暂时读不到", "warn"]]);
      inflightTableEl.innerHTML = "";
      return;
    }
    var b = r.body || {};
    setKV(inflightSummaryEl, [
      ["在途", b.inflight_count == null ? "—" : b.inflight_count,
        b.inflight_count > 0 ? "warn" : "ok"],
      ["队列深度", b.queue_depth == null ? "—" : b.queue_depth],
      ["事件总数", b.events_total == null ? "—" : b.events_total],
      ["pid", b.pid == null ? "—" : b.pid]
    ]);
    var rows = b.inflight || [];
    var mc = b.multicaptcha_inflight || [];
    // 嫁接层的在途任务与主链路**分开列**：两者的超时/产物语义不同，混在一张表里
    // 排障时看不出是谁卡住了（服务端也是分开给的）。
    if (mc.length) {
      var mcHtml = "<div class='sub'>万盾嫁接层在途（captchas/）</div>" +
        "<table><thead><tr><th>类型</th><th>目标</th><th class='num'>已耗时</th></tr></thead><tbody>";
      mc.forEach(function (row) {
        var age = row.started ? Math.max(0, Math.round(Date.now() / 1000 - row.started)) : null;
        mcHtml += "<tr><td>" + esc(row.type) + "</td><td>" + esc(row.url || "—") +
                  "</td><td class='num'>" + (age == null ? "—" : age + " s") + "</td></tr>";
      });
      inflightTableEl.innerHTML = mcHtml + "</tbody></table>";
      if (!rows.length) { return; }
      // 主链路表格追加在后面
    } else {
      inflightTableEl.innerHTML = "";      // 必须先清空，否则上一轮的表格会残留在页面上
      if (!rows.length) {
        inflightTableEl.innerHTML = '<div class="empty">当前没有在途请求</div>';
        return;
      }
    }
    var html = (mc.length ? "<div class='sub'>主链路在途</div>" : "") +
      "<table><thead><tr><th>方法</th><th>路径</th><th class='num'>已耗时</th></tr></thead><tbody>";
    rows.forEach(function (row) {
      html += "<tr><td>" + esc(row.method) + "</td><td>" + esc(row.path) +
              "</td><td class='num'>" + esc(row.elapsed_sec) + " s</td></tr>";
    });
    inflightTableEl.insertAdjacentHTML("beforeend", html + "</tbody></table>");
  }

  function renderLogs(r) {
    if (r.status !== 200) {
      logsEl.className = "empty";
      logsEl.textContent = r.status === 401
        ? "填入 token 后自动显示最近事件"
        : "暂时读不到最近事件";
      return;
    }
    var b = r.body || {};
    var lines = b.lines || [];
    if (!lines.length) {
      logsEl.className = "empty";
      logsEl.textContent = "还没有事件";
      return;
    }
    logsEl.className = "";
    var html = "<table><thead><tr><th>事件</th><th>目标</th><th>结果</th><th class='num'>耗时</th></tr></thead><tbody>";
    lines.forEach(function (ln) {
      var result = ln.ok === false
        ? "<span class='bad'>" + esc(ln.error_code || "失败") + "</span>"
        : "<span class='ok'>ok</span>";
      var took = ln.ms != null ? ln.ms + " ms" : (ln.elapsed != null ? ln.elapsed + " s" : "—");
      html += "<tr><td>" + esc(ln.event) + "</td><td>" + esc(ln.host || ln.path || "—") +
              "</td><td>" + result + "</td><td class='num'>" + esc(took) + "</td></tr>";
    });
    logsEl.innerHTML = html + "</tbody></table>";
  }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function refresh() {
    refreshBtn.disabled = true;
    // 串行而不是并行：并行时 /status 的快照里会包含页面自己那条 /logs 请求，
    // 让人误以为"有请求卡在途"（观察者效应）。串行后 /status 看到的是"除本页之外"的真实在途。
    return getJSON("/health")
      .then(function (h) { return getJSON("/status").then(function (st) {
        return getJSON("/logs?lines=20").then(function (lg) { return [h, st, lg]; });
      }); })
      .then(function (rs) {
        lastOkAt = Date.now();
        markStale(false);
        renderHealth(rs[0]);
        renderCaptchaTypes(rs[0].body || {});
        renderProxies(rs[0].body || {});
        renderStatus(rs[1]);
        renderLogs(rs[2]);
        // /health 是公开的（所以"服务活着"这件事不需要 token 就能看到）；
        // 但 /status 与 /logs 需要 token —— 401 时必须**明确说清楚缺什么、去哪拿**，
        // 否则用户看到的是"健康 ready，但两块面板写着读不到"，只能自己去猜。
        var needsToken = rs[1].status === 401 || rs[2].status === 401;
        if (needsToken) {
          setBanner("服务活着（健康信息是公开的）。要看「在途请求」与「最近事件」需要 Bearer token：" +
                    "从服务端的 .env 里取 CF_SOLVE_TOKEN= 后面的值，粘到右上角后点「保存」。", false);
        } else {
          setBanner("");
        }
        var total = (rs[2].body || {}).total;
        if (total != null && !needsToken) {
          if (lastTotal != null && total !== lastTotal) {
            setBanner("事件总数 " + lastTotal + " → " + total, false, 3000);
          }
          lastTotal = total;
        }
      })
      .catch(function (e) {
        setState("连不上", "down");
        // 关键：卡片里的数字现在是**过期数据**，必须标出来并视觉降级，
        // 否则会出现"横幅说连不上、卡片用绿色说一切正常"的自相矛盾。
        markStale(true);
        setBanner("连不上服务：请求失败（服务是否已停止？端口是否对？浏览器控制台可见 " +
                  e.message + "）。下面的数据是最后一次成功获取的，仅供参考。", true);
      })
      .then(function () { refreshBtn.disabled = false; });
  }

  saveBtn.addEventListener("click", function () {
    sessionStorage.setItem(TOKEN_KEY, tokenInput.value.trim());
    refresh();
  });
  refreshBtn.addEventListener("click", refresh);
  autoBtn.addEventListener("click", function () {
    var on = autoBtn.getAttribute("aria-pressed") === "true";
    if (on) {
      clearInterval(timer); timer = null;
      autoBtn.setAttribute("aria-pressed", "false");
      autoBtn.textContent = "自动刷新";
    } else {
      timer = setInterval(refresh, 5000);
      autoBtn.setAttribute("aria-pressed", "true");
      autoBtn.textContent = "自动刷新（开）";
      refresh();
    }
  });

  tokenInput.value = token();
  refresh();
})();
</script>
</body>
</html>
"""

#: 预计算长度：`_send_html` 要写 `Content-Length`，每次请求重新 encode 是浪费。
STATUS_PAGE_HTML = STATUS_PAGE_HTML.replace("__UI_TOKENS__", TOKENS_CSS)

STATUS_PAGE_HTML_BYTES = STATUS_PAGE_HTML.encode("utf-8")
