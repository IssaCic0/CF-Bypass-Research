"""代理池与「按域粘性」测试（离线，不发任何真实网络请求）。

跑法：python -m pytest tests/test_proxypool.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import proxypool as pp  # noqa: E402
import service as svc  # noqa: E402
from test_service import FakeBrowser, FakeCM, wait_until  # noqa: E402

P1 = "http://u1:p1@gw1.example.net:8080"
P2 = "http://u2:p2@gw2.example.net:8080"


# ── 解析与脱敏 ──────────────────────────────────────
def test_parse_proxy_url_full():
    d = pp.parse_proxy_url(P1)
    assert d == {"server": "http://gw1.example.net:8080", "username": "u1", "password": "p1"}


def test_parse_proxy_url_without_scheme():
    d = pp.parse_proxy_url("gw.example.net:9000")
    assert d["server"] == "http://gw.example.net:9000"


@pytest.mark.parametrize("bad", ["", "http://hostonly", "://nohost:80"])
def test_parse_proxy_url_rejects_bad(bad):
    with pytest.raises(ValueError):
        pp.parse_proxy_url(bad)


def test_mask_proxy_url_hides_credentials():
    masked = pp.mask_proxy_url(P1)
    assert "u1" not in masked and "p1" not in masked
    assert masked == "http://gw1.example.net:8080"


# ── 粘性 ────────────────────────────────────────────
def test_same_host_always_maps_to_same_proxy():
    pool = pp.ProxyPool([P1, P2])
    first = pool.for_host("steamdb.info")
    for _ in range(20):
        assert pool.for_host("steamdb.info") is first, "同域必须粘住同一个出口"


def test_hosts_are_distributed_across_proxies():
    pool = pp.ProxyPool([P1, P2])
    hosts = [f"site{i}.example.com" for i in range(200)]
    used = {pool.for_host(h).raw_url for h in hosts}
    assert used == {P1, P2}, f"一致性哈希应把域分散到所有出口，实际只用了 {used}"


def test_adding_proxy_only_remaps_a_fraction():
    """扩容只应影响约 1/N 的域，而不是全部重排（这正是选一致性哈希的原因）。"""
    pool = pp.ProxyPool([P1, P2])
    hosts = [f"site{i}.example.com" for i in range(200)]
    before = {h: pool.for_host(h).raw_url for h in hosts}

    pool3 = pp.ProxyPool([P1, P2, "http://gw3.example.net:8080"])
    # 新池重新构建后，用同样的 hosts 比对
    after = {h: pool3.for_host(h).raw_url for h in hosts}
    changed = sum(1 for h in hosts if before[h] != after[h])
    assert changed < len(hosts), "不应把全部域都重排"
    assert changed > 0, "扩容总该有一部分域迁移到新出口"


# ── 健康与冷却 ──────────────────────────────────────
def test_failures_trigger_cooldown_then_skip():
    pool = pp.ProxyPool([P1, P2], max_consecutive_failures=2, cooldown_base=60)
    victim = pool.for_host("a.example.com")
    for _ in range(2):
        pool.record_failure(victim, "unsolved")
    assert victim.in_cooldown(), "连续失败到阈值必须进冷却"

    # 同一域名仍然粘在它身上 → 现在应该顺延到另一个健康出口
    fallback = pool.for_host("a.example.com")
    assert fallback is not None and fallback.raw_url != victim.raw_url


def test_all_cooling_down_returns_none():
    pool = pp.ProxyPool([P1], max_consecutive_failures=1, cooldown_base=60)
    only = pool.for_host("a.example.com")
    pool.record_failure(only, "unsolved")
    assert pool.for_host("a.example.com") is None


def test_success_clears_cooldown():
    pool = pp.ProxyPool([P1], max_consecutive_failures=1, cooldown_base=60)
    e = pool.for_host("a.example.com")
    pool.record_failure(e, "unsolved")
    assert e.in_cooldown()
    pool.record_success(e)
    assert not e.in_cooldown() and e.consecutive_failures == 0


def test_snapshot_never_leaks_credentials():
    pool = pp.ProxyPool([P1, P2])
    blob = json.dumps(pool.snapshot(), ensure_ascii=False)
    assert "p1" not in blob and "p2" not in blob and "u1" not in blob
    assert "gw1.example.net:8080" in blob


# ── 出口标识与缓存键联动 ────────────────────────────
def test_egress_ip_changes_egress_id_and_cache_key():
    pool = pp.ProxyPool([P1, P2])
    s = svc.Solver(browser_factory=lambda *, headless: FakeCM(FakeBrowser()),
                   allow_private_hosts=True, proxy_pool=pool)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        host = "example.com"
        entry = pool.for_host(host)
        key_before = s.cache_key(host, entry)

        pool.set_egress_ip(entry, "203.0.113.9")
        key_after = s.cache_key(host, entry)

        assert key_before != key_after, "出口 IP 变了，缓存键必须随之变化（否则凭证串味）"
        assert entry.egress_id == "ip:203.0.113.9"
    finally:
        s.close()


# ── 与 Solver 的接线 ────────────────────────────────
def test_solver_passes_proxy_to_context_and_records_success():
    captured = {}

    class CapturingBrowser(FakeBrowser):
        def new_context(self, proxy=None):
            captured["proxy"] = proxy
            return super().new_context(proxy=proxy)

    pool = pp.ProxyPool([P1])
    s = svc.Solver(browser_factory=lambda *, headless: FakeCM(CapturingBrowser()),
                   allow_private_hosts=True, proxy_pool=pool)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        r = s.solve("https://example.com/", timeout=10)
        assert captured["proxy"] == {
            "server": "http://gw1.example.net:8080",
            "username": "u1",
            "password": "p1",
        }, "求解必须真的走该出口"
        assert r["egress_id"].startswith("px:") or r["egress_id"].startswith("ip:")
        snap = s.health()[1]["proxies"]
        assert snap["entries"][0]["success"] == 1
        assert "p1" not in json.dumps(snap), "健康端点不得泄露代理密码"
    finally:
        s.close()


def test_solver_fails_fast_when_all_proxies_cooling():
    pool = pp.ProxyPool([P1], max_consecutive_failures=1, cooldown_base=60)
    s = svc.Solver(browser_factory=lambda *, headless: FakeCM(FakeBrowser()),
                   allow_private_hosts=True, proxy_pool=pool)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        entry = pool.for_host("example.com")
        pool.record_failure(entry, "unsolved")     # 唯一出口进冷却
        with pytest.raises(svc.SolveError) as ei:
            s.solve("https://example.com/", timeout=10)
        assert ei.value.code is svc.ErrorCode.BUSY
    finally:
        s.close()


def test_request_level_error_does_not_penalize_proxy():
    """参数错误不该把好代理打进冷却——只有"可能是出口的锅"的失败才算。"""
    from test_service import FakeCM

    pool = pp.ProxyPool([P1], max_consecutive_failures=1, cooldown_base=60)
    s = svc.Solver(browser_factory=lambda *, headless: FakeCM(FakeBrowser()),
                   allow_private_hosts=True, proxy_pool=pool)
    try:
        assert wait_until(lambda: s.health()[0] == 200)
        with pytest.raises(svc.SolveError) as ei:
            s.solve("ftp://bad-scheme/", timeout=10)
        assert ei.value.code is svc.ErrorCode.INVALID_REQUEST
        assert not pool.for_host("example.com").in_cooldown(), "参数错误不得冷却代理出口"
    finally:
        s.close()
