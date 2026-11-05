"""Prometheus 文本指标渲染（v5.0 T-56 自 service.py 下沉）。

为什么拆：`service.py` 2300+ 行远超 800 行规范，`metrics_text` 是其中
最独立的一块 —— 纯格式化、只读 Solver 的观测字段、不碰浏览器/缓存/HTTP。
拆出后 service.py 少 ~85 行，且 metrics 格式可以独立单测。

兼容性：`Solver.metrics_text` 保留为**原样再导出**（调本模块），
`/metrics` 端点与所有既有调用方零感知。
"""

from __future__ import annotations

import os
import sys

import time
from datetime import datetime

#: `/metrics` 里按域指标只保留前 N 个域（域名是不可控输入，防止标签基数爆炸）。
HOST_METRICS_TOP_N = 20


def _now_fn() -> float:
    return time.time()


def _age_seconds(stamp, now: float) -> float | None:
    """把 ISO-8601 时间戳换成"距今多少秒"；无法解析返回 None（→ NaN，不填假值）。"""
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    try:
        return max(0.0, now - dt.timestamp())
    except (OverflowError, OSError, ValueError):
        return None

#: 延迟直方图分桶（秒），用于 /metrics（原 service.py 同值）
LATENCY_BUCKETS = (1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)


def process_metrics(*, platform_name: str | None = None) -> dict:
    """采集本进程的 RSS / 峰值 / 线程数（跨平台，失败就少给字段而不是抛）。

    为什么不用 psutil：本项目刻意把依赖压到最少（psutil 是 C 扩展，装不上就没有指标）。
    Windows 用 GetProcessMemoryInfo，POSIX 用 resource.getrusage + /proc 可选。
    拿不到就是 `{}` —— /metrics 少几条序列，好过整个端点 500。

    `platform_name` 只为**测试可注入**：CI 抓到一个真缺陷 —— 测试里用
    `monkeypatch.setattr(os, "name", "nt")` 模拟 Windows，那改的是**全局 os 模块**，
    而 `pathlib` 实例化时会读 `os.name` → Linux 上 `Path()` 变成 `WindowsPath` →
    `NotImplementedError` 把整个 pytest 打崩。所以平台判定必须可注入，测试不许动全局。
    """
    out: dict[str, int] = {}
    try:
        import threading as _threading
        out["threads"] = _threading.active_count()
    except Exception:  # noqa: BLE001
        pass
    try:
        if (platform_name or os.name) == "nt":
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            # 伪句柄 -1 = 当前进程；不 CloseHandle
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.c_void_p(-1), ctypes.byref(counters), counters.cb
            ):
                out["rss_bytes"] = int(counters.WorkingSetSize)
                out["peak_rss_bytes"] = int(counters.PeakWorkingSetSize)
        else:
            import resource
            # Linux 上 ru_maxrss 单位是 KB，macOS 是字节 —— 用 sys.platform 判，别猜
            maxrss = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
            out["peak_rss_bytes"] = maxrss if sys.platform == "darwin" else maxrss * 1024
            try:
                with open("/proc/self/statm", encoding="ascii") as fh:
                    pages = int(fh.read().split()[1])
                out["rss_bytes"] = pages * os.sysconf("SC_PAGE_SIZE")
            except Exception:  # noqa: BLE001 —— 非 Linux（如 macOS）没有 statm
                pass
    except Exception:  # noqa: BLE001 —— 采集失败 = 少指标，不是故障
        pass
    return out


def render_metrics_text(
    *,
    solves: int,
    hits: int,
    failures: int,
    timeouts: int,
    rejected_busy: int,
    restarts: int,
    browser_alive: bool,
    queue_depth: int,
    elapsed_sum: float,
    hist: list[int],
    backend_ok: dict[str, int],
    backend_err: dict[str, int],
    host_stats_fn=None,
    research_fn=None,
    process_fn=None,
) -> str:
    """渲染 Prometheus 文本。只暴露计数与桶，不含任何域名/凭证。

    `host_stats_fn` 是 `Solver._store.host_stats`（可能不存在，None 时跳过按域段）。
    `research_fn` 是 `Solver.research_status`（T-76，可能不存在，None 时跳过研究段）。
    `process_fn` 是 `Solver.process_metrics`（v6.2，返回 rss/peak/fd 等），None 时跳过。
    """
    ok_map = dict(backend_ok)
    err_map = dict(backend_err)
    lines = [
        # 口径修正：`_solves` 只在**真正发起过一次后端调用**时 +1（含失败），
        # 而原始 HELP 写"含失败"、实现却只在成功路径 +1 —— 这类"指标说谎"
        # 比没有指标更坏，因为它会让人相信系统是健康的。
        "# HELP cfsolve_solves_total 真实发起的求解次数（含失败）",
        "# TYPE cfsolve_solves_total counter",
        f"cfsolve_solves_total {solves}",
        "# HELP cfsolve_cache_hits_total 缓存命中次数",
        "# TYPE cfsolve_cache_hits_total counter",
        f"cfsolve_cache_hits_total {hits}",
        "# HELP cfsolve_failures_total 求解失败次数",
        "# TYPE cfsolve_failures_total counter",
        f"cfsolve_failures_total {failures}",
        "# HELP cfsolve_timeouts_total 超时次数",
        "# TYPE cfsolve_timeouts_total counter",
        f"cfsolve_timeouts_total {timeouts}",
        "# HELP cfsolve_rejected_total 准入拒绝次数",
        "# TYPE cfsolve_rejected_total counter",
        f"cfsolve_rejected_total {rejected_busy}",
        "# HELP cfsolve_browser_restarts_total 浏览器重启次数",
        "# TYPE cfsolve_browser_restarts_total counter",
        f"cfsolve_browser_restarts_total {restarts}",
        "# HELP cfsolve_browser_alive 浏览器是否存活",
        "# TYPE cfsolve_browser_alive gauge",
        f"cfsolve_browser_alive {1 if browser_alive else 0}",
        "# HELP cfsolve_queue_depth 队列深度",
        "# TYPE cfsolve_queue_depth gauge",
        f"cfsolve_queue_depth {queue_depth}",
        "# HELP cfsolve_solve_seconds 求解耗时分布",
        "# TYPE cfsolve_solve_seconds histogram",
    ]
    cumulative = 0
    for edge, count in zip(LATENCY_BUCKETS, hist):
        cumulative += count
        lines.append(f'cfsolve_solve_seconds_bucket{{le="{edge:g}"}} {cumulative}')
    lines.append(f'cfsolve_solve_seconds_bucket{{le="+Inf"}} {solves}')
    lines.append(f"cfsolve_solve_seconds_sum {round(elapsed_sum, 3)}")

    # 按后端：定位"哪一棒在退步"。没有它，多后端只是黑盒。
    lines += [
        "# HELP cfsolve_backend_total 按后端的调用结果计数",
        "# TYPE cfsolve_backend_total counter",
    ]
    for name in sorted(set(ok_map) | set(err_map)):
        if ok_map.get(name):
            lines.append(f'cfsolve_backend_total{{backend="{name}",result="ok"}} {ok_map[name]}')
        if err_map.get(name):
            lines.append(f'cfsolve_backend_total{{backend="{name}",result="error"}} {err_map[name]}')

    # 按域：只取 top-N，防止标签基数爆炸（域名是不可控输入，绝不能直接当标签用全量）
    if callable(host_stats_fn):
        try:
            rows = host_stats_fn(HOST_METRICS_TOP_N)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            lines += [
                "# HELP cfsolve_host_solves_total 按域求解次数（仅 top-N，其余丢弃以防基数爆炸）",
                "# TYPE cfsolve_host_solves_total counter",
            ]
            for row in rows:
                host = str(row.get("host") or "")
                if host:
                    lines.append(f'cfsolve_host_solves_total{{host="{host}"}} {int(row.get("solves") or 0)}')
            lines += [
                "# HELP cfsolve_host_failures_total 按域失败次数（仅 top-N）",
                "# TYPE cfsolve_host_failures_total counter",
            ]
            for row in rows:
                host = str(row.get("host") or "")
                if host:
                    lines.append(f'cfsolve_host_failures_total{{host="{host}"}} {int(row.get("failures") or 0)}')
    lines.append(f"cfsolve_solve_seconds_count {solves}")

    # T-76：研究资产三条序列。让"没有样本/断言全在 skip"这件事**不可能被忘记**。
    # 关键语义：无快照时 snapshot_age 是 **NaN**，不是 0 —— 0 会假装"刚拍过快照"。
    if callable(research_fn):
        try:
            research = research_fn() or {}
            variants = int(research.get("corpus_variants") or 0)
            # 真实三态计数，各自独立取数、互不推算。
            # 曾经写成 "pass = total − skip，fail 恒 0"，把真实的 fail 记进 pass —— 指标说谎。
            counts = {
                "pass": max(0, int(research.get("assertions_pass") or 0)),
                "fail": max(0, int(research.get("assertions_fail") or 0)),
                "skip": max(0, int(research.get("assertions_skipped") or 0)),
            }
            snapshot_at = research.get("last_snapshot_at")
            age = _age_seconds(snapshot_at, _now_fn())
            age_text = "NaN" if age is None else f"{age:.0f}"
        except Exception:  # noqa: BLE001 —— 研究段损坏只降级，绝不 500
            variants = 0
            counts = {"pass": 0, "fail": 0, "skip": 0}
            age_text = "NaN"
        lines += [
            "# HELP cfsolve_research_corpus_variants 研究语料变体数（corpus/INDEX.json）",
            "# TYPE cfsolve_research_corpus_variants gauge",
            f"cfsolve_research_corpus_variants {variants}",
            "# HELP cfsolve_research_assertions 断言矩阵按状态计数",
            "# TYPE cfsolve_research_assertions gauge",
            f'cfsolve_research_assertions{{state="pass"}} {counts["pass"]}',
            f'cfsolve_research_assertions{{state="fail"}} {counts["fail"]}',
            f'cfsolve_research_assertions{{state="skip"}} {counts["skip"]}',
            "# HELP cfsolve_research_snapshot_age_seconds 距上次研究快照的秒数（无快照为 NaN）",
            "# TYPE cfsolve_research_snapshot_age_seconds gauge",
            f"cfsolve_research_snapshot_age_seconds {age_text}",
        ]
    # v6.2：进程自检指标（参考 camofox-browser 的 camofox_memory_usage_bytes）。
    # 为什么值得加：本项目有过两次真实内存爆炸（21GB / 31GB），而在此之前 /metrics
    # 只有业务计数、没有任何『进程本身胖了』的信号 —— 爆炸只能靠事后发现。
    if callable(process_fn):
        try:
            proc = process_fn() or {}
        except Exception:  # noqa: BLE001 —— 采集失败只降级，绝不让 /metrics 500
            proc = {}
        rss = proc.get('rss_bytes')
        if isinstance(rss, int) and rss >= 0:
            lines += [
                '# HELP cfsolve_process_rss_bytes 求解进程的常驻内存（RSS, 字节）',
                '# TYPE cfsolve_process_rss_bytes gauge',
                f'cfsolve_process_rss_bytes {rss}',
            ]
        peak = proc.get('peak_rss_bytes')
        if isinstance(peak, int) and peak >= 0:
            lines += [
                '# HELP cfsolve_process_peak_rss_bytes 进程峰值常驻内存（RSS, 字节）',
                '# TYPE cfsolve_process_peak_rss_bytes gauge',
                f'cfsolve_process_peak_rss_bytes {peak}',
            ]
        threads = proc.get('threads')
        if isinstance(threads, int) and threads >= 0:
            lines += [
                '# HELP cfsolve_process_threads 进程线程数（卡死时线程只增不减是信号）',
                '# TYPE cfsolve_process_threads gauge',
                f'cfsolve_process_threads {threads}',
            ]
    return "\n".join(lines) + "\n"
