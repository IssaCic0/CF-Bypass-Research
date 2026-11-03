"""pick-prewarm-targets 的分类与清单解析测试（全部离线，不联网）。"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SPEC = Path(__file__).resolve().parent.parent / "scripts" / "pick-prewarm-targets.py"
spec = importlib.util.spec_from_file_location("pick_prewarm_targets", SPEC)
mod = importlib.util.module_from_spec(spec)
sys.modules["pick_prewarm_targets"] = mod
spec.loader.exec_module(mod)


# -- read_candidates -------------------------------------------------------


def test_reads_lines_strips_comments_and_blank(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text(
        "# 注释\n\nhttps://a.com/\nb.com\n  c.com  \n# 另一注释\n",
        encoding="utf-8",
    )
    assert mod.read_candidates(str(f)) == ["https://a.com/", "https://b.com", "https://c.com"]


def test_dedups_by_host(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("a.com\nhttps://a.com/\nhttp://a.com\nb.com\n", encoding="utf-8")
    assert mod.read_candidates(str(f)) == ["https://a.com", "https://b.com"]


def test_skips_lines_without_hostname(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("not a url\na.com\n", encoding="utf-8")
    assert mod.read_candidates(str(f)) == ["https://a.com"]


# -- classify（对照 solver/detect.py 的 recommend() 输出） -------------------


def _result(route: str) -> dict:
    return {"recommendation": {"route": route}}


@pytest.mark.parametrize(
    ("route", "expected_state"),
    [
        ("pure_protocol_may_work", mod.PICKED),  # jsd：纯协议可解
        ("browser", mod.PICKED),  # managed/interactive/embedded
        ("direct", mod.SKIPPED),  # 无防护
        ("direct_unless_blocked", mod.SKIPPED),  # CF 在场但无挑战标记
        ("browser_token", mod.SKIPPED),  # 一次性 token，保温无意义
        ("unsupported_or_browser", mod.SKIPPED),  # 非 CF 防护
    ],
)
def test_classify_routes(route, expected_state):
    state, reason = mod.classify(_result(route))
    assert state == expected_state
    assert reason  # 每个分类都必须带人话原因


def test_classify_unknown_route_is_skipped_with_warning():
    state, reason = mod.classify(_result("something_new"))
    assert state == mod.SKIPPED
    assert "未知 route" in reason


def test_classify_missing_recommend_is_skipped():
    state, reason = mod.classify({})
    assert state == mod.SKIPPED


# -- detect_one 错误分支（mock 网络层，不联网） -------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_detect_one_parses_ok(monkeypatch):
    payload = b'{"url": "https://a.com", "recommendation": {"route": "browser"}}'
    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(payload)
    )
    state, result, detail = mod.detect_one("http://x", "https://a.com", "tok", 5)
    assert state == "ok"
    assert result["recommendation"]["route"] == "browser"
    assert detail.endswith("s")


def test_detect_one_http_error(monkeypatch):
    import urllib.error

    def raise_http(request, timeout):
        raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(mod.urllib.request, "urlopen", raise_http)
    state, result, detail = mod.detect_one("http://x", "https://a.com", "bad", 5)
    assert state == "http_error"
    assert result is None
    assert "401" in detail


def test_detect_one_network_error(monkeypatch):
    import socket

    def raise_net(request, timeout):
        raise socket.timeout("timed out")

    monkeypatch.setattr(mod.urllib.request, "urlopen", raise_net)
    state, result, detail = mod.detect_one("http://x", "https://a.com", "tok", 5)
    assert state == "network_error"
    assert result is None
