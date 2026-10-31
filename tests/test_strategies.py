"""多方案（引擎 / 纯协议后端）测试 —— 全部离线。

跑法：python -m pytest tests/test_strategies.py -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import cfsolve_backend as cfb  # noqa: E402
import engines as eng  # noqa: E402
from errors import ErrorCode, SolveError  # noqa: E402

UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) AppleWebKit/605.1.15 Safari/604.1"


# ── 引擎白名单与预检 ────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    (None, eng.Engine.CAMOUFOX), ("", eng.Engine.CAMOUFOX),
    ("camoufox", eng.Engine.CAMOUFOX), (" cloakbrowser ", eng.Engine.CLOAKBROWSER),
    ("patchright", eng.Engine.PATCHRIGHT),
])
def test_engine_parse(raw, expected):
    assert eng.Engine.parse(raw) is expected


def test_unknown_engine_rejected():
    with pytest.raises(ValueError) as ei:
        eng.Engine.parse("selenium")
    assert "未知 engine" in str(ei.value)


def test_preflight_camoufox_ok():
    eng.preflight(eng.Engine.CAMOUFOX)          # 本机已装 camoufox


@pytest.mark.parametrize("missing", [eng.Engine.CLOAKBROWSER, eng.Engine.PATCHRIGHT])
def test_preflight_missing_engine_gives_install_hint(missing):
    """缺依赖必须给出可执行的安装命令，而不是等第一次求解才炸。"""
    try:
        eng.preflight(missing)
    except SolveError as e:
        assert e.code is ErrorCode.NOT_CONFIGURED
        assert e.hint and ("pip install" in e.hint)
    else:
        pytest.skip(f"{missing.value} 本机已安装，跳过缺失场景")


def test_build_factory_returns_callable_for_each_engine():
    for e in eng.Engine:
        assert callable(eng.build_factory(e))


# ── 纯协议后端（cfsolve）────────────────────────────
def _profile(tmp_path) -> Path:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({
        "engine": "webkit",
        "transport": {"headers": {"User-Agent": UA}},
        "browser": {"inner_width": 390, "inner_height": 844},
        "answers": [], "challenge_page_answers": [],
    }), encoding="utf-8")
    return p


def _backend(tmp_path, binary="/bin/challenge") -> cfb.CfsolveBackend:
    return cfb.CfsolveBackend(binary, str(_profile(tmp_path)))


def test_backend_not_configured_when_binary_missing(tmp_path):
    b = _backend(tmp_path, binary=str(tmp_path / "nope"))
    assert b.is_configured() is False
    with pytest.raises(SolveError) as ei:
        b.solve("https://example.com/", 30)
    assert ei.value.code is ErrorCode.NOT_CONFIGURED


def test_backend_not_configured_when_profile_missing(tmp_path):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(tmp_path / "missing.json"))
    assert b.is_configured() is False


def test_user_agent_read_from_profile(tmp_path):
    assert _backend(tmp_path).user_agent() == UA


def _patch_run(monkeypatch, *, stdout="", stderr="", code=0):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, code, stdout, stderr)

    monkeypatch.setattr(cfb.subprocess, "run", fake_run)
    return captured


def test_backend_parses_clearance_and_passes_expected_args(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)))
    captured = _patch_run(monkeypatch, stdout="cf_clearance=ABC123\n", stderr="progress")

    out = b.solve("https://example.com/", timeout=30)

    assert out["ok"] and out["want"] == "clearance" and out["cf_clearance"] == "ABC123"
    assert out["user_agent"] == UA, "回放必须用 profile 里的 UA"
    assert out["cacheable"] is True
    cmd = captured["cmd"]
    assert cmd[0] == str(binp) and "--solve" in cmd and "--fingerprint" in cmd
    assert "https://example.com/" in cmd and "--page" in cmd
    assert "--sitekey" not in cmd, "普通挑战页不应带 sitekey"


def test_backend_widget_mode_adds_sitekey_and_blank_page(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)))
    captured = _patch_run(monkeypatch, stdout="TOKEN-XYZ\n")

    out = b.solve("https://example.com/login", timeout=30, want="turnstile",
                  sitekey="0xAAA", action="login")

    assert out["token"] == "TOKEN-XYZ" and out["want"] == "turnstile"
    assert out["cacheable"] is False, "一次性 token 不可缓存"
    assert out["token_field"] == "cf-turnstile-response"
    cmd = captured["cmd"]
    assert "--sitekey" in cmd and "0xAAA" in cmd and "--blank-page" in cmd
    assert "--action" in cmd and "login" in cmd


def test_backend_widget_requires_sitekey(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)))
    _patch_run(monkeypatch, stdout="X\n")
    with pytest.raises(SolveError) as ei:
        b.solve("https://example.com/", 30, want="turnstile")
    assert ei.value.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("stderr,expected", [
    ("Error: NotChallenged", ErrorCode.UNSOLVED),
    ("Blocked by site", ErrorCode.UNSOLVED),
    ("Rejected by Cloudflare", ErrorCode.UNSOLVED),
    ("Network error: connection refused", ErrorCode.TRANSPORT),
    ("weird failure", ErrorCode.UNSOLVED),
])
def test_backend_classifies_errors(stderr, expected):
    err = cfb.classify(stderr, 1)
    assert err.code is expected


def test_backend_nonzero_exit_raises_classified(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)))
    _patch_run(monkeypatch, stderr="Rejected", code=3)
    with pytest.raises(SolveError) as ei:
        b.solve("https://example.com/", 30)
    assert ei.value.code is ErrorCode.UNSOLVED
    assert "Rejected" in ei.value.hint or "profile" in ei.value.hint


def test_backend_timeout_maps_to_timeout_code(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)), timeout=5)

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(cfb.subprocess, "run", boom)
    with pytest.raises(SolveError) as ei:
        b.solve("https://example.com/", 30)
    assert ei.value.code is ErrorCode.TIMEOUT


def test_backend_empty_output_is_unsolved(tmp_path, monkeypatch):
    binp = tmp_path / "challenge"
    binp.write_text("x", encoding="utf-8")
    b = cfb.CfsolveBackend(str(binp), str(_profile(tmp_path)))
    _patch_run(monkeypatch, stdout="", code=0)
    with pytest.raises(SolveError) as ei:
        b.solve("https://example.com/", 30)
    assert ei.value.code is ErrorCode.UNSOLVED


# ── 路由：纯协议优先，失败回落浏览器 ────────────────
def test_router_prefers_pure_protocol_then_falls_back():
    import backends as be

    class Fake(cfb.CfsolveBackend):
        def __init__(self, name, *, fail=None, out=None):
            self._name, self._fail, self._out = name, fail, out

        def name(self):
            return self._name

        def is_configured(self):
            return True

        def solve(self, url, timeout, force=False, **kw):
            if self._fail:
                raise SolveError(self._fail, "nope", stage="wait")
            return dict(self._out or {"ok": True, "solved": True, "cf_clearance": "X", "user_agent": "UA"})

    pure = Fake("cfsolve", fail=ErrorCode.UNSOLVED)
    browser = Fake("camoufox", out={"ok": True, "solved": True, "cf_clearance": "FROM-BROWSER", "user_agent": "UA"})
    out = be.BackendRouter([pure, browser]).solve("https://e.com/", 10)
    assert out["backend"] == "camoufox"
    assert out["attempts"][0]["error_code"] == "unsolved"
