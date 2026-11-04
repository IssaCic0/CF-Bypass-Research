"""T-79：request_id 贯穿（一次求解可定位）。

契约（plans/下一步改进指南.md T-78/T-79 系列）：
  · 入口生成 `rid = uuid4().hex[:12]`；调用方 `X-Request-Id` 合法（`[A-Za-z0-9_-]{1,64}`）则沿用，非法丢弃并新生成（不反射）
  · 出口三处都带：响应头 `X-Request-Id`、成功/错误 envelope 的 `request_id`、审计 JSONL 白名单列
  · `/solve` 的 `attempts` 每跳附 `rid`
  · **绝不**把 rid 做成 metrics label（防高基数）
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import service  # noqa: E402
from test_service import live_server, _post  # noqa: E402,F401

HOST = "https://example.com/"


def _get_full(base, path, token=None, headers=None):
    """带响应头的 GET（回显断言需要读 X-Request-Id 头）。"""
    req = urllib.request.Request(f"{base}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_request_id_generated_and_echoed(live_server):
    """未带 X-Request-Id：服务生成 12 位 hex rid，响应头与 envelope 都带。"""
    status, body, headers = _get_full(live_server, "/health")
    assert status == 200
    rid = headers.get("X-Request-Id", "")
    assert re.fullmatch(r"[0-9a-f]{12}", rid), f"rid 应为 uuid4().hex[:12]，实际 {rid!r}"
    parsed = json.loads(body)
    assert parsed.get("request_id") == rid, "envelope 的 request_id 必须与响应头一致"


def test_request_id_from_caller_is_honored_and_validated(live_server):
    """合法调用方值沿用；非法值丢弃并新生成（绝不反射回显）。"""
    # 合法：沿用
    status, _, headers = _get_full(live_server, "/health", headers={"X-Request-Id": "abc-DEF_123"})
    assert headers.get("X-Request-Id") == "abc-DEF_123"

    # 非法（含换行注入/超长/怪字符）：丢弃并新生成
    for bad in ("bad id with space", "x" * 100, "id\x00nul", "ricci<script>"):
        status, _, headers = _get_full(live_server, "/health", headers={"X-Request-Id": bad})
        rid = headers.get("X-Request-Id", "")
        assert rid != bad, f"非法值被反射回显：{bad!r}"
        assert re.fullmatch(r"[0-9a-f]{12}", rid), f"应重新生成合法 rid，实际 {rid!r}"


def test_error_envelope_contains_request_id(live_server):
    """错误 envelope 也必须带 request_id（排障第一问：这条错是哪次请求）。"""
    # GET /solve 不是合法方法 → 404 也一样是错误 envelope
    status, body, headers = _get_full(live_server, "/status")
    assert status == 401
    rid = headers.get("X-Request-Id", "")
    assert re.fullmatch(r"[0-9a-f]{12}", rid)
    assert json.loads(body).get("request_id") == rid

    # 带正确 token 但 body 坏 → 400 错误 envelope 同样带
    req = urllib.request.Request(f"{live_server}/solve", data=b"{not json", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", "Bearer test-token")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode()
    assert status == 400
    parsed = json.loads(body)
    assert parsed.get("request_id"), "错误 envelope 必须含 request_id"
    assert re.fullmatch(r"[0-9a-f]{12}", parsed["request_id"])


def test_audit_line_contains_request_id(live_server, tmp_path):
    """审计 JSONL 白名单加了 request_id 列（与 req_id 同值）。"""
    audit = tmp_path / "audit.jsonl"
    # 审计句柄是类级缓存：先关掉残留，再切路径（test_governance 的既有坑）
    if service.Handler.audit_fp is not None:
        try:
            service.Handler.audit_fp.close()
        finally:
            service.Handler.audit_fp = None
    old = service.Handler.audit_path
    service.Handler.audit_path = str(audit)
    try:
        status, body = _post(live_server, "/solve", {"url": HOST, "timeout": 10}, token="test-token")
        assert status == 200
        # 注意：handler 线程在响应发出**之后**才执行 _log（含审计写）。
        # 这里必须等它落盘后再收 fp —— 提前 close 会杀死尚未完成的写入
        # （fp 仍处于打开状态时 _audit_write 照常工作，所以只需等一小段）。
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if "solve" in audit.read_text(encoding="utf-8"):
                    break
            except FileNotFoundError:
                pass
            time.sleep(0.05)
    finally:
        service.Handler.audit_path = old
        # 先摘引用再 close（顺序反过来在本机稳定触发"文件读不到"的怪象）
        fp = service.Handler.audit_fp
        service.Handler.audit_fp = None
    if fp is not None:
        fp.close()
    lines = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines() if x.strip()]
    solve_lines = [x for x in lines if x.get("event") == "solve"]
    assert solve_lines, "solve 事件应进审计"
    rid = solve_lines[-1].get("request_id")
    assert rid and re.fullmatch(r"[0-9a-f]{12}", rid), f"审计行缺 request_id：{solve_lines[-1]}"
    assert solve_lines[-1].get("req_id") == rid


def test_solve_attempts_carry_rid(live_server, monkeypatch):
    """/solve 的 attempts 轨迹每跳附 rid（成功与失败两条路径）。"""
    solver = service.Handler.solver

    # 成功路径：假 solve 返回带 attempts 的结果
    def fake_solve_ok(*a, **k):
        return {"ok": True, "solved": True, "attempts": [
            {"backend": "camoufox", "ok": True},
            {"backend": "cfsolve", "ok": True},
        ]}

    monkeypatch.setattr(solver, "solve", fake_solve_ok)
    status, body = _post(live_server, "/solve", {"url": HOST, "timeout": 10}, token="test-token")
    assert status == 200
    attempts = body.get("attempts") or []
    assert attempts, "attempts 应原样回传"
    rid = body.get("request_id", "")
    assert all(a.get("rid") == rid for a in attempts), f"每跳都应附 rid：{attempts}"

    # 失败路径：SolveError.extra 里的 attempts 也要附 rid
    from errors import ErrorCode, SolveError

    def fake_solve_fail(*a, **k):
        raise SolveError(ErrorCode.UNSOLVED, "全链失败", stage="solve",
                         extra={"attempts": [{"backend": "camoufox", "ok": False}]})

    monkeypatch.setattr(solver, "solve", fake_solve_fail)
    status, body = _post(live_server, "/solve", {"url": HOST, "timeout": 10}, token="test-token")
    assert status == 200  # UNSOLVED 走 HTTP 200 契约
    attempts = body.get("attempts") or []
    assert attempts and all(a.get("rid") == body.get("request_id") for a in attempts)


def test_request_id_not_in_metrics_labels(live_server):
    """防高基数：metrics 文本里绝不允许出现 request_id 维度的 label。"""
    # 先制造几个带不同 rid 的请求
    for i in range(3):
        _get_full(live_server, "/status", token="test-token", headers={"X-Request-Id": f"probe-{i}"})
    status, text, _ = _get_full(live_server, "/metrics", token="test-token")
    assert status == 200
    assert "request_id" not in text, "metrics 不得含 request_id（label 或 HELP 都不许）"
    assert "X-Request-Id" not in text
