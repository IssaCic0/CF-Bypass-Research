"""保活池测试（离线：用假 solver，不联网、不起浏览器）。

跑法：python -m pytest tests/test_prewarm.py -q
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import prewarm as pw  # noqa: E402


class FakeSolver:
    def __init__(self, remaining: dict[str, float] | None = None, fail: set[str] | None = None):
        self.remaining = remaining or {}
        self.fail = fail or set()
        self.solved: list[str] = []

    def cache_remaining(self, url: str) -> float:
        return self.remaining.get(url, -1.0)

    def solve(self, url: str, timeout: float = 60.0, force: bool = False):
        self.solved.append(url)
        if url in self.fail:
            raise RuntimeError("solve failed")
        self.remaining[url] = 1500.0     # 续期后变长
        return {"ok": True}


# ── 目标解析 ────────────────────────────────────────
def test_parse_targets_normalizes_and_dedupes():
    got = pw.parse_targets("example.com, https://a.com/,example.com;b.com")
    assert got == ["https://example.com/", "https://a.com/", "https://b.com/"]


def test_parse_targets_reads_file_with_comments(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("# 保活清单\n\nexample.com\nhttps://b.com\n  # 注释行\nc.com\n", encoding="utf-8")
    assert pw.parse_targets("", str(f)) == ["https://example.com/", "https://b.com/", "https://c.com/"]


def test_parse_targets_missing_file_raises():
    with pytest.raises(ValueError):
        pw.parse_targets("", "/definitely/not/here.txt")


# ── 单轮行为 ────────────────────────────────────────
def _prewarmer(solver, targets, **kw):
    kw.setdefault("jitter", 0)          # 测试里不要抖动，保证确定性
    kw.setdefault("interval", 3600)
    return pw.Prewarmer(solver, targets, **kw)


def test_skips_hosts_with_plenty_of_life_left():
    s = FakeSolver({"https://fresh.example/": 1200.0})
    p = _prewarmer(s, ["https://fresh.example/"], renew_before=300)
    p.run_once()
    assert s.solved == [], "剩余寿命足够时不该做无意义求解"
    assert p.stats.skipped == 1 and p.stats.renewed == 0


def test_renews_hosts_below_threshold():
    s = FakeSolver({"https://soon.example/": 30.0})
    p = _prewarmer(s, ["https://soon.example/"], renew_before=300)
    p.run_once()
    assert s.solved == ["https://soon.example/"]
    assert p.stats.renewed == 1
    assert p.stats.per_host["https://soon.example/"]["failures"] == 0


def test_no_cache_entry_is_renewed():
    """从没解过的域（remaining=-1）也该预热，这正是"冷路径"的来源。"""
    s = FakeSolver({})
    p = _prewarmer(s, ["https://cold.example/"])
    p.run_once()
    assert s.solved == ["https://cold.example/"]


def test_failure_sets_backoff_and_skips_next_round():
    s = FakeSolver({"https://bad.example/": 10.0}, fail={"https://bad.example/"})
    p = _prewarmer(s, ["https://bad.example/"], renew_before=300, backoff_base=300)
    p.run_once()
    assert p.stats.failures == 1
    assert p.stats.last_error.startswith("https://bad.example/")

    calls_before = len(s.solved)
    p.run_once()                       # 第二轮：还在退避窗口内，不该再打
    assert len(s.solved) == calls_before, "失败退避期间不得原速重试（会加深 IP 负面印象）"
    assert p.stats.per_host["https://bad.example/"]["next_attempt_in_sec"] > 0


def test_success_after_failure_clears_backoff():
    s = FakeSolver({"https://flaky.example/": 10.0})
    p = _prewarmer(s, ["https://flaky.example/"], renew_before=300)
    s.fail = {"https://flaky.example/"}
    p.run_once()
    assert p.stats.failures == 1
    s.fail = set()
    p._hosts[0].next_attempt_at = 0.0  # 手动解除退避（模拟时间流逝）
    p.run_once()
    assert p.stats.renewed == 1
    assert p.stats.per_host["https://flaky.example/"]["failures"] == 0


def test_one_bad_host_does_not_block_others():
    s = FakeSolver({"https://bad.example/": 5.0, "https://good.example/": 5.0},
                   fail={"https://bad.example/"})
    p = _prewarmer(s, ["https://bad.example/", "https://good.example/"], renew_before=300)
    p.run_once()
    assert "https://good.example/" in s.solved, "一个域失败不能影响其它域的续期"


def test_thread_survives_unexpected_exception():
    """保活线程里的异常绝不能让线程静默退出。"""

    class Boom:
        def cache_remaining(self, url):
            raise RuntimeError("boom")

        def solve(self, *a, **k):
            raise RuntimeError("boom")

    p = _prewarmer(Boom(), ["https://x.example/"])
    p.start()
    time.sleep(0.3)
    p.stop()
    p.join(timeout=3)
    assert not p.is_alive()
    assert p.stats.runs >= 1, "至少跑过一轮，说明异常没有打断主循环"


def test_stats_are_json_friendly():
    import json

    s = FakeSolver({"https://a.example/": 0.0})
    p = _prewarmer(s, ["https://a.example/"])
    p.run_once()
    blob = json.dumps(p.stats.as_dict(), ensure_ascii=False)
    assert '"enabled": true' in blob and "renewed" in blob
