"""求解服务的错误码与异常。

为什么需要单独的模块：
  · 原来的 `_do_solve` 一路 `except` 吞掉一切，上层只能拿到 `{"error": "TypeError: ..."}`，
    调用方无法区分"超时 / 浏览器崩了 / 被盾拒了 / 自己参数写错"——也就无法决定要不要重试。
  · 这里把错误码收敛成一个枚举（参考成熟 captcha sidecar 的做法：跨后端归一化错误码），
    并强制带 `stage`（失败发生在哪一步）与 `hint`（可执行的下一步）。

设计约束：
  · 纯 stdlib、零依赖（本仓库的既有风格）。
  · 错误码是**对外契约**：改名等于破坏兼容，新增只能追加。
  · **不要在契约里写"永不产生"的码**：`RATE_LIMITED` 曾注释成"同一 host 求解过频"，
    但按 host 限流这个功能根本没实现 —— 调用方会照着文档去写 429 重试逻辑，那是死代码。
    现标记为预留值；真要做按 host 限流时再实现，并同步更新 `docs/API.md`。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """对外稳定的错误码（字符串值与 JSON 输出一致）。"""

    # —— 请求级（4xx）——
    INVALID_REQUEST = "invalid-request"     # 参数非法：URL 不合规、timeout 越界、body 畸形
    PAYLOAD_TOO_LARGE = "payload-too-large"  # 请求体超限：可减小后重试（区别于 400 的"不该重试"）
    UNAUTHORIZED = "unauthorized"           # 缺少或错误的 Bearer token
    RATE_LIMITED = "rate-limited"           # **预留值**：当前没有按 host 限流，不会产生（见下）
    BUSY = "busy"                           # 准入拒绝：队列已满 / inflight 已达上限

    # —— 环境级（5xx）——
    NOT_CONFIGURED = "not-configured"       # 后端未就绪：camoufox 未安装 / 二进制未 fetch
    BROWSER_UNAVAILABLE = "browser-unavailable"  # 浏览器进程不可用（启动失败或中途断开）
    TIMEOUT = "timeout"                     # 超过本次求解预算
    TRANSPORT = "transport"                 # 网络层失败（DNS / 连接 / TLS）
    INTERNAL = "internal"                   # 未预期异常

    # —— 求解结果（HTTP 200，solved:false）——
    UNSOLVED = "unsolved"                   # 浏览器跑完了，但没拿到 cf_clearance


#: 错误码 → HTTP 状态码。
#:
#: 契约（与 `docs/API.md` 一致，且是 gocfsolve 依赖的语义）：
#:   · 2xx  → 读 `ok` / `solved` 字段（求解失败 ≠ 请求失败，用 200 + ok:false）
#:   · 非 2xx → 读 `error_code` / `error` / `stage` / `hint`
#: 两者永不同时出现，调用方不需要猜。
STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.BUSY: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.NOT_CONFIGURED: 503,
    ErrorCode.BROWSER_UNAVAILABLE: 503,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.TRANSPORT: 502,
    ErrorCode.INTERNAL: 500,
    ErrorCode.UNSOLVED: 200,
}


class SolveError(Exception):
    """带错误码、阶段与可执行提示的异常。

    `stage` 取值：`validate`（入参校验）/ `admit`（准入）/ `navigate`（导航）
    / `wait`（等 cookie）/ `solve`（求解整体）/ `internal`。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        stage: str = "solve",
        hint: str = "",
        http_status: int | None = None,
        extra: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.stage = stage
        self.hint = hint
        self.http_status = http_status if http_status is not None else STATUS_BY_CODE.get(code, 500)
        self.extra: dict = extra or {}

    def payload(self) -> dict:
        out = {
            "ok": False,
            "solved": False,
            "error_code": self.code.value,
            "error": self.message,
            "stage": self.stage,
        }
        if self.hint:
            out["hint"] = self.hint
        if self.extra:
            out.update(self.extra)      # 例如多后端路由的 attempts 轨迹
        return out

    def __str__(self) -> str:  # 日志里只打一行
        return f"[{self.code.value}/{self.stage}] {self.message}"
