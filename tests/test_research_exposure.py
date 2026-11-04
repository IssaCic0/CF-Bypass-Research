"""T-76：/health research 段 + /metrics 三条研究序列。

契约：
  · `/health` 增加 `research` 段 —— **仅 include_internal=True（带 token）时**；免鉴权最小集不许扩
  · `/metrics` 新增三条：cfsolve_research_corpus_variants、cfsolve_research_assertions{state}、
    cfsolve_research_snapshot_age_seconds（无快照时 **NaN，不许填 0**）
  · 数据源防御性读取：corpus/INDEX.json 与断言矩阵文件缺失 → 全 0/NaN + note
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import service  # noqa: E402
from metrics import render_metrics_text  # noqa: E402
from test_service import live_server, _get, _post, factory_returning, FakeBrowser  # noqa: E402,F401

HOST = "https://example.com/"


def _research_payload(variants=2, total=6, passed=4, failed=1, skipped=1,
                      snapshot_at=None, diff_at=None, note=""):
    """与服务端 `research_status()` 的**真实**返回形状一致（三态计数，不是伪造）。"""
    return {
        "corpus_variants": variants,
        "assertions_total": total,
        "assertions_pass": passed,
        "assertions_fail": failed,
        "assertions_skipped": skipped,
        "last_snapshot_at": snapshot_at,
        "last_diff_at": diff_at,
        "note": note,
    }


def _iso_ago(seconds: float) -> str:
    """生产者写的是 ISO-8601（corpus manifest 的 captured_utc），不是 epoch 浮点。"""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ── /health research 段 ──────────────────────────────
def test_health_research_section_only_with_token(live_server):
    """免鉴权最小集**不含** research；带 token 的完整视图含且字段齐全。"""
    status, body = _get(live_server, "/health")
    assert status == 200
    minimal = json.loads(body)
    assert "research" not in minimal, "免鉴权最小集不得出现 research 段"

    status, body = _get(live_server, "/health", token="test-token")
    assert status == 200
    full = json.loads(body)
    assert "research" in full
    r = full["research"]
    for key in ("corpus_variants", "assertions_total", "assertions_skipped",
                "last_snapshot_at", "last_diff_at", "note"):
        assert key in r, f"research 段缺字段 {key}"
    assert isinstance(r["corpus_variants"], int)
    assert isinstance(r["note"], str)


def test_health_research_empty_entries_has_note(live_server, monkeypatch, tmp_path):
    """INDEX.json 存在但 entries 为空 → 必须给 note（不许静默报 0）。"""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "INDEX.json").write_text(
        json.dumps({"updated_utc": "2026-11-04T00:00:00Z", "entries": []}), encoding="utf-8"
    )
    monkeypatch.setattr(service.Handler.solver, "corpus_dir", str(corpus))
    status, body = _get(live_server, "/health", token="test-token")
    r = json.loads(body)["research"]
    assert r["corpus_variants"] == 0
    assert "entries 为空" in r["note"], r


def test_health_research_reads_corpus_when_present(live_server, monkeypatch, tmp_path):
    """corpus 目录存在时：按**生产者的真实形状**读出变体数与三态断言计数。

    INDEX.json 由 `corpus.py::_write_index` 写成 `{"entries": [...]}`（键名 entries，
    不是 variants）；assertions.json 由 `research.py::assertions_matrix` 写成
    `{assertions_total, pass, fail, skip, last_snapshot_at}`。这两份形状与真实的
    `corpus/` 产物一致 —— 用"服务端期望的形状"造夹具等于为谎言背书。
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "INDEX.json").write_text(json.dumps({"entries": [
        {"id": "a", "real": True}, {"id": "b", "real": True}, {"id": "c", "real": False},
    ]}), encoding="utf-8")
    (corpus / "assertions.json").write_text(json.dumps({
        "assertions_total": 9,
        "pass": 2,
        "fail": 1,
        "skip": 6,
        "last_snapshot_at": "2026-11-04T07:24:51Z",
        "last_diff_at": None,
        "skip_reason": "样本未走到 stage2",
    }), encoding="utf-8")
    monkeypatch.setattr(service.Handler.solver, "corpus_dir", str(corpus))
    status, body = _get(live_server, "/health", token="test-token")
    r = json.loads(body)["research"]
    assert r["corpus_variants"] == 3
    assert r["assertions_total"] == 9
    assert r["assertions_pass"] == 2
    assert r["assertions_fail"] == 1
    assert r["assertions_skipped"] == 6
    assert r["last_snapshot_at"] == "2026-11-04T07:24:51Z"
    assert "stage2" in r["note"]  # 生产者写的 skip_reason 必须透出，不许吞掉


def test_health_research_defensive_when_corpus_missing(live_server, monkeypatch, tmp_path):
    """corpus 不存在：全 0/None + note，绝不抛异常。

    显式把 corpus_dir 指到不存在的路径 —— 用例结果不该取决于宿主仓库里
    "此刻有没有真实样本"（本轮真的存进去过一份，依赖真实状态就会假红/假绿）。
    """
    monkeypatch.setattr(service.Handler.solver, "corpus_dir",
                        str(tmp_path / "no-such-corpus"))
    status, body = _get(live_server, "/health", token="test-token")
    assert status == 200
    r = json.loads(body)["research"]
    assert r["corpus_variants"] == 0
    assert r["assertions_total"] == 0
    assert r["assertions_pass"] == 0
    assert r["assertions_fail"] == 0
    assert r["assertions_skipped"] == 0
    assert r["last_snapshot_at"] is None
    assert r["last_diff_at"] is None
    assert r["note"], "corpus 为空时必须有 note（不许静默给 0）"


# ── /metrics 三条序列 ────────────────────────────────
def _metrics_base_kwargs():
    return dict(
        solves=1, hits=0, failures=0, timeouts=0, rejected_busy=0, restarts=0,
        browser_alive=True, queue_depth=0, elapsed_sum=1.0,
        hist=[0] * len(service.LATENCY_BUCKETS),
        backend_ok={}, backend_err={},
    )


def test_metrics_research_series_present():
    """三条研究序列存在，且**三态计数来自真实来源**（pass/fail/skip 各自独立）。"""
    text = render_metrics_text(
        research_fn=lambda: _research_payload(variants=2, total=6, passed=4, failed=1, skipped=1),
        **_metrics_base_kwargs())
    assert "cfsolve_research_corpus_variants 2" in text
    assert 'cfsolve_research_assertions{state="pass"} 4' in text
    # 这一条曾经被写死成 0（fail 恒 0、pass = total − skip），那是指标说谎
    assert 'cfsolve_research_assertions{state="fail"} 1' in text
    assert 'cfsolve_research_assertions{state="skip"} 1' in text


def test_metrics_snapshot_age_is_nan_when_absent():
    """无快照时 age 必须是 NaN，**不许填 0**（0 会假装"刚拍过快照"）。"""
    text = render_metrics_text(
        research_fn=lambda: _research_payload(snapshot_at=None, diff_at=None), **_metrics_base_kwargs())
    line = next(x for x in text.splitlines() if x.startswith("cfsolve_research_snapshot_age_seconds"))
    assert line.split()[-1] == "NaN", f"无快照必须是 NaN：{line}"

    # 有快照时是有限正数（生产者给的是 ISO 字符串，不是 epoch 浮点）
    text2 = render_metrics_text(
        research_fn=lambda: _research_payload(snapshot_at=_iso_ago(60)),
        **_metrics_base_kwargs())
    line2 = next(x for x in text2.splitlines() if x.startswith("cfsolve_research_snapshot_age_seconds"))
    val = float(line2.split()[-1])
    assert 55 <= val <= 120, f"age 应约 60s：{line2}"


def test_metrics_uses_real_matrix_counts_with_multiple_variants(tmp_path, monkeypatch):
    """用 `assertions_matrix` 的**真实产出**（≥2 个变体）喂 /metrics：三态不得被清零。

    复审 N2：`assertions_total` 曾写成"断言定义数=9"，而三态是跨变体求和；
    2 个变体时 sum=18≠9 → 三个 state 全被清零，`fail` 又回到"恒 0 的假绿"。
    """
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import corpus as _corpus
    import research as _research

    croot = tmp_path / "corpus"
    monkeypatch.setattr(_corpus, "CORPUS_ROOT", croot)
    good = ("var dispatcher = 1; var collection = 2; var probe = 3;\n"
            "var counter = 4; var token = 5; var input = 6;\n")
    bad = good.replace("counter", "cnt_renamed")  # 一份缺 counter → 产生真实 fail

    for name, js in (("v1", good), ("v2", bad)):
        d = tmp_path / name
        d.mkdir()
        (d / "iframe.deobfuscated.js").write_text(js, encoding="utf-8")
        (d / "response.decoded.txt").write_text("probe body", encoding="utf-8")
        assert _corpus.cmd_add(str(d), real=True)["ok"]

    report = _research.assertions_matrix()
    summary = json.loads((croot / "assertions.json").read_text(encoding="utf-8"))

    # 前提：确实有两个变体、且确实判出了 fail（否则这条用例没意义）
    assert report["totals"]["fail"] >= 1, report["totals"]
    assert summary["assertions_total"] == sum(
        summary[k] for k in ("pass", "fail", "skip")
    ), "total 必须是判定次数（跨变体求和），不是断言定义数"

    text = render_metrics_text(
        research_fn=lambda: {
            "corpus_variants": 2,
            "assertions_total": summary["assertions_total"],
            "assertions_pass": summary["pass"],
            "assertions_fail": summary["fail"],
            "assertions_skipped": summary["skip"],
            "last_snapshot_at": None,
            "last_diff_at": None,
            "note": "",
        },
        **_metrics_base_kwargs())
    assert f'cfsolve_research_assertions{{state="fail"}} {summary["fail"]}' in text
    assert 'cfsolve_research_assertions{state="fail"} 0' not in text, "fail 被清零了"


def test_metrics_snapshot_age_nan_when_timestamp_unparseable():
    """时间戳不是 ISO-8601 时 age 是 NaN —— 绝不抛异常、绝不填 0。"""
    text = render_metrics_text(
        research_fn=lambda: _research_payload(snapshot_at="not-a-timestamp"),
        **_metrics_base_kwargs())
    line = next(x for x in text.splitlines() if x.startswith("cfsolve_research_snapshot_age_seconds"))
    assert line.split()[-1] == "NaN", line


def test_metrics_research_bad_payload_degrades():
    """research_fn 返回损坏值（字符串/非数字）也只降级，不 500。"""
    text = render_metrics_text(
        research_fn=lambda: {"corpus_variants": "abc", "assertions_total": {}, "assertions_pass": []},
        **_metrics_base_kwargs())
    assert "cfsolve_research_corpus_variants 0" in text
    assert 'cfsolve_research_assertions{state="pass"} 0' in text


def test_metrics_research_fn_absent_still_renders(live_server):
    """research_fn 缺省（旧调用方/未注入）不渲染研究段也不崩。"""
    text = render_metrics_text(**_metrics_base_kwargs())
    assert "cfsolve_research" not in text


def test_metrics_research_fn_crash_degrades_not_dies():
    """research_fn 抛异常 → 研究段全 0，/metrics 本身绝不能死。"""
    def boom():
        raise RuntimeError("corpus exploded")
    text = render_metrics_text(research_fn=boom, **_metrics_base_kwargs())
    assert "cfsolve_research_corpus_variants 0" in text
    assert "cfsolve_research_snapshot_age_seconds NaN" in text


def test_metrics_host_stats_fn_crash_degrades_not_dies():
    """host_stats_fn 抛异常（既有分支，顺带补一条锁死）。"""
    def boom(_n):
        raise RuntimeError("store gone")
    text = render_metrics_text(host_stats_fn=boom, **_metrics_base_kwargs())
    assert "cfsolve_solves_total 1" in text
    assert "cfsolve_host_solves_total" not in text


def test_metrics_endpoint_carries_research_series(live_server, monkeypatch, tmp_path):
    """/metrics 端点（带 token）实际携带三条研究序列。"""
    status, text = _get(live_server, "/metrics", token="test-token")
    assert status == 200
    assert "cfsolve_research_corpus_variants" in text
    assert 'cfsolve_research_assertions{state=' in text
    assert "cfsolve_research_snapshot_age_seconds" in text
