"""`/status` 与 `/logs` 的契约测试 + `solver/eventlog.py` 的安全边界单测（v5.0.1 P2-1）。

两件事必须同时成立，缺一不可：
  1. **问题答得上来**：`/status` 能说清"现在有几个在途、最久那个多久"，
     `/logs` 能说清"刚才那几条为什么失败"；
  2. **不该漏的不漏**：`/logs` 是对外可读端点，凭证类字段必须由**白名单**挡住，
     而不是指望每个调用点都记得别传。

第 2 条是这组用例的重点 —— 一个"贴心的日志端点"如果会回显 token，
那它是漏洞而不是功能。
"""

from __future__ import annotations

import json
import time

import pytest

from solver.eventlog import ALLOWED_FIELDS, MAX_VALUE_LEN, EventLog
from solver.service import AUDIT_FIELDS, MAX_LOG_LINES
from test_service import _get, _post, live_server  # noqa: F401 —— fixture 需被收集


# ─────────────────────────────────────────── EventLog 单元测试


def test_whitelist_covers_audit_fields():
    """审计白名单是本模块白名单的子集。

    两份白名单故意分开（审计落盘口径该更严），但**不允许漂移**：
    有人往审计里加字段却忘了让 `/logs` 认得它，这条会红。
    """
    missing = set(AUDIT_FIELDS) - ALLOWED_FIELDS
    assert not missing, f"这些审计字段没有进 /logs 白名单：{sorted(missing)}"


@pytest.mark.parametrize(
    "field",
    ["token", "cookie", "authorization", "cf_clearance", "password", "secret", "user_agent_full"],
)
def test_credential_like_fields_are_dropped(field):
    """白名单外的一切字段必须被丢弃 —— 这是结构性防线，不依赖调用点自觉。"""
    log = EventLog(maxlen=10)
    log.record(event="solve", **{field: "SENSITIVE-VALUE"})
    snap = log.snapshot(10)
    assert snap["lines"], "事件本身应当被记录"
    assert field not in snap["lines"][0], f"{field} 竟然进了可对外读的缓冲"
    assert "SENSITIVE-VALUE" not in json.dumps(snap["lines"], ensure_ascii=False)
    assert snap["dropped_fields"] == 1, "丢弃必须可观测，不能静默"


def test_allowed_fields_survive():
    log = EventLog(maxlen=10)
    log.record(event="solve", host="example.com", ok=False, error_code="timeout", stage="solve")
    line = log.snapshot(10)["lines"][0]
    assert line["event"] == "solve"
    assert line["host"] == "example.com"
    assert line["ok"] is False
    assert line["error_code"] == "timeout"
    assert "ts" in line


def test_long_values_are_truncated():
    """域名/错误串是调用方可控输入：不截断就是内存放大器。"""
    log = EventLog(maxlen=10)
    log.record(event="solve", host="h" * (MAX_VALUE_LEN * 3))
    assert len(log.snapshot(10)["lines"][0]["host"]) == MAX_VALUE_LEN


def test_buffer_is_bounded_but_total_keeps_counting():
    log = EventLog(maxlen=5)
    for i in range(20):
        log.record(event="solve", req_id=f"r{i}")
    snap = log.snapshot(100)
    assert snap["kept"] == 5, "缓冲必须定长"
    assert snap["total"] == 20, "total 是累计条数，不是当前长度"
    # 新的在前
    assert snap["lines"][0]["req_id"] == "r19"
    assert snap["lines"][-1]["req_id"] == "r15"


def test_snapshot_lines_zero_returns_no_lines_but_counts():
    log = EventLog(maxlen=10)
    log.record(event="solve")
    snap = log.snapshot(0)
    assert snap["lines"] == []
    assert snap["total"] == 1


def test_concurrent_writes_do_not_lose_count():
    """多线程并发写不丢计数、不抛异常（HTTP handler 是多线程的）。"""
    import threading

    log = EventLog(maxlen=50)
    threads = [
        threading.Thread(target=lambda: [log.record(event="solve", req_id="x") for _ in range(200)])
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log.snapshot(0)["total"] == 1600


# ─────────────────────────────────────────── HTTP 契约测试


def test_status_requires_token(live_server):
    status, body = _get(live_server, "/status")
    assert status == 401
    assert json.loads(body)["error_code"] == "unauthorized"


def test_logs_requires_token(live_server):
    status, body = _get(live_server, "/logs")
    assert status == 401
    assert json.loads(body)["error_code"] == "unauthorized"


def test_status_answers_what_is_running(live_server):
    status, body = _get(live_server, "/status", token="test-token")
    assert status == 200
    doc = json.loads(body)
    assert doc["ok"] is True
    assert doc["state"] == "ready"
    assert doc["inflight_count"] == 0
    assert doc["inflight"] == []
    assert isinstance(doc["pid"], int)
    assert doc["backends"] == ["camoufox"]
    # 刻意不带 /health 完整体里的高敏字段
    assert "last_error" not in doc
    assert "proxies" not in doc
    assert "ua_fingerprint" not in doc


def test_logs_surface_failures_and_never_leak_token(live_server):
    """一次真实的 400 会进缓冲，且响应里绝不出现 token。"""
    code, payload = _post(live_server, "/solve", {"nope": 1}, token="test-token")
    assert code == 400
    assert payload["error_code"] == "invalid-request"

    status, body = _get(live_server, "/logs?lines=10", token="test-token")
    assert status == 200
    assert "test-token" not in body, "/logs 泄露了 token"

    doc = json.loads(body)
    assert doc["total"] >= 1
    solve_events = [ln for ln in doc["lines"] if ln.get("event") == "solve"]
    assert solve_events, f"400 之后应当留下一条 solve 事件，实际：{doc['lines']}"
    latest = solve_events[0]
    assert latest["ok"] is False
    assert latest["error_code"] == "invalid-request"
    assert latest["req_id"], "没有 req_id 就无法和 stdout 的 JSON 行对上"


def test_logs_lines_param_is_clamped_and_forgiving(live_server):
    """`lines` 是调用方可控输入：非法值回默认、超大值夹取、负数夹到 0，都不报错。"""
    for line in [ln for ln in ("?lines=1", "?lines=abc", f"?lines={MAX_LOG_LINES * 10}", "?lines=-5")]:
        status, body = _get(live_server, f"/logs{line}", token="test-token")
        assert status == 200, f"{line} 不该报错"
        doc = json.loads(body)
        assert doc["ok"] is True
        assert len(doc["lines"]) <= MAX_LOG_LINES


def test_logs_default_lines_when_no_param(live_server):
    status, body = _get(live_server, "/logs", token="test-token")
    assert status == 200
    assert json.loads(body)["ok"] is True


def test_status_counts_inflight_request(live_server):
    """在途明细必须反映真实并发：慢请求期间 `/status` 应能看见它。"""
    import threading

    # 注意：这里必须用**脚本模式**的模块对象。
    # `tests/test_service.py` 把 `solver/` 插进 sys.path 后 `import service`，而本文件顶部
    # 用的是包模式 `solver.service` —— 两者是**两个不同的模块对象**（两个 Handler 类）。
    # `live_server` fixture 把 solver 挂在脚本模式那个 Handler 上，读另一个对象永远是空的。
    import service as svc

    solver = svc.Handler.solver
    started = threading.Event()
    release = threading.Event()

    def slow_solve():
        # 直接打 solver 层：HTTP 层已有 _track_inflight 覆盖，这里验证
        # "begin/end_request 的配对"与 status 的读取口径一致。
        rid = solver.begin_request("POST", "/solve")
        started.set()
        release.wait(5.0)
        solver.end_request(rid)

    t = threading.Thread(target=slow_solve, daemon=True)
    t.start()
    assert started.wait(5.0)
    try:
        status, body = _get(live_server, "/status", token="test-token")
        assert status == 200
        doc = json.loads(body)
        assert doc["inflight_count"] == 1
        assert doc["inflight"][0]["path"] == "/solve"
        assert doc["inflight"][0]["method"] == "POST"
        assert doc["inflight"][0]["elapsed_sec"] >= 0.0
    finally:
        release.set()
        t.join(5.0)

    # 收尾后必须归零，否则就是"注册表泄漏"
    status, body = _get(live_server, "/status", token="test-token")
    assert json.loads(body)["inflight_count"] == 0


def test_status_never_echoes_query_string(live_server):
    """`/status` 是任何 token 持有者都能读的端点，绝不能把调用方的 query 回显出去。

    具体场景：`POST /detect?url=https://internal-host/...` 里的内部主机名属于调用方的
    敏感信息；若原样登记进在途注册表，`/status` 就会把它播给所有 token 持有者。
    剥离做在 `Solver.begin_request` 里（纵深防御），所以这里直接测那一层。
    """
    from service import Handler  # 脚本模式模块：fixture 把 solver 挂在它上面

    solver = Handler.solver
    rid = solver.begin_request("GET", "/detect?url=https://internal-host.internal/secret&fresh=1")
    try:
        snap = solver.inflight_snapshot()
        assert [r["path"] for r in snap] == ["/detect"], snap
        assert "internal-host" not in json.dumps(snap, ensure_ascii=False)
    finally:
        solver.end_request(rid)


# ─────────────────────────────────────────── 只读状态页（UI）

def test_status_page_is_public_html(live_server):
    """页面必须**免鉴权**且是 HTML —— 否则浏览器连页面都打不开。"""
    status, body = _get(live_server, "/")
    assert status == 200
    assert "<!doctype html>" in body.lower()
    assert "CF 求解服务" in body


def test_status_page_contains_no_service_data(live_server):
    """页面**不含任何服务端数据**：token 由用户在页面上填，数据靠浏览器的后续请求去拿。"""
    _, body = _get(live_server, "/")
    assert "test-token" not in body          # 服务器 token 绝不能出现在页面里
    assert "cf_clearance" not in body.lower()


def test_status_page_points_at_the_real_endpoints(live_server):
    """页面上写的数据源必须与真实路由一致（改名/删除路由会在这里红）。"""
    _, body = _get(live_server, "/")
    for path in ("/health", "/status", "/logs"):
        assert path in body, f"页面没有引用 {path}"
    assert "sessionStorage" in body, "token 应当只存在 sessionStorage"


def test_status_page_has_no_external_resources(live_server):
    """零依赖：不引 CDN、不引字体、不引图标 —— 离线环境也必须能打开。"""
    _, body = _get(live_server, "/")
    lowered = body.lower()
    for bad in ("http://", "https://", "<link ", "src=\"//"):
        assert bad not in lowered, f"页面引用了外部资源：{bad}"


def test_status_page_buttons_are_real_and_wired(live_server):
    """按钮必须是真按钮且真的绑了行为（不是画上去的装饰）。"""
    _, body = _get(live_server, "/")
    for btn_id, handler in (
        ("save", "saveBtn.addEventListener"),
        ("refresh", "refreshBtn.addEventListener"),
        ("auto", "autoBtn.addEventListener"),
    ):
        assert f'id="{btn_id}"' in body, f"缺少按钮 {btn_id}"
        assert handler in body, f"按钮 {btn_id} 没有绑定事件"


def test_status_page_escapes_untrusted_values(live_server):
    """`esc()` 必须存在且覆盖 & < > \" ' —— 事件里的 host 是调用方可控输入。"""
    _, body = _get(live_server, "/")
    assert "function esc(" in body
    assert "&amp;" in body and "&lt;" in body and "&quot;" in body


def test_status_page_marks_previous_data_stale_when_unreachable(live_server):
    """服务中途死掉时，页面**不许**继续用绿色显示上一次的数据。

    这是截图验收时抓到的真缺陷：横幅说"连不上"，健康卡却还是绿色的 `ready/alive`，
    在途卡还显示旧 pid —— 页面在用过期数据自相矛盾。
    现在必须有 `markStale()` + "数据已过期 N 秒" 标记，并把卡片视觉降级。
    """
    _, body = _get(live_server, "/")
    assert "markStale" in body
    assert "数据已过期" in body
    assert "classList.toggle(\"stale\"" in body
    assert ".card.stale" in body


def test_status_page_sends_hardening_headers(live_server):
    """新增的公网可读端点必须自带防线（CSP / nosniff / 不许被 iframe 套壳 / 不发 Referer）。

    为什么单列：它是对外**免鉴权**的页面，一旦将来有人往页面里塞了可变内容，
    没有 CSP 就等于把 XSS 的后果放大到"能把 sessionStorage 里的 token 外传"。
    """
    import urllib.request

    with urllib.request.urlopen(live_server + "/", timeout=10) as r:
        h = {k.lower(): v for k, v in r.headers.items()}
    assert "content-security-policy" in h, "缺少 CSP"
    csp = h["content-security-policy"]
    for directive in ("default-src 'none'", "connect-src 'self'", "frame-ancestors 'none'"):
        assert directive in csp, f"CSP 缺少 {directive}"
    assert h.get("x-content-type-options") == "nosniff"
    assert h.get("referrer-policy") == "no-referrer"


def test_allowed_fields_has_no_credential_like_names():
    """反向断言：白名单里不能出现任何"看起来就是凭证"的字段名。

    为什么需要它：`AUDIT_FIELDS ⊆ ALLOWED_FIELDS` 只能防"审计字段没进白名单"，
    防不了"有人把 token 同时加进两个白名单"——那时子集关系依然成立、断言照样通过，
    而凭证会进 `/logs`。这里用**名字黑名单**兜住这个方向。
    """
    # `clearance_len` 是**长度**（审计与 /logs 都只记长度，不记本体），显式豁免
    SAFE = {"clearance_len"}
    banned = ("token", "cookie", "clearance", "authorization", "secret", "password", "credential", "proxy")
    offenders = sorted(
        f for f in ALLOWED_FIELDS
        if f not in SAFE and any(b in f.lower() for b in banned)
    )
    assert not offenders, f"这些字段名像凭证，不该进对外可读的 /logs：{offenders}"


# ── v6.2：进程自检指标（RSS / 峰值 / 线程数）──────────────


def _scalar(text: str, name: str) -> int:
    """从 Prometheus 文本里取一条无标签标量的值。"""
    for line in text.splitlines():
        if line.startswith(name + " "):
            return int(float(line.split()[1]))
    raise AssertionError(f"指标 {name} 不存在")


def test_process_metrics_present_and_sane(live_server):
    """卡死/内存爆炸的第一现场信号：进程自己胖了没有。

    本项目有过 21GB/31GB 两次真实内存爆炸，而此前 /metrics 只有业务计数。
    """
    base = live_server
    status, body = _get(base, "/metrics", "test-token")
    assert status == 200
    assert "cfsolve_process_rss_bytes" in body
    assert "cfsolve_process_peak_rss_bytes" in body
    assert "cfsolve_process_threads" in body
    rss = _scalar(body, "cfsolve_process_rss_bytes")
    assert 10 * 1024 * 1024 < rss < 200 * 1024 * 1024, f"RSS 数值不合理：{rss}"
    peak = _scalar(body, "cfsolve_process_peak_rss_bytes")
    # ⚠️ **不能断言 `peak >= rss`**（CI 抓到：84922368 vs 84926464，差 4KB）。
    # 内核的 `ru_maxrss` 按页记账惰性更新，会短暂滞后于 `/proc/self/statm` 的当前值，
    # 所以"峰值略小于当前"是**操作系统的正常行为**，不是我们的 bug。
    # 有意义的断言是"两者在同一量级"。
    assert peak >= rss - 1024 * 1024, f"峰值与当前差了一个数量级：peak={peak} rss={rss}"


def test_metrics_still_ok_when_process_probe_fails(monkeypatch):
    """/metrics 不能因为"采集进程信息失败"就 500 —— 少几条序列好过整页挂掉。"""
    import metrics as m
    monkeypatch.setattr(m, "process_metrics", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    text = m.render_metrics_text(
        solves=0, hits=0, failures=0, timeouts=0, rejected_busy=0, restarts=0,
        browser_alive=False, queue_depth=0, elapsed_sum=0.0, hist=[0],
        backend_ok={}, backend_err={},
        process_fn=m.process_metrics,
    )
    assert "cfsolve_solves_total" in text
    assert "cfsolve_process_rss_bytes" not in text


def test_process_metrics_posix_branch(monkeypatch):
    """POSIX 分支：用假 `resource` 模块跑到（两个平台都要能跑这条）。

    ⚠️ **不许 monkeypatch 全局 `os.name`**（CI 抓到的真缺陷）：`pathlib` 实例化时读
    `os.name`，在 Linux 上把它改成 "nt" 会让 `Path()` 变成 `WindowsPath` →
    `NotImplementedError` 把整个 pytest 打崩。改用 `platform_name=` 注入。
    """
    import metrics as m
    import types

    class _Usage:
        ru_maxrss = 2048          # KB
    fake_resource = types.SimpleNamespace(
        RUSAGE_SELF=0, getrusage=lambda _who: _Usage())
    monkeypatch.setitem(__import__("sys").modules, "resource", fake_resource)
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError("no proc")))
    out = m.process_metrics(platform_name="posix")
    assert out["peak_rss_bytes"] == 2048 * 1024
    assert "threads" in out


def test_process_metrics_never_raises(monkeypatch):
    """采集本身炸了也必须返回 dict（可能只有 threads）—— /metrics 不能因自检挂掉。

    注入 Windows 分支但让 `ctypes.windll` 不存在：实现里整段包在 try/except 里，
    所以只会少给字段，不会抛。
    """
    import metrics as m

    out = m.process_metrics(platform_name="nt")
    assert isinstance(out, dict)
    assert "threads" in out


def test_age_seconds_handles_overflow():
    """极端时间戳不能让 /metrics 打挂（平台差异：Windows 的 `.timestamp()` 抛 OSError，
    Linux 能算出巨大值）。断言只要求"不抛"，两种结果都接受 —— 之前写成
    `is None` 在 Linux 上是假红（CI 抓到）。"""
    import metrics as m
    from datetime import datetime
    value = m._age_seconds(datetime.max.isoformat(), m.time.time())
    assert value is None or isinstance(value, float)


def test_process_metrics_threads_probe_failure(monkeypatch):
    """线程数采集失败 → 只少这一条，不抛。

    ⚠️ **不许把 `sys.modules["threading"]` 换成假对象**（CI 抓到）：
    pytest 自己的内部实现也 import threading，换掉会把整个会话搞坏。
    改成 patch 真正的 `threading.active_count`。
    """
    import metrics as m
    import threading
    monkeypatch.setattr(threading, "active_count",
                        lambda: (_ for _ in ()).throw(RuntimeError("no")))
    out = m.process_metrics(platform_name="posix")
    assert "threads" not in out            # 少一条，但不抛


def test_process_metrics_linux_statm(monkeypatch):
    """Linux 上有 /proc/self/statm 时 rss_bytes 走 pages × page_size。"""
    import metrics as m
    import io
    import types

    class _Usage:
        ru_maxrss = 1024
    fake_resource = types.SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _w: _Usage())
    monkeypatch.setitem(__import__("sys").modules, "resource", fake_resource)
    monkeypatch.setattr(m.sys, "platform", "linux")
    monkeypatch.setattr(m.os, "sysconf", lambda name: 4096, raising=False)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("100 50 0 0"))
    out = m.process_metrics(platform_name="posix")
    assert out["rss_bytes"] == 50 * 4096


def test_process_metrics_outer_failure(monkeypatch):
    """整段采集抛异常 → 只保留已采集到的字段，绝不向上冒泡。"""
    import metrics as m
    import types
    monkeypatch.setitem(__import__("sys").modules, "resource",
                        types.SimpleNamespace(RUSAGE_SELF=0,
                                              getrusage=lambda _w: (_ for _ in ()).throw(RuntimeError("boom"))))
    out = m.process_metrics(platform_name="posix")
    # 采集顺序是"先 statm（当前 RSS）→ 再 getrusage（峰值）"（v6.2.2 改的，
    # 为了让 peak<rss 的窗口最小）。所以 getrusage 抛异常时 **rss_bytes 已经采到**、
    # 只有 peak_rss_bytes 缺失。断言要跟着实现顺序走，不能假设"整段失败 = 什么都没有"。
    assert "threads" in out
    assert "peak_rss_bytes" not in out, "getrusage 失败时峰值不该出现"


def test_process_metrics_samples_current_before_peak():
    """回归（CI 第三次红）：POSIX 分支必须**先读当前 RSS、再读峰值**。

    内核的 `ru_maxrss` 按页记账惰性更新，会短暂滞后于 `/proc/self/statm` 的当前值。
    先读峰值再读当前，会让 "peak < rss" 的窗口更大（CI 实测差 4KB）。
    这里用源码顺序断言（行为在单测里无法稳定复现那个 4KB 窗口）。
    """
    import inspect
    import metrics as m

    src = inspect.getsource(m.process_metrics)
    # 只看**代码行**：注释里也提到这两个名字，按整段字符串位置判断会被自己写的注释误导
    code = chr(10).join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    assert code.index("/proc/self/statm") < code.index("ru_maxrss"), \
        "必须先读 statm（当前）再读 ru_maxrss（峰值），否则 peak<rss 的窗口更大"


# ── v6.3：/audit 端点（审计 JSONL 尾部）──────────────


def _audit_file(tmp_path, n=8, bad_tail=False):
    import json as _j
    p = tmp_path / "audit.jsonl"
    rows = [{"event": "solve", "req_id": f"r{i}", "host": f"s{i}.com", "ok": i % 3 != 0,
             "error_code": "" if i % 3 else "unsolved", "ms": 1500, "ts": 1761000000 + i}
            for i in range(n)]
    text = "\n".join(_j.dumps(r) for r in rows) + "\n"
    if bad_tail:
        text += '{"half": '          # 进程被杀时的半截行
    p.write_text(text, encoding="utf-8")
    return p


def test_read_jsonl_tail_returns_newest_first(tmp_path):
    from solver.eventlog import read_jsonl_tail

    f = _audit_file(tmp_path, 8)
    got = read_jsonl_tail(str(f), 3)
    assert len(got["lines"]) == 3
    assert got["lines"][0]["req_id"] == "r7", "最新的必须在最前（与 /logs 契约一致）"


def test_read_jsonl_tail_skips_half_written_line(tmp_path):
    """审计是 append 写入，进程被杀时最后一行可能是半截 JSON。

    一行坏**不能**把端点弄挂 —— 运维恰恰在事故后最需要看它。
    """
    from solver.eventlog import read_jsonl_tail

    f = _audit_file(tmp_path, 5, bad_tail=True)
    got = read_jsonl_tail(str(f), 10)
    assert len(got["lines"]) == 5, "半截行应被跳过，其余行照常返回"
    assert "无法解析" in got["note"], "跳过的行数必须如实告知，不能静默"


def test_read_jsonl_tail_is_bounded(tmp_path):
    """大文件只读尾部一段 —— 这是本函数存在的**主要**理由（内存）。"""
    from solver.eventlog import read_jsonl_tail

    f = tmp_path / "big.jsonl"
    line = '{"event":"x","req_id":"' + "a" * 200 + '"}\n'
    f.write_text(line * 20000, encoding="utf-8")     # ≈4MB
    got = read_jsonl_tail(str(f), 5, max_bytes=64 * 1024)
    assert len(got["lines"]) == 5
    assert got["truncated"] is True, "只读了尾部一段时必须如实标注"


def test_read_jsonl_tail_notes_for_missing_cases(tmp_path):
    from solver.eventlog import read_jsonl_tail

    assert "未开启" in read_jsonl_tail("", 5)["note"]
    assert "不可读" in read_jsonl_tail(str(tmp_path / "nope"), 5)["note"]
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert "为空" in read_jsonl_tail(str(empty), 5)["note"]


def test_audit_endpoint_requires_token(live_server):
    base = live_server
    status, _ = _get(base, "/audit")
    assert status == 401


def test_audit_endpoint_serves_tail_and_clamps_lines(live_server, monkeypatch, tmp_path):
    import json as _j
    base = live_server
    # ⚠️ fixture 必须**超过** MAX_LOG_LINES，否则"夹取"断言是空转的：
    # 之前这里只造了 6 行，`lines=99999` 无论夹不夹都 <= 200 —— 于是漏掉了
    # `/audit?lines=100000` 真能吐 3506 行的缺陷（实测 524KB）。
    f = _audit_file(tmp_path, MAX_LOG_LINES + 300)
    # 必须 patch 脚本模式那个 Handler：live_server fixture 用的是 `import service`
    # （sys.path 里插了 solver/），而 `solver.service` 是另一个模块对象、另一个 Handler 类。
    import service as svc_mod
    monkeypatch.setattr(svc_mod.Handler, "audit_path", str(f))
    status, body = _get(base, "/audit?lines=2", "test-token")
    assert status == 200
    data = _j.loads(body)
    assert len(data["lines"]) == 2
    # lines 是调用方可控输入：超大值必须被夹取，不能返回全文件
    status, body = _get(base, "/audit?lines=99999", "test-token")
    rows = _j.loads(body)["lines"]
    assert status == 200
    assert len(rows) == MAX_LOG_LINES, f"夹取失效：拿到 {len(rows)} 行"
    # 0/负数：与 `/logs?lines=0` 一致 —— 要 0 行就回 0 行，不许"至少一行"
    for q in ("/audit?lines=0", "/audit?lines=-5"):
        status, body = _get(base, q, "test-token")
        assert status == 200 and _j.loads(body)["lines"] == [], q


def test_audit_endpoint_without_audit_log_says_so(live_server, monkeypatch):
    """没开 --audit-log 时要说清楚原因，不能返回一个空数组让人以为"没事件"。"""
    import json as _j
    base = live_server
    import service as svc_mod
    monkeypatch.setattr(svc_mod.Handler, "audit_path", "")
    status, body = _get(base, "/audit", "test-token")
    assert status == 200
    assert "未开启审计" in _j.loads(body)["note"]


def test_console_renders_audit_section():
    """logdy 的价值是"浏览器里能看"；区块必须在页面里，且遵守 DOM 写入纪律。"""
    from solver.console_html import CONSOLE_PAGE_HTML

    assert 'id="audit"' in CONSOLE_PAGE_HTML
    assert "/audit?lines=" in CONSOLE_PAGE_HTML
    assert "innerHTML" not in CONSOLE_PAGE_HTML, "本页硬约束：禁止 innerHTML"
    assert "createElement" in CONSOLE_PAGE_HTML


def test_read_jsonl_tail_zero_lines_returns_zero(tmp_path):
    """`lines=0` 要 0 行 —— 与 `/logs?lines=0` 同一套夹取规则（API.md 这么承诺的）。

    回归：`read_jsonl_tail` 内部曾是 `max(1, lines)`，于是 `/audit?lines=0` 回 1 行，
    和 `/logs?lines=0` 回 0 行对不上。
    """
    from solver.eventlog import read_jsonl_tail

    f = _audit_file(tmp_path, 12)
    got = read_jsonl_tail(str(f), 0)
    assert got["lines"] == [], got
    assert got["truncated"] is False
