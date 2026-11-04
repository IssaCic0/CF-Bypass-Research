"""`scripts/verify_real.py` 的挑战页判据测试（v5.0.2）。

为什么必须有这个文件：2026-11-04 真机实测里，判据用"整段 body 找子串 `Just a moment`"，
而 udemy 的**真实页面**（306KB）里正好含这个字符串（站点自己的 JS 文案）——
于是**回放成功**被判成**挑战页**，我据此得出"回放未通过"的**错误结论**，还写进了
CHANGELOG 与 Release notes。

判据只能有一份口径（与 Go 的 `gocfsolve.challengeMarkers` 对齐），且只能看**前缀**。
这些用例就是那个错误结论的守门人。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_real as vr  # noqa: E402


def test_marker_deep_in_body_is_not_a_challenge():
    """真实页面深处出现 marker（udemy 那种）不能判成挑战页 —— 这就是当初的误报。"""
    body = (b"<html><head><title>Real Page</title></head><body>"
            + b"content " * 3000 + b"Just a moment</body></html>")
    assert len(body) > vr.CHALLENGE_PEEK_BYTES * 2
    assert vr.is_challenge(200, {"Content-Type": "text/html; charset=utf-8"}, body) is False


def test_marker_in_prefix_is_a_challenge():
    for body in (
        b"<html><head><title>Just a moment...</title></head></html>",
        b"<html><script>window._cf_chl_opt={cType:'managed'};</script></html>",
        b'<html><script src="/cdn-cgi/challenge-platform/h/b/jsch/v1"></script></html>',
        b"<html><body>Checking your browser before accessing</body></html>",
    ):
        assert vr.is_challenge(200, {"Content-Type": "text/html"}, body) is True, body[:40]


def test_status_403_is_always_a_challenge():
    assert vr.is_challenge(403, {"Content-Type": "text/html"}, b"anything") is True


def test_cf_mitigated_header_wins():
    assert vr.is_challenge(200, {"cf-mitigated": "challenge", "Content-Type": "text/html"},
                           b"clean page") is True


def test_non_html_is_not_a_challenge():
    """JSON / 图片里出现 marker 不算挑战页（也不该去读正文）。"""
    assert vr.is_challenge(200, {"Content-Type": "application/json"},
                           b'{"just a moment": true}') is False


@pytest.mark.parametrize("status", [301, 302, 404, 500])
def test_other_statuses_are_not_challenges(status):
    assert vr.is_challenge(status, {"Content-Type": "text/html"}, b"") is False


def test_headers_may_be_urllib_message_like():
    """真实调用里 headers 是 `email.message.Message`（大小写不敏感），要能正常取到。"""

    class MsgLike(dict):
        pass

    assert vr.is_challenge(200, MsgLike({"CF-Mitigated": "challenge"}), b"x") is True
