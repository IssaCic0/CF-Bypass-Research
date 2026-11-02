"""Prometheus 文本指标渲染（v5.0 T-56 自 service.py 下沉）。

为什么拆：`service.py` 2300+ 行远超 800 行规范，`metrics_text` 是其中
最独立的一块 —— 纯格式化、只读 Solver 的观测字段、不碰浏览器/缓存/HTTP。
拆出后 service.py 少 ~85 行，且 metrics 格式可以独立单测。

兼容性：`Solver.metrics_text` 保留为**原样再导出**（调本模块），
`/metrics` 端点与所有既有调用方零感知。
"""

from __future__ import annotations

#: `/metrics` 里按域指标只保留前 N 个域（域名是不可控输入，防止标签基数爆炸）。
HOST_METRICS_TOP_N = 20

#: 延迟直方图分桶（秒），用于 /metrics（原 service.py 同值）
LATENCY_BUCKETS = (1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 60.0)


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
) -> str:
    """渲染 Prometheus 文本。只暴露计数与桶，不含任何域名/凭证。

    `host_stats_fn` 是 `Solver._store.host_stats`（可能不存在，None 时跳过按域段）。
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
    return "\n".join(lines) + "\n"
