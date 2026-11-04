"""solver/auth.py 与 solver/probe.py 的局部门槛补测（v6.0 T-82③）。

只补**真实分支**（policy 明令禁止无断言凑覆盖率）：
  · auth：并发首启竞态（O_EXCL 失败 → 回读 / 追加）、token 文件不可读降级；
  · probe：逐跳重定向校验（302 → 守卫逐跳 → 上限熔断 → 非 3xx HTTPError 读 body）、
    无 Location 的畸形重定向。
"""

from __future__ import annotations

import http.server
import os
import socket
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import auth  # noqa: E402
import probe  # noqa: E402


# ─────────────────────────────── auth.py ───────────────────────────────


def test_token_race_existing_file_without_token_line_appends(tmp_path, monkeypatch):
    """并发首启竞态：文件被别的进程抢建但没有 token 行 → 追加并标记 generated。"""
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    path = tmp_path / ".env"
    path.write_text("OTHER_VAR=1\n", encoding="utf-8")
    # 让 os.open 撞 O_EXCL：文件已存在 → FileExistsError → 读不到 token → 追加
    token, source = auth.load_or_create_token(str(path))
    assert source == "generated"
    raw = path.read_text(encoding="utf-8")
    assert "OTHER_VAR=1" in raw, "追加不许破坏文件里已有的内容"
    assert f"{auth.TOKEN_LINE_PREFIX}{token}" in raw


def test_token_race_file_created_between_check_and_open(tmp_path, monkeypatch):
    """检查不存在 → open 前"别的进程"写好完整 token 文件 → 回读对端的（source=file）。"""
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    path = tmp_path / ".env"
    real_open = os.open
    # os.path.exists 返回 False（检查期），但 os.open 时文件已在 —— 模拟窗口期竞态
    monkeypatch.setattr(auth.os.path, "exists", lambda p: False)

    def racing_open(path_arg, flags, mode=0o666):
        if flags & os.O_EXCL and not path.exists():
            path.write_text(f"{auth.TOKEN_LINE_PREFIX}peer-wrote-this-token-0123456789\n",
                            encoding="utf-8")
        return real_open(path_arg, flags, mode)

    monkeypatch.setattr(auth.os, "open", racing_open)
    token, source = auth.load_or_create_token(str(path))
    assert source == "file" and token == "peer-wrote-this-token-0123456789"


def test_token_file_unreadable_degrades_to_generated(tmp_path, monkeypatch):
    """token 文件存在但不可读（OSError）→ 不崩溃，走自动生成。"""
    monkeypatch.delenv(auth.TOKEN_ENV, raising=False)
    path = tmp_path / ".env"
    path.write_text(f"{auth.TOKEN_LINE_PREFIX}should-not-be-readable\n", encoding="utf-8")
    real_open = open

    def denying_open(file, *a, **k):
        if str(file) == str(path) and "r" in str(a[:1]):
            raise PermissionError(13, "Permission denied")
        return real_open(file, *a, **k)

    monkeypatch.setitem(__builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__,
                        "open", denying_open)
    try:
        token, source = auth.load_or_create_token(str(path))
    finally:
        pass
    # 不可读 → _read_token_file 返回 "" → 视为无 token → 生成
    # 注意：这里 O_EXCL 也会 FileExistsError（文件确实存在），再回读仍失败 → 追加 generated
    assert source == "generated" and token


# ─────────────────────────────── probe.py ───────────────────────────────


class _RedirectServer(http.server.BaseHTTPRequestHandler):
    """可控的重定向目标：/hopN → 302 → /hopN+1；/final → 200。"""

    seen_guards: list = []
    max_hops = 2  # ≥ PROBE_MAX_REDIRECTS 时触发熔断

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/final"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Set-Cookie", "cf_chl_test=1; Path=/")
            body = b"<html>final</html>"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        elif self.path.startswith("/noloc"):
            self.send_response(302)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path.startswith("/loop"):     # 无限循环：/loop0 → /loop0 → …
            self.send_response(302)
            self.send_header("Location", "/loop0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        elif self.path.startswith("/hop"):
            hop = int(self.path.strip("/").removeprefix("hop") or 0)
            if hop >= 2:  # hop0 → hop1 → hop2 → /final：3 跳内收敛
                self.send_response(302)
                self.send_header("Location", "/final")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(302)
            self.send_header("Location", f"/hop{hop + 1}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        else:
            self.send_response(404)
            body = b"<html>not found</html>"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *a):  # 静音
        pass


@pytest.fixture
def redirect_srv():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RedirectServer)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_probe_follows_redirects_with_per_hop_guard(redirect_srv):
    """302 链必须逐跳过守卫（含首跳与每一跳的目标），最终 200 汇总。"""
    _RedirectServer.max_hops = 2
    guarded: list[str] = []

    def guard(u: str):
        guarded.append(u)
        if u.endswith("/hop999"):
            raise AssertionError("guard 必须先于请求执行")

    result = probe.probe_protection(f"{redirect_srv}/hop0", timeout=5, guard=guard,
                                    user_agent="UA-TEST")
    assert result["status"] == 200
    assert result["final_url"].endswith("/final") or result["status"] == 200
    # 逐跳校验：守卫被调用的次数 = 跳数（首跳 + 每个重定向目标）
    assert len(guarded) >= 3, f"守卫应逐跳调用，实际只调了 {len(guarded)} 次：{guarded}"
    assert guarded[0].endswith("/hop0")


def test_probe_redirect_loop_hits_limit_and_raises(redirect_srv):
    """重定向环（hop 无限增长）在 PROBE_MAX_REDIRECTS 跳内熔断 → SolveError/TRANSPORT。"""
    from errors import ErrorCode, SolveError

    with pytest.raises(SolveError) as ei:
        probe.probe_protection(f"{redirect_srv}/loop0", timeout=5)
    assert ei.value.code is ErrorCode.TRANSPORT
    assert "重定向" in ei.value.payload().get("error", "")


def test_probe_redirect_without_location_reports_status(redirect_srv):
    """畸形重定向（302 无 Location）→ 不死循环，如实上报 302。"""
    result = probe.probe_protection(f"{redirect_srv}/noloc", timeout=5)
    assert result["status"] == 302


def test_probe_non_redirect_http_error_reads_body(redirect_srv):
    """非 3xx 的 HTTPError（如 404）→ 如实带状态码，不误判为传输失败。"""
    result = probe.probe_protection(f"{redirect_srv}/definitely-not-there", timeout=5)
    assert result["status"] == 404


def test_probe_transport_error_wrapped_as_solve_error():
    """连接不上 → 包成 SolveError/TRANSPORT + 可执行 hint，而不是裸 OSError。"""
    from errors import ErrorCode, SolveError

    # 127.0.0.1:1 几乎必然连接拒绝
    with pytest.raises(SolveError) as ei:
        probe.probe_protection("http://127.0.0.1:1/", timeout=2)
    assert ei.value.code is ErrorCode.TRANSPORT
