"""`scripts/doctor.py::check_proxy_pool` 的契约（v6.3）。

为什么单测直接调这个函数：它是"代理池自检"的全部逻辑，而 `main()` 会起真服务、
真读环境。下面三条都是**真的错过一次**的：

  1. 一条 `--proxy-egress-ip` 拼错就整段 `return`，于是用户加了 `--verify-egress`
     却**什么都没实测**，报告里连"未实测"都不出现 —— 读起来像"没问题"。
     而这正是这条功能存在的理由（登记值和真实出口对不上 = 稳定静默 403）。
  2. `item.partition("=")` 按第一个等号切：代理口令含 `=`（base64 填充很常见）时
     把 `scheme://u:dGVzdA==@h:8080` 切成 server=`scheme://u:dGVzdA`，于是
     "出口明明在列表里"却报"不在列表里"。IP 里不可能有 `=`，所以该 `rpartition`。
  3. `--verify-egress` 默认不出站：不传这个开关时必须明确说"未实测"。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOLVER = ROOT / "solver"
if str(SOLVER) not in sys.path:
    sys.path.insert(0, str(SOLVER))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _doctor():
    if "proxypool" not in sys.modules:
        import proxypool  # noqa: F401
    spec = importlib.util.spec_from_file_location("doctor_mod", ROOT / "scripts" / "doctor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _names(rep) -> str:
    return " | ".join(f"{c['name']}:{c['detail']}" for c in rep.checks)


def _stub_verify(monkeypatch, checks=None):
    """替掉实测，保证测试不出站。"""
    import proxypool

    calls = []

    def fake(pool, *, echo_url, timeout, fetch=None):
        calls.append({"echo_url": echo_url, "urls": pool.snapshot()["entries"]})
        return checks if checks is not None else []

    monkeypatch.setattr(proxypool, "verify_egress", fake)
    return calls


def test_unknown_egress_entry_does_not_skip_verification(monkeypatch):
    """一条登记项拼错，不许把实测整段吞掉。"""
    doc = _doctor()
    calls = _stub_verify(monkeypatch)
    rep = doc.Report()
    doc.check_proxy_pool(rep, ["http://a.test:8080"],
                         ["http://typo.test:9999=203.0.113.9"],
                         verify=True, echo_url="http://echo.test/", timeout=1.0)
    text = _names(rep)
    assert "不在 --proxy 列表里" in text, text      # 拼错要报出来
    assert calls, "登记项拼错时 --verify-egress 被跳过了"  # 但实测必须照跑
    assert "实测" in text, text


def test_egress_registration_survives_password_with_equals(monkeypatch):
    """口令含 `=` 时，`scheme://u:p=@h:port=IP` 仍然能登记上。"""
    doc = _doctor()
    _stub_verify(monkeypatch)
    rep = doc.Report()
    url = "http://user:dGVzdA==@h.test:8080"
    doc.check_proxy_pool(rep, [url], [f"{url}=203.0.113.9"],
                         verify=False, echo_url="x", timeout=1.0)
    text = _names(rep)
    assert "不在 --proxy 列表里" not in text, text
    assert "全部已登记出口 IP" in text, text


def test_without_verify_flag_says_not_tested(monkeypatch):
    doc = _doctor()
    calls = _stub_verify(monkeypatch)
    rep = doc.Report()
    doc.check_proxy_pool(rep, ["http://a.test:8080"], ["http://a.test:8080=203.0.113.9"],
                         verify=False, echo_url="x", timeout=1.0)
    assert not calls, "没加 --verify-egress 就不许出站"
    assert "未实测" in _names(rep), _names(rep)


def test_mismatch_is_fail_and_names_the_consequence(monkeypatch):
    """登记值≠实测值必须是 FAIL —— 那是缓存键错位、凭证发给错出口的源头。"""
    doc = _doctor()
    import proxypool

    class FakeCheck:
        proxy, registered, observed, ok = "http://a.test:8080", "1.2.3.4", "9.9.9.9", False
        detail = "**不一致**：登记 1.2.3.4 但实测 9.9.9.9"

    monkeypatch.setattr(proxypool, "verify_egress", lambda *a, **k: [FakeCheck()])
    rep = doc.Report()
    doc.check_proxy_pool(rep, ["http://a.test:8080"], ["http://a.test:8080=1.2.3.4"],
                         verify=True, echo_url="x", timeout=1.0)
    assert rep.worst() == doc.FAIL, _names(rep)
    assert any("静默 403" in c["hint"] for c in rep.checks if c["level"] == doc.FAIL)
