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
import ipaddress
import re
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
    def entries_snapshot(self) -> list[dict]:
        """**内部**只读快照：含 raw_url（校验出口 IP 时要用真地址）。

        与 `snapshot()` 的差别：那个是给 `/health` 用的对外视图，必须脱敏；
        这个只给同进程内的校验逻辑用，**绝不许进日志或 HTTP 响应**。
        """
        with self._lock:
            return [{"raw_url": e.raw_url, "egress_ip": e.egress_ip} for e in self._entries]

    def snapshot(self) -> dict:
        with self._lock:
            entries = [e.snapshot() for e in self._entries]
            healthy = sum(1 for e in self._entries if not e.in_cooldown())
        return {"size": len(self._entries), "healthy": healthy, "entries": entries}


# ── 出口 IP 实测校验（v6.3，参考 proxypool 的 tester 思路）──────────
#
# 要解决的缺口：`egress_ip` 原来只能靠**人**填（字段注释原文是"部署时用 curl 核对后填入"）。
# 填错了不会报错 —— 只是悄悄换了缓存键，于是「求解走 A 出口、回放走 B 出口」，
# 而 cf_clearance 是绑 IP 的：**稳定的静默 403**，排查起来极贵。
#
# 参考项目的做法值得学：它不只问"代理通不通"，而是发两次请求比对
#   · 直连拿 origin_ip，经代理拿 anonymous_ip
#   · 断言 origin != anonymous（代理确实生效）
#   · 断言 proxy.host == anonymous（这个出口确实是我以为的那个 IP）
#
# 我们只取后一半（前半"代理是否生效"在部署侧无意义——求解服务本来就只有代理这一条路），
# 并且把它做成**可对比登记值**的校验，而不是无条件覆盖。

#: 默认回显地址。用户 2026-11-05 拍板：IP 回显请求**不计入**真机预算（不是目标站）。
#:
#: 为什么默认用 Cloudflare 的 /cdn-cgi/trace 而不是第三方 IP 站：
#:   · 我们打的目标站大量在 CF 后面，trace 与目标走同一套边缘网络，
#:     测出来的 IP 更接近"目标站看到的你"；
#:   · 返回是纯文本 `key=value` 行，`ip=` 一行即答案，不依赖第三方站的 JSON 结构；
#:   · 不需要额外信任一个第三方服务（原参考项目用 httpbin.org/ip）。
#: 但它是**可配置**的 —— 有些部署环境访问不了 CF，或者更信自己家的回显服务。
DEFAULT_EGRESS_ECHO_URL = "https://www.cloudflare.com/cdn-cgi/trace"

#: 回显响应里认哪些键（不同服务用的名字不同，全收一遍再看）。
_ECHO_IP_KEYS = ("ip", "origin", "query", "origin_ip")

#: 回显值可能被引号包着（`ip="1.2.3.4"`），统一剥掉这几种引号
_QUOTES = "\"'"

_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def parse_echo_ip(text: str) -> str:
    """从回显服务的响应里抠出 IP（纯函数，便于离线测）。

    支持三种常见形态：
      · CF `/cdn-cgi/trace`：多行 `key=value`，取 `ip=` 行
      · httpbin.org/ip：`{"origin": "1.2.3.4"}`（JSON，但这里按文本处理即可）
      · ifconfig.me / ipify：整个响应就是一行 IP

    抠不出合法 IPv4 就返回空串 —— **不猜、不返回原文**：调用方据此判"探测失败"，
    而不是把一个错误页面前 20 字符当成 IP 记进 egress_id。
    """
    if not text:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # key=value 形态
        if "=" in line:
            key, _, value = line.partition("=")
            if key.strip().lower() in _ECHO_IP_KEYS:
                candidate = value.strip().strip(_QUOTES)
                if _is_ipv4(candidate):
                    return candidate
        # JSON 形态：找 "ip": "..." / "origin": "..."
        for key in _ECHO_IP_KEYS:
            m = re.search(r'"' + key + r'"\s*:\s*"([^"]+)"', line)
            if m:
                candidate = m.group(1).strip()
                if _is_ipv4(candidate):
                    return candidate
    # 兜底：整段里第一个合法 IPv4（ifconfig.me / ipify 这类"就一行 IP"的服务）
    for m in _IPV4_RE.finditer(text):
        if _is_ipv4(m.group(0)):
            return m.group(0)
    return ""


def _is_ipv4(text: str) -> bool:
    try:
        ipaddress.IPv4Address(text)
        return True
    except (ipaddress.AddressValueError, ValueError):
        return False


def _default_fetch(url: str, proxy_url: str, timeout: float) -> str:
    """经代理取回 url 的响应正文（标准库实现，零依赖）。

    刻意不用 httpx/requests：这几个包在 requirements 里是给别的用途钉死的版本，
    为了一个自检功能去碰它们不值得。
    """
    import urllib.error
    import urllib.request

    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"User-Agent": "cfsolve-egress-check/1.0"})
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(64 * 1024).decode("utf-8", "replace")


@dataclass
class EgressCheck:
    """一个出口的实测结果。`ok` 的语义见 `verify_egress` 的文档。"""

    proxy: str            # 脱敏后的 scheme://host:port
    registered: str       # 登记的 IP（`--proxy-egress-ip` 填的；空=没登记）
    observed: str         # 实测 IP（探测失败为空）
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {
            "proxy": self.proxy, "registered": self.registered, "observed": self.observed,
            "ok": self.ok, "detail": self.detail,
        }


def verify_egress(
    pool: "ProxyPool",
    *,
    echo_url: str = DEFAULT_EGRESS_ECHO_URL,
    timeout: float = 10.0,
    fetch=None,
) -> list[EgressCheck]:
    """逐个出口实测 IP，并与登记值比对。**只报告，不改池状态**。

    `ok` 的语义（三种"好"与两种"坏"）：
      · 未登记 + 实测到 IP      → ok=True，detail 说明"建议把该 IP 用 --proxy-egress-ip 登记"
      · 已登记 + 实测一致       → ok=True
      · 已登记 + 探测失败       → ok=True（探测不可达不等于配置错，交给人判断）
      · 已登记 + 实测不一致     → **ok=False**（这是最危险的一种：缓存键会错位）
      · 未登记 + 探测失败       → ok=True，detail 说明"未登记且探测不到，将退回地址哈希"

    刻意**不自动写回** `entry.egress_ip`：改 egress_id 会让整域旧缓存失效，
    这种动作必须由人显式做（`--proxy-egress-ip`）。自动写回 = 探测服务抖一下就把缓存全废。

    `fetch(url, proxy_url, timeout) -> str` 可注入，便于完全离线地测本函数的判定逻辑。
    """
    do_fetch = fetch or _default_fetch
    out: list[EgressCheck] = []
    for entry in pool.entries_snapshot():
        masked = mask_proxy_url(entry["raw_url"])
        registered = entry["egress_ip"]
        try:
            text = do_fetch(echo_url, entry["raw_url"], timeout)
            observed = parse_echo_ip(text)
            err = "" if observed else f"回显里没找到合法 IPv4（前 80 字符：{text[:80]!r}）"
        except Exception as e:  # noqa: BLE001 —— 探测失败是预期情形之一，不往上抛
            observed = ""
            err = f"{type(e).__name__}: {str(e)[:120]}"

        if not registered and observed:
            ok, detail = True, f"未登记，实测 {observed}；建议用 --proxy-egress-ip 登记（否则退回地址哈希）"
        elif not registered and not observed:
            ok, detail = True, f"未登记且探测失败：{err}；将退回地址哈希（缓存仍可用，只是不能跨入口共享）"
        elif registered and observed and registered == observed:
            ok, detail = True, f"一致（{observed}）"
        elif registered and observed and registered != observed:
            ok, detail = False, (f"**不一致**：登记 {registered} 但实测 {observed}。"
                                 "缓存键会错位 → 凭证可能发给错出口 → 静默 403")
        else:
            ok, detail = True, f"已登记 {registered}，但探测失败：{err}（无法确认，建议手工复核）"
        out.append(EgressCheck(masked, registered, observed, ok, detail))
    return out
