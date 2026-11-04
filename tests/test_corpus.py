"""`scripts/corpus.py` 的测试（v6.0 T-66）。

全部用 `tmp_path` 造合成 session 快照，**离线**，不碰任何真样本。
验收用例（改前必红精神）：add 幂等 / redact --check 拦 cf_clearance 明文 /
verify 查篡改 / 合成样本不可标 real:true。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import corpus  # noqa: E402


def make_synthetic_session(root: Path, clearance: str | None = None) -> Path:
    """造一份最小但结构完整的合成 cfsolve 会话目录。"""
    sess = root / "session-001"
    sess.mkdir()
    (sess / "iframe.deobfuscated.js").write_text(
        "var x = 1; var dispatcher = function(){return 2};\n", encoding="utf-8"
    )
    (sess / "iframe.js").write_text("/* obfuscated */", encoding="utf-8")
    (sess / "response.decoded.txt").write_text("resp-body", encoding="utf-8")
    (sess / "options.json").write_text(
        json.dumps({"page": "https://sub.example.com/path", "ua": "test-ua"}),
        encoding="utf-8",
    )
    (sess / "capture.json").write_text(
        json.dumps({"host": "sub.example.com"}), encoding="utf-8"
    )
    if clearance:
        (sess / "iframe.js").write_text(
            f"var c = 'cf_clearance={clearance}';\n", encoding="utf-8"
        )
    return sess


# ---------------------------------------------------------------- add


def test_corpus_add_accepts_orchestrator_naming(tmp_path, monkeypatch):
    """新版 cfsolve 的会话产物叫 orchestrator.*（2026-11-04 真机实测）。

    只认 iframe.* 会把真实会话拒收，等于"真抓到样本却入不了库"。
    """
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = tmp_path / "session-orch"
    sess.mkdir()
    (sess / "orchestrator.js").write_text("/* obfuscated */", encoding="utf-8")
    (sess / "orchestrator.deobfuscated.js").write_text(
        "var dispatcher = function(){return 2};\n", encoding="utf-8"
    )
    (sess / "page.html").write_text("<html></html>", encoding="utf-8")

    assert corpus.pick_deobf(sess).name == "orchestrator.deobfuscated.js"
    r = corpus.cmd_add(str(sess))
    assert r["ok"], r
    dest = Path(r["dir"])
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    names = {f["name"] for f in manifest["files"]}
    assert "orchestrator.deobfuscated.js" in names
    assert manifest["source_sha8"] == dest.name.rsplit("-", 1)[1]


def test_corpus_add_creates_stable_hash_dir(tmp_path, monkeypatch):
    """同一份目录 add 两次 → 同一路径，第二次幂等。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)

    r1 = corpus.cmd_add(str(sess))
    assert r1["ok"], r1
    d1 = Path(r1["dir"])
    assert d1.is_dir()
    assert (tmp_path / "corpus").resolve() in d1.resolve().parents
    # 目录名 = <UTC日期>-<8位哈希>（日期本身含 '-'，用 rsplit）
    head, h8 = d1.name.rsplit("-", 1)
    assert len(h8) == 8 and len(head.split("-")) == 3  # YYYY-MM-DD

    m1 = json.loads((d1 / "manifest.json").read_text(encoding="utf-8"))

    # 第二次 add：同一路径，不重复写
    r2 = corpus.cmd_add(str(sess))
    assert r2["ok"] and Path(r2["dir"]) == d1
    assert r2.get("idempotent") is True
    m2 = json.loads((d1 / "manifest.json").read_text(encoding="utf-8"))
    assert m1 == m2

    # 哈希来自 iframe.deobfuscated.js 的 sha256 前 8 位
    import hashlib

    expect = hashlib.sha256(
        (sess / "iframe.deobfuscated.js").read_bytes()
    ).hexdigest()[:8]
    assert d1.name.endswith(expect)


def test_corpus_add_synthetic_never_real(tmp_path, monkeypatch):
    """合成样本不可标 real:true —— 防止"误以为归档了真样本"。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    r = corpus.cmd_add(str(sess))
    m = json.loads((Path(r["dir"]) / "manifest.json").read_text(encoding="utf-8"))
    assert m.get("real") is not True
    # 脱敏后 host 只留 eTLD+1
    assert m["target"]["host"] == "example.com"


def test_corpus_add_requires_deobfuscated(tmp_path, monkeypatch):
    """没有 *.deobfuscated.js 的目录拒绝入库。

    文件名随 CF/cfsolve 版本变（iframe.* → orchestrator.*），因此判据是后缀而非固定名。
    """
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = tmp_path / "bad-session"
    sess.mkdir()
    rc, msg = corpus.cmd_add(str(sess), _rc=True)
    assert rc != 0 and "deobfuscated" in msg


# ---------------------------------------------------------------- list / verify


def test_corpus_list_and_verify(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    r = corpus.cmd_add(str(sess))

    rc = corpus.cmd_list()
    assert rc == 0
    out = capsys.readouterr().out
    assert Path(r["dir"]).name in out

    rc = corpus.cmd_verify()
    assert rc == 0

    # 篡改一个字节 → verify exit 1
    d = Path(r["dir"])
    f = d / "iframe.deobfuscated.js"
    f.write_text(f.read_text(encoding="utf-8") + "\n// tampered", encoding="utf-8")
    rc2 = corpus.cmd_verify()
    assert rc2 != 0


def test_corpus_verify_detects_tampering_by_name(tmp_path, monkeypatch):
    """验收用例：改一个字节 → exit 1。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    r = corpus.cmd_add(str(sess))
    d = Path(r["dir"])
    (d / "response.decoded.txt").write_bytes(b"resp-bodX")
    assert corpus.cmd_verify() == 1


# ---------------------------------------------------------------- redact --check


def test_corpus_scan_detects_json_shaped_secrets(tmp_path):
    """JSON 形态的凭据必须命中（旧正则只认 `cf_clearance=`，对 JSON 全部漏检）。"""
    d = tmp_path / "json-snap"
    d.mkdir()
    (d / "capture.json").write_text(json.dumps({
        "cf_clearance": "abcdef123456XYZ",
        "token": "supersecrettoken123",
        "headers": {"Cookie": "a=1; b=2"},
        "Authorization": "Bearer abcdefghijkl",
    }), encoding="utf-8")
    kinds = {h["kind"] for h in corpus.scan_secrets(d)}
    assert "cf_clearance_plaintext" in kinds, kinds
    assert "token_assignment" in kinds, kinds
    assert "cookie_header" in kinds, kinds
    assert "authorization_bearer" in kinds, kinds


def test_corpus_add_refuses_when_redaction_leaves_plaintext(tmp_path, monkeypatch):
    """脱敏后仍命中 → 默认拒绝入库（且不留半成品目录）。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    # redact_text 不处理 `secret` 键，因此它会作为残留明文被闸门抓到
    (sess / "iframe.deobfuscated.js").write_text(
        'var k = {"secret": "abcdefgh12345678"};\n', encoding="utf-8"
    )
    r = corpus.cmd_add(str(sess))
    assert not r["ok"], r
    assert "敏感明文" in r["error"]
    assert not list((tmp_path / "corpus").glob("*")) or all(
        not (p / "manifest.json").exists() for p in (tmp_path / "corpus").glob("*")
    ), "被拒绝的样本不得留下可用的入库目录"


def test_corpus_redact_check_flags_plaintext_in_raw_dir(tmp_path, capsys):
    """对**未入库的原始目录**，redact --check 必须报明文并给文件与行号。

    闸门的第一半：原始输入里有明文就要看得见（报行号、不回显值本身）。
    """
    sess = make_synthetic_session(tmp_path, clearance="abc123SECRET")
    rc = corpus.cmd_redact_check(str(sess))
    out = capsys.readouterr().err
    assert rc == 1
    assert "iframe.js" in out
    assert "1" in out
    assert "cf_clearance" in out
    assert "abc123SECRET" not in out, "报告里绝不许回显明文值"


def test_corpus_add_refuses_plaintext_in_js(tmp_path, monkeypatch):
    """JS 文本**不做**就地脱敏（整篇替换会写坏样本）→ 含明文即拒绝入库。

    这是复审 N1 的结论：宁可拒绝入库，也不许产出"看着脱敏了其实结构被改坏"的语料。
    """
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path, clearance="abc123SECRET")
    r = corpus.cmd_add(str(sess))
    assert not r["ok"], r
    assert "敏感明文" in r["error"]
    assert not list((tmp_path / "corpus").glob("*/manifest.json")), "被拒绝的样本不得留下入库目录"


def test_corpus_add_redacts_json_without_breaking_structure(tmp_path, monkeypatch):
    """JSON 走结构化脱敏：明文消失，但文件仍可 `json.loads`（结构未被写坏）。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = tmp_path / "json-session"
    sess.mkdir()
    (sess / "iframe.deobfuscated.js").write_text("var x = 1;\n", encoding="utf-8")
    (sess / "capture.json").write_text(json.dumps({
        "host": "example.com",
        "Cookie": "cf_clearance=abc123SECRET",
    }), encoding="utf-8")

    r = corpus.cmd_add(str(sess))
    assert r["ok"], r
    d = Path(r["dir"])
    stored = (d / "capture.json").read_text(encoding="utf-8")
    assert "abc123SECRET" not in stored, "明文不得落盘"
    parsed = json.loads(stored)  # 结构必须完好 —— 这正是 N1 要守的性质
    assert parsed["host"] == "example.com"
    assert corpus.cmd_redact_check(str(d)) == 0


def test_corpus_redact_passes_clean_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    r = corpus.cmd_add(str(sess))
    assert corpus.cmd_redact_check(Path(r["dir"])) == 0


def test_corpus_redact_rules(tmp_path):
    """脱敏硬规则单测：cookie 丢值 / URL query 丢弃 / host 留 eTLD+1 / clearance 只留长度。"""
    assert corpus.redact_cookie("a=1; b=2") == "a=[REDACTED]; b=[REDACTED]"
    assert corpus.redact_url_query("https://x.com/p?a=1&b=2") == "https://x.com/p"
    assert corpus.redact_host("sub.example.com") == "example.com"
    assert corpus.redact_clearance("cf_clearance=abc-123_X") == (
        "cf_clearance=[len:9]"
    )


def test_corpus_index_json(tmp_path, monkeypatch):
    """add 后生成 corpus/INDEX.json 摘要：只有元数据无内容。"""
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    sess = make_synthetic_session(tmp_path)
    r = corpus.cmd_add(str(sess))
    idx_path = tmp_path / "corpus" / "INDEX.json"
    assert idx_path.exists()
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    ids = [e["id"] for e in idx["entries"]]
    assert Path(r["dir"]).name in ids
    e = idx["entries"][0]
    # 元数据字段
    for key in ("id", "files", "total_bytes", "captured_utc"):
        assert key in e
    # 无内容：INDEX.json 不应包含样本正文
    raw = idx_path.read_text(encoding="utf-8")
    assert "dispatcher" not in raw


def test_corpus_list_empty_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_ROOT", tmp_path / "corpus")
    assert corpus.cmd_list() == 0
