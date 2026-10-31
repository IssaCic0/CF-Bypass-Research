"""保活池：在凭证过期前主动续期，避免"冷路径"排队吃满求解预算。

为什么需要它（而不是"请求来了再解"）：
  · 冷求解要 14–18s；一旦多个域同时到期，请求就会排在浏览器队列后面，
    客户端等不及 → 超时 → 重试 → 队列更长（thundering herd）。
  · 提前续期把"贵操作"挪到空闲时段，稳态请求就只剩回放。

设计要点（每条都对应一种会踩的坑）：
  · **抖动**：每个域在到期前的续期时刻加随机抖动，避免所有域同一秒一起续期。
  · **失败退避**：某个域连续失败就拉长它的下次尝试间隔，绝不原速重试
    （对已被提分的域连续求解 = 继续加深印象，只会更糟）。
  · **走同一条求解路径**：续期调用 `Solver.solve()`，因此自动受 inflight/队列/SSRF
    守卫/代理粘性约束，不会绕过任何治理。
  · **只续"快要过期"的**：剩余寿命还长的直接跳过，不做无意义求解。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field


@dataclass
class HostState:
    url: str
    failures: int = 0
    next_attempt_at: float = 0.0
    last_renewed_at: float = 0.0
    last_error: str = ""


@dataclass
class PrewarmStats:
    runs: int = 0
    renewed: int = 0
    skipped: int = 0
    failures: int = 0
    last_run_at: float = 0.0
    last_error: str = ""
    per_host: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "enabled": True,
            "targets": len(self.per_host),
            "runs": self.runs,
            "renewed": self.renewed,
            "skipped": self.skipped,
            "failures": self.failures,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "hosts": self.per_host,
        }


class Prewarmer(threading.Thread):
    """后台线程：周期性地把"快要过期"的域重新解一遍。"""

    def __init__(
        self,
        solver,
        targets: list[str],
        *,
        renew_before: float = 300.0,
        interval: float = 60.0,
        jitter: float = 30.0,
        max_failures: int = 3,
        backoff_base: float = 300.0,
        solve_timeout: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        super().__init__(name="cfsolve-prewarm", daemon=True)
        self._solver = solver
        self._renew_before = max(30.0, renew_before)
        self._interval = max(5.0, interval)
        self._jitter = max(0.0, jitter)
        self._max_failures = max(1, max_failures)
        self._backoff_base = max(30.0, backoff_base)
        self._solve_timeout = solve_timeout
        self._rng = rng or random.Random()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._hosts: list[HostState] = [HostState(url=_normalize(u)) for u in targets if _normalize(u)]
        self.stats = PrewarmStats(per_host={h.url: {"failures": 0, "last_error": ""} for h in self._hosts})

    # -- 生命周期 ----------------------------------
    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        # 启动后先随机等一下，避免多进程同时开始续期（进程级抖动）
        if self._jitter:
            self._stop.wait(self._rng.uniform(0, self._jitter))
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 —— 保活线程绝不能因单次异常退出
                with self._lock:
                    self.stats.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            self._stop.wait(self._interval)

    # -- 单轮 --------------------------------------
    def run_once(self) -> None:
        now = time.time()
        with self._lock:
            self.stats.runs += 1
            self.stats.last_run_at = now
            hosts = list(self._hosts)

        for host in hosts:
            if self._stop.is_set():
                return
            if host.next_attempt_at > time.time():
                continue
            try:
                remaining = self._solver.cache_remaining(host.url)
            except Exception as e:  # noqa: BLE001
                remaining = -1.0
                host.last_error = f"cache_remaining: {e}"
            if remaining >= 0 and remaining > self._renew_before:
                with self._lock:
                    self.stats.skipped += 1
                continue

            # 到期前的抖动：把续期时刻摊开，避免同一秒一起打
            if self._jitter and remaining >= 0:
                time.sleep(self._rng.uniform(0, min(self._jitter, max(0.0, self._renew_before / 4))))
                if self._stop.is_set():
                    return

            try:
                self._solver.solve(host.url, timeout=self._solve_timeout)
            except Exception as e:  # noqa: BLE001
                self._record_failure(host, e)
                continue
            self._record_success(host)

    def _record_success(self, host: HostState) -> None:
        with self._lock:
            host.failures = 0
            host.last_error = ""
            host.next_attempt_at = 0.0
            host.last_renewed_at = time.time()
            self.stats.renewed += 1
            self.stats.per_host[host.url] = {"failures": 0, "last_error": "", "last_renewed_at": host.last_renewed_at}

    def _record_failure(self, host: HostState, err: Exception) -> None:
        with self._lock:
            host.failures += 1
            host.last_error = f"{type(err).__name__}: {str(err)[:120]}"
            # 指数退避：只影响这个域，不拖累其它域
            backoff = self._backoff_base * (2 ** min(host.failures - 1, 4))
            host.next_attempt_at = time.time() + backoff
            self.stats.failures += 1
            self.stats.last_error = f"{host.url}: {host.last_error}"
            self.stats.per_host[host.url] = {
                "failures": host.failures,
                "last_error": host.last_error,
                "next_attempt_in_sec": round(backoff, 1),
            }


def parse_targets(inline: str = "", file_path: str = "") -> list[str]:
    """解析保活目标：`--prewarm a.com,b.com` 与 `--prewarm-file hosts.txt`。

    文件格式：一行一个，允许 `#` 注释与空行；可以只写域名（自动补 https://）。
    """
    raw: list[str] = []
    if inline:
        raw.extend(inline.replace(";", ",").split(","))
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    raw.append(line)
        except OSError as e:
            raise ValueError(f"读取 --prewarm-file 失败：{e}") from e

    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        url = _normalize(item)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _normalize(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value.rstrip("/") + "/"
