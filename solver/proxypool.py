"""代理池：为「按域粘性」提供出口，并保证缓存键随出口变化。

为什么必须有这一层（而不是"部署方自己保证同出口"）：
  · `cf_clearance` 与「域 + 出口 IP + UA」绑定。求解走 A 出口、回放走 B 出口 = 静默 403。
  · 只要引入多个出口，就必须回答两个问题：
      1) 同一域名固定用哪个出口？——**一致性哈希粘性**：`proxy = ring.get(host)`。
         池子扩容只影响 1/N 的域，而不是把所有域的打散重来。
      2) 缓存键怎么区分出口？——每个出口有稳定的 `egress_id`，参与缓存键。
         否则 A 出口拿到的凭证会被发给 B 出口的请求。

健康与淘汰（借鉴成熟代理池的做法，但按本仓库规模简化）：
  · 评分制：成功 +5（上限 100），失败 -10（求解失败比普通失败更贵）。
  · 连续失败到阈值 → 进入冷却，冷却时长按失败次数指数退避（60s → 120s → … 上限 30min）。
  · **不硬删**：代理可能只是临时的网络抖动，删了就得重新加。

刻意不做的事：
  · 不做后台线程定时探活（会引入额外网络流量与线程生命周期问题）；健康度由真实求解结果驱动。
  · 不在日志或 /health 里暴露代理凭据（只给 egress_id 与脱敏后的地址）。
  · 不实现"随机轮换"——那与本项目的语义正相反（换出口 = 凭证作废）。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SCORE_MAX = 100
SCORE_SUCCESS_STEP = 5
SCORE_FAILURE_STEP = 10
DEFAULT_MAX_CONSECUTIVE = 3
DEFAULT_COOLDOWN_BASE = 60.0
COOLDOWN_MAX = 1800.0
VIRTUAL_NODES = 64


def parse_proxy_url(raw: str) -> dict:
    """把 `scheme://user:pass@host:port` 解析成 Playwright 需要的 proxy 字典。

    返回值里只有连接所需字段，调用方不应把 username/password 写进日志。
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("代理地址为空")
    if "://" not in raw:
        raw = "http://" + raw
    u = urlsplit(raw)
    if not u.hostname or not u.port:
        raise ValueError(f"代理地址缺少 host/port: {raw}")
    scheme = (u.scheme or "http").lower()
    proxy = {"server": f"{scheme}://{u.hostname}:{u.port}"}
    if u.username:
        proxy["username"] = u.username
    if u.password:
        proxy["password"] = u.password
    return proxy


def mask_proxy_url(raw: str) -> str:
    """脱敏：只保留 scheme://host:port，去掉凭据。用于日志与 /health。"""
    try:
        proxy = parse_proxy_url(raw)
        return proxy["server"]
    except ValueError:
        return "<invalid>"


@dataclass
class ProxyEntry:
    raw_url: str
    weight: int = 1
    score: int = SCORE_MAX
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    egress_ip: str = ""          # 实测出口 IP（部署时用 curl 核对后填入；空=未实测）
    last_latency: float = 0.0
    success_count: int = 0
    failure_count: int = 0
    _seq: int = field(default=0)

    @property
    def egress_id(self) -> str:
        """出口的稳定标识，参与缓存键。

        优先用实测 IP（同一 IP 的不同入口可以共享缓存）；未实测时退回代理地址的哈希。
        **它是稳定值**：只要出口不变，缓存就一直有效；换出口自动换键。
        """
        if self.egress_ip:
            return f"ip:{self.egress_ip}"
        digest = hashlib.sha256(self.raw_url.encode("utf-8")).hexdigest()[:12]
        return f"px:{digest}"

    def in_cooldown(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.cooldown_until

    def snapshot(self) -> dict:
        """对外可见的视图：**不含凭据**。"""
        return {
            "proxy": mask_proxy_url(self.raw_url),
            "egress_id": self.egress_id,
            "egress_ip_known": bool(self.egress_ip),
            "score": self.score,
            "weight": self.weight,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_left_sec": max(0, round(self.cooldown_until - time.time(), 1)),
            "success": self.success_count,
            "failure": self.failure_count,
        }


class ProxyPool:
    """一致性哈希 + 评分 + 冷却的代理池。线程安全。"""

    def __init__(
        self,
        proxies: list[str],
        *,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE,
        cooldown_base: float = DEFAULT_COOLDOWN_BASE,
        virtual_nodes: int = VIRTUAL_NODES,
    ) -> None:
        if not proxies:
            raise ValueError("代理池不能为空；不用代理时请把 proxy_pool 设为 None")
        self._lock = threading.Lock()
        self._entries: list[ProxyEntry] = []
        for i, raw in enumerate(proxies):
            parse_proxy_url(raw)          # 尽早暴露配置错误（启动即失败，而不是跑起来才炸）
            self._entries.append(ProxyEntry(raw_url=raw.strip(), _seq=i))
        self._max_consecutive = max_consecutive_failures
        self._cooldown_base = cooldown_base
        self._virtual_nodes = virtual_nodes
        self._ring: list[tuple[int, int]] = []   # (hash, entry_index)
        self._rebuild_ring()

    # -- 环 -----------------------------------------
    def _rebuild_ring(self) -> None:
        ring: list[tuple[int, int]] = []
        for idx, entry in enumerate(self._entries):
            replicas = max(1, self._virtual_nodes * max(1, entry.weight))
            for r in range(replicas):
                token = f"{entry.egress_id}#{r}".encode("utf-8")
                ring.append((int.from_bytes(hashlib.sha256(token).digest()[:8], "big"), idx))
        ring.sort()
        self._ring = ring

    def _ring_position(self, key: str) -> int:
        """返回环上第一个 >= hash(key) 的位置下标（没有则 0）。"""
        h = int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
        if h > self._ring[-1][0]:
            return 0
        lo, hi = 0, len(self._ring) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._ring[mid][0] < h:
                lo = mid + 1
            else:
                hi = mid
        return lo

    # -- 选择 ---------------------------------------
    def for_host(self, host: str) -> ProxyEntry | None:
        """按域取出口：粘性优先；粘住的那个在冷却中则**按环序**顺延到下一个健康节点。

        返回 None 表示当前没有任何可用出口（调用方应快速失败，不要干等）。
        """
        now = time.time()
        with self._lock:
            if not self._ring:
                return None
            start = self._ring_position(host)
            n = len(self._ring)
            seen: set[int] = set()
            for step in range(n):
                entry_idx = self._ring[(start + step) % n][1]
                if entry_idx in seen:
                    continue
                seen.add(entry_idx)
                entry = self._entries[entry_idx]
                if not entry.in_cooldown(now):
                    return entry
            return None

    # -- 结果回传 -----------------------------------
    def record_success(self, entry: ProxyEntry, latency: float = 0.0) -> None:
        now = time.time()
        with self._lock:
            entry.score = min(SCORE_MAX, entry.score + SCORE_SUCCESS_STEP)
            entry.consecutive_failures = 0
            entry.cooldown_until = 0.0
            entry.success_count += 1
            if latency > 0:
                entry.last_latency = latency
        self._maybe_rebuild(entry)

    def record_failure(self, entry: ProxyEntry, reason: str = "other") -> None:
        now = time.time()
        with self._lock:
            entry.score = max(0, entry.score - SCORE_FAILURE_STEP)
            entry.consecutive_failures += 1
            entry.failure_count += 1
            if entry.consecutive_failures >= self._max_consecutive:
                # 指数退避冷却，但不删除（可能只是抖动）
                factor = 2 ** (entry.consecutive_failures - self._max_consecutive)
                entry.cooldown_until = now + min(self._cooldown_base * factor, COOLDOWN_MAX)
        self._maybe_rebuild(entry)

    def set_egress_ip(self, entry: ProxyEntry, ip: str) -> None:
        """登记实测出口 IP。会改变 egress_id，因此缓存键随之变化（语义正确）。"""
        with self._lock:
            entry.egress_ip = ip.strip()
        self._rebuild_ring()

    def set_egress_ip_by_server(self, server: str, ip: str) -> bool:
        """按 `scheme://host:port` 登记出口 IP；找不到该出口返回 False。"""
        target = mask_proxy_url(server)
        if target == "<invalid>":
            target = (server or "").strip().rstrip("/")
        for entry in list(self._entries):
            if mask_proxy_url(entry.raw_url) == target:
                self.set_egress_ip(entry, ip)
                return True
        return False

    def _maybe_rebuild(self, entry: ProxyEntry) -> None:
        if entry.egress_ip:
            self._rebuild_ring()

    # -- 视图 ---------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            entries = [e.snapshot() for e in self._entries]
            healthy = sum(1 for e in self._entries if not e.in_cooldown())
        return {"size": len(self._entries), "healthy": healthy, "entries": entries}
