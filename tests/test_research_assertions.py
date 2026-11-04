"""结构断言矩阵（v6.0 T-68）—— 把"文档里的假设"变成"可跑的红/绿断言"。

数据驱动自 `corpus/INDEX.json`：
  - stage2 的 6 类结构断言（dispatcher / collection fn / probe state /
    counter / token / input）
  - source 的 3 条提取假设（A/B/C）

设计要点（防假绿，见指南 §3 T-68）：
  - 无样本 → skip 且 reason 必含「待授权清单」（conftest 汇总统计）
  - 合成样本用例**永不 skip**：`test_assertion_framework_runs_on_synthetic_corpus`
  - 「断言真的在断言」的唯一证明：`test_oracle_is_red_when_structure_changes`
    （合成样本 counter 字段改名 → 红）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import corpus  # noqa: E402

SKIP_REASON = (
    "corpus empty: no real session snapshot; "
    "see plans/下一步改进指南.md §6 待授权清单"
)

#: stage2 的 6 类结构断言（名字 = manifest 断言矩阵的 key）
STAGE2_ASSERTIONS = (
    "dispatcher",
    "collection_fn",
    "probe_state",
    "counter",
    "token",
    "input",
)
SOURCE_ASSERTIONS = ("A_inline_chl_opt", "B_static_token", "C_encoder_shape")


def _load_index():
    idx_path = corpus.CORPUS_ROOT / "INDEX.json"
    if not idx_path.exists():
        return None
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    # 只取真样本：合成样本不参与"结构假设是否被现实满足"的判定
    real = [e for e in idx.get("entries", []) if e.get("real")]
    return real or None


def _entry_dir(entry: dict) -> Path:
    return corpus.CORPUS_ROOT / entry["id"]


# --------------------------------------------------------------- 断言本体


def assert_stage2_structure(js: str) -> dict:
    """对 deobfuscated JS 做 6 类结构断言，返回 {断言名: bool}。

    语义（对应 stage2/analysis/* 的离线等价物，字面量级匹配）：
    """
    return {
        "dispatcher": "dispatcher" in js,
        "collection_fn": "collection" in js,
        "probe_state": "probe" in js,
        "counter": "counter" in js,
        "token": "token" in js,
        "input": "input" in js,
    }


def assert_source_hypotheses(js: str) -> dict:
    """source 的 3 条提取假设（§2.1 A/B/C）。"""
    return {
        # A：inline JS 含 _cf_chl_opt
        "A_inline_chl_opt": "_cf_chl_opt" in js,
        # B：setRequestHeader('cf-chl', ...) 里的 token 是静态串
        "B_static_token": "setRequestHeader" in js and "cf-chl" in js,
        # C：encoder / alphabet / modulus 可用固定形态读出
        "C_encoder_shape": ("alphabet" in js and "modulus" in js)
        or "encoder" in js,
    }


def run_assertions(js: str) -> dict:
    out = assert_stage2_structure(js)
    out.update(assert_source_hypotheses(js))
    return out


# --------------------------------------------------------------- 真样本用例


@pytest.mark.parametrize("assertion", STAGE2_ASSERTIONS + SOURCE_ASSERTIONS)
def test_structure_assertion_on_real_corpus(assertion):
    """真样本上每条断言必须被**执行**（pass 或 fail 都算执行），不许静默吞掉。

    本用例刻意**不**断言"一定 pass"：假设本身会被现实推翻——本轮真机实测里
    `C_encoder_shape` 在内联脚本上就是不成立的（真实发现，已记录）。断言矩阵的
    价值是"假设过期就可见"，不是"永远绿"。
    """
    import research

    entries = _load_index()
    if not entries:
        pytest.skip(SKIP_REASON)
    judged: list[tuple[str, bool]] = []
    for e in entries:
        session_dir = _entry_dir(e)
        js_path = corpus.pick_deobf(session_dir)
        assert js_path is not None, f"{e['id']}: 语料目录里没有 *.deobfuscated.js"
        if assertion in STAGE2_ASSERTIONS and not research._has_stage2_program(session_dir):
            continue  # 该样本没走到 stage2 → 不可判定，不算失败
        hits = run_assertions(js_path.read_text(encoding="utf-8", errors="replace"))
        assert assertion in hits, f"断言 [{assertion}] 没有产出结果"
        judged.append((e["id"], hits[assertion]))
    if not judged:
        pytest.skip("真样本都不含 stage2 程序 — " + SKIP_REASON)
    assert all(isinstance(v, bool) for _, v in judged), judged


# --------------------------------------------------------------- 合成样本（永不 skip）


SYNTHETIC_JS = """
var _cf_chl_opt = {cType: 'managed'};
var dispatcher = function(state) { return state; };
var collection = function() {};
var probe = {state: 0};
var counter = 0;
var token = setRequestHeader('cf-chl', 'static-token-string');
var input = document.input;
var alphabet = 'abcdef', modulus = 65537;
var encoder = {alphabet: alphabet, modulus: modulus};
"""


@pytest.fixture()
def synthetic_corpus(tmp_path, monkeypatch):
    """把合成样本注册进一个独立的 corpus 根，供框架自检用。

    带 `response.decoded.txt`：让样本"含 stage2 程序"，矩阵里 stage2 的 6 条
    才可判定（否则按判据会被标成 skip —— 那是另一条用例专测的行为）。
    """
    croot = tmp_path / "syn-corpus"
    monkeypatch.setattr(corpus, "CORPUS_ROOT", croot)
    sess = tmp_path / "syn-session"
    sess.mkdir()
    (sess / "iframe.deobfuscated.js").write_text(SYNTHETIC_JS, encoding="utf-8")
    (sess / "response.decoded.txt").write_text("probe response body", encoding="utf-8")
    r = corpus.cmd_add(str(sess), real=False)
    assert r["ok"], r
    return Path(r["dir"])


def test_stage2_assertions_skip_when_sample_has_no_stage2_program(tmp_path, monkeypatch):
    """只到入口脚本的样本：stage2 断言 skip（不可判定），不报 fail。

    真机实测：内存爆炸/未走到 probe POST 的会话就是这样。把"缺输入"报成
    "结构不符"会误导成"CF 改版了"。
    """
    import research

    croot = tmp_path / "half-corpus"
    monkeypatch.setattr(corpus, "CORPUS_ROOT", croot)
    sess = tmp_path / "half-session"
    sess.mkdir()
    (sess / "orchestrator.deobfuscated.js").write_text(SYNTHETIC_JS, encoding="utf-8")
    assert corpus.cmd_add(str(sess), real=True)["ok"]

    report = research.assertions_matrix()
    row = report["variants"][0]
    for name in STAGE2_ASSERTIONS:
        assert row["assertions"][name] == "skip", row
    for name in SOURCE_ASSERTIONS:
        assert row["assertions"][name] == "pass", row
    assert "未走到 stage2" in row["skip_reason"]


def test_assertion_framework_runs_on_synthetic_corpus(synthetic_corpus):
    """框架自检：合成样本上 9 条断言全部命中。永不 skip。"""
    js = (synthetic_corpus / "iframe.deobfuscated.js").read_text(encoding="utf-8")
    hits = run_assertions(js)
    expected = set(STAGE2_ASSERTIONS + SOURCE_ASSERTIONS)
    assert set(hits) == expected
    bad = [k for k, v in hits.items() if not v]
    assert not bad, f"合成样本应命中全部断言，失败: {bad}"


def test_oracle_is_red_when_structure_changes(synthetic_corpus):
    """改名即红：counter 字段消失 → 对应断言失败（这是"断言真的在断言"的证明）。"""
    js = (synthetic_corpus / "iframe.deobfuscated.js").read_text(encoding="utf-8")
    mutated = js.replace("counter", "cnt_renamed")
    hits = assert_stage2_structure(mutated)
    assert hits["counter"] is False
    assert hits["dispatcher"] is True  # 其余断言不受影响


# --------------------------------------------------------------- 矩阵输出


def test_matrix_report_on_synthetic(synthetic_corpus, tmp_path, monkeypatch):
    """research.assertions_matrix 在合成样本上产出全 pass 矩阵，可写证据 JSON。"""
    import research

    out_path = tmp_path / "research-assertions-test.json"
    report = research.assertions_matrix(out_path=out_path, include_synthetic=True)
    assert report["totals"]["fail"] == 0
    assert report["totals"]["pass"] >= 9
    assert out_path.exists()
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["totals"]["pass"] == report["totals"]["pass"]
