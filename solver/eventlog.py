"""最近 N 条事件的内存环形缓冲（v5.0.1 P2-1）。

为什么需要：现在排一次障必须 `ssh` 上去翻 JSONL 审计文件或者对着 `/metrics` 猜，
而"刚才那 3 个请求为什么失败"是最常见的运维问题。参考 `captcha-solver` 的
`GET /logs`（最近 100 条，`total` 是全量计数）—— 它是同类自托管求解服务里
最省事的一层可观测性，成本只有一个 `deque`。

安全边界（本模块存在的**主要**理由）：
  · 这是个**对外可读**的端点，所以"写进来什么"必须由**白名单**决定，
    而不是"调用方自觉不要传敏感字段"。白名单之外的一切字段一律丢弃并计数
    （`dropped_fields` 暴露出来，而不是静默消失）。
  · 单值长度截断到 200 字符：防止某个畸形 URL/错误串把缓冲撑成内存放大器。
  · 凭证永不可能出现：`_log()` 的调用点本来就不传 token/cookie；
    这里再加一道结构性防线 —— 万一将来有人手滑传了 `token=`，也会被白名单挡掉。

不做的事：不落盘（审计要落盘请用 `--audit-log` 的 JSONL）、不做聚合（聚合看 `/metrics`）、
不保证跨进程（每个 worker 各有一份，`/status` 会带上进程标识）。
"""

from __future__ import annotations

import json as _json
import threading
import time
from collections import deque

#: 允许进入对外可读缓冲的字段。**新增字段必须先想清楚它是否会泄露凭证/主机细节。**
#: 这份清单是按 `solver/service.py` 里所有 `_log(...)` 调用点实测出来的，不是猜的。
ALLOWED_FIELDS = frozenset(
    {
        "event",      # solve / detect / shutdown / shutdown-denied …
        "req_id",     # 短随机串，用于把同一请求的多行串起来
        "request_id", # T-79：对外契约的贯穿 ID（与 req_id 同值，非敏感）
        "host",       # 目标域名（运维必须知道是哪个站出的问题）
        "ok",         # bool
        "elapsed",    # 秒（浮点）
        "ms",         # 毫秒（整数）
        "backend",    # cfsolve / pydoll / camoufox / flaresolverr
        "error_code", # ErrorCode 的字符串值
        "stage",      # validate / admit / solve / internal
        "route",      # route=auto 的决策结果
        "cached",     # 是否命中缓存
        "peer",       # 对端地址（仅 /shutdown 相关事件）
        "reason",     # 人话原因（如 browser_unavailable）
        "status",     # HTTP 状态码
        "method",     # GET / POST
        "path",       # 请求路径
        "clearance_len",  # 凭证**长度**（审计文件也这么记，绝不记本体）
        "ts",         # 事件时间戳（由 _log 的 setdefault 填入）
    }
)

#: 审计文件（`--audit-log` 的 JSONL）另有一份更窄的白名单 `solver.service.AUDIT_FIELDS`。
#: 两份白名单是**故意分开**的：审计是落盘的取证材料，口径应当更严；本模块是内存里的
#: 排障缓冲。为了不让它们悄悄漂移，`tests/test_observability.py` 断言
#: `AUDIT_FIELDS ⊆ ALLOWED_FIELDS` —— 往审计里加字段的人会立刻被提醒这里也要跟上。

#: 审计 JSONL 尾部读取的默认上限（字节）。见 `read_jsonl_tail` 的说明。
#:
#: 512KB ≈ 几千行审计记录，足够回答"刚才那几十条为什么失败"；
#: 再大就是在用它当日志搜索引擎了 —— 那是 logdy/jq 的活儿，不是本服务该做的事。
AUDIT_TAIL_MAX_BYTES = 512 * 1024

#: 单值最大长度。域名/错误串都可能被调用方控制，不截断就是内存放大器。
MAX_VALUE_LEN = 200


def _scrub(value):
    """把任意值收敛成"JSON 可序列化 + 定长"的形式。"""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else str(value)
    return text[:MAX_VALUE_LEN]


class EventLog:
    """定长、线程安全的事件环形缓冲。

    线程安全的必要性：HTTP handler 是多线程的，`/logs` 的读取与求解线程的写入会并发。
    """

    def __init__(self, maxlen: int = 200) -> None:
        self._buf: deque[dict] = deque(maxlen=max(1, int(maxlen)))
        self._lock = threading.Lock()
        self._total = 0
        self._dropped = 0

    def record(self, **fields) -> None:
        """记录一条事件。白名单外的字段被丢弃并累计到 `dropped_fields`。"""
        clean: dict = {}
        dropped = 0
        for key, value in fields.items():
            if key not in ALLOWED_FIELDS:
                dropped += 1
                continue
            clean[key] = _scrub(value)
        clean.setdefault("ts", round(time.time(), 3))
        with self._lock:
            self._buf.append(clean)
            self._total += 1
            self._dropped += dropped

    def snapshot(self, lines: int = 50) -> dict:
        """最近 `lines` 条（新的在前）。`total` 是**进入缓冲的总条数**，不是当前长度。"""
        with self._lock:
            total = self._total
            dropped = self._dropped
            items = list(self._buf)
        n = max(0, min(int(lines), len(items)))
        recent = list(reversed(items))[:n]
        return {"total": total, "kept": len(items), "dropped_fields": dropped, "lines": recent}

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
            self._total = 0
            self._dropped = 0


def read_jsonl_tail(path: str, lines: int, *, max_bytes: int = AUDIT_TAIL_MAX_BYTES) -> dict:
    """读一个 JSONL 文件的**尾部**若干行（v6.3，参考 logdy 的"把日志变成可浏览的东西"）。

    要解决的问题：`--audit-log` 一直在写 JSONL，但**没有任何查看手段** ——
    要么 ssh 上去 `tail -f | jq`，要么就是不看。而"昨天那次失败的完整链路"恰恰在文件里。

    设计要点（每条都是踩过的坑）：
      · **有界读取**：从文件末尾按块倒着读，最多 `max_bytes`。审计可能有几百 MB，
        一次 read() 就把内存打爆（本项目有过 21GB 内存爆炸史，不重复踩）。
      · **新行在前**：与 `/logs` 的既有契约一致（运维最先要看最新的）。
      · **坏行跳过而不是抛**：审计是 append 写入，进程被杀时最后一行可能是半截 JSON。
        一行坏不能把整个端点弄挂 —— 运维恰恰在事故后最需要看它。
      · **只读**：不轮转、不删、不加锁。轮转仍是外部 logrotate 的事。

    返回：
      `{"lines": [...新的在前...], "total_lines_estimate": int, "truncated": bool, "note": str}`
      `truncated=True` 表示只读了文件尾部的一段（`total_lines_estimate` 只是**这段**的行数，
      不是全文行数 —— 别把它当"总共多少条"用）。
    """
    import os as _os

    NL = b"\n"
    empty = {"lines": [], "total_lines_estimate": 0, "truncated": False, "note": ""}
    if not path:
        return {**empty, "note": "未开启审计（启动时没有 --audit-log）"}
    try:
        size = _os.path.getsize(path)
    except OSError as e:
        return {**empty, "note": f"审计文件不可读：{type(e).__name__}"}
    if size == 0:
        return {**empty, "note": "审计文件为空"}

    # `lines=0` 要 0 行（与 `/logs` 同一套夹取规则），不能因为"至少要读一行"就回 1 行
    want = max(0, int(lines))
    if want == 0:
        return {**empty, "note": "调用方要 0 行（?lines=0）"}
    chunk = 64 * 1024
    read_bytes = 0
    data = b""
    pos = size
    try:
        with open(path, "rb") as fh:
            while pos > 0 and read_bytes < max_bytes:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                data = fh.read(step) + data
                read_bytes += step
                # 行数够了就停（大文件尾部只需最后一小段）
                if data.count(NL) > want:
                    break
    except OSError as e:
        return {**empty, "note": f"读审计文件失败：{type(e).__name__}"}

    raw_lines = data.split(NL)
    # 从中间开始读时，第一段是半截行 —— 丢掉（否则会把半截 JSON 当坏行计数）
    if pos > 0 and raw_lines:
        raw_lines = raw_lines[1:]

    parsed: list[dict] = []
    bad = 0
    for raw in reversed(raw_lines):
        if not raw.strip():
            continue
        try:
            obj = _json.loads(raw.decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 —— 半截行/损坏行跳过，绝不 500
            bad += 1
            continue
        if isinstance(obj, dict):
            parsed.append(obj)
        if len(parsed) >= want:
            break
    note = f"{bad} 行无法解析（进程被杀时会留半截 JSON）" if bad else ""
    return {
        "lines": parsed,
        "total_lines_estimate": data.count(NL),
        "truncated": pos > 0,
        "note": note,
    }
