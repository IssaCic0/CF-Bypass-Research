"""HTTP 求解请求的解析与校验（v5.0.1 D1：自 `service.py` 的 `do_POST` 下沉，只搬不改）。

为什么拆出来：
  · `do_POST` 原本把"读 body → 解析 → 逐字段校验 → SSRF → 路由 → 调后端 → 记日志 → 回包"
    全塞在 175 行里，解析与校验部分**夹在 `try` 与 `except` 之间**，改一个字段要先读完整段；
  · 解析与校验本身是**纯函数**（不碰 socket、不碰浏览器、不碰缓存），完全可以在没有 HTTP
    的情况下逐条单测 —— 拆出来之后，参数边界（非法 route / 缺 sitekey / 超长 action …）
    的用例不再需要起一个真实服务。
  · 不搬的两件事：**SSRF 守卫**（依赖 `Solver._check_target`，是守卫不是解析）与
    **route 探测**（要出站请求）。两者仍留在 `do_POST` 里，顺序与原来完全一致：
    解析 → SSRF → 路由。

行为承诺：错误码 / `stage` / `hint` 文案与拆分前**逐字一致**（拆分不改变对外契约）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

try:  # 作为包导入（tests / solver 包）
    from .challenges import TOKEN_WANTS, Want
    from .errors import ErrorCode, SolveError
    from .route import ROUTE_MODES
except ImportError:  # 直接以脚本运行：python solver/service.py
    from challenges import TOKEN_WANTS, Want  # type: ignore[no-redef]
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]
    from route import ROUTE_MODES  # type: ignore[no-redef]

#: URL 长度上限（超过直接 400：更长的东西几乎必然是攻击或客户端 bug）
MAX_URL_LEN = 2048
#: 请求体上限。超过不是"读慢一点"，而是**不读**（见 service.py 的 `_drain_bounded`）。
MAX_BODY_BYTES = 64 * 1024
#: `timeout` 的默认值与允许区间
DEFAULT_TIMEOUT = 45.0
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 120.0  # 上限：避免 timeout=1e9 把 worker 永久占住

#: `_read_body_raw` 用这个哨兵把"超大 body"传给上层转 413（400 与 413 的重试语义不同）
BODY_TOO_LARGE_MSG = "__payload_too_large__"

#: sitekey / action / cdata 的长度上限（都是调用方可控字符串）
MAX_TOKEN_FIELD_LEN = 256


def parse_json_body(raw: bytes) -> dict:
    """把请求体解析成 JSON 对象；不是 JSON 或不是对象都报 `invalid-request`。"""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise SolveError(ErrorCode.INVALID_REQUEST, f"请求体不是合法 JSON：{e}", stage="validate")
    if not isinstance(payload, dict):
        raise SolveError(ErrorCode.INVALID_REQUEST, "请求体必须是 JSON 对象", stage="validate")
    return payload


def clamp_timeout(value) -> float:
    """把外部传入的 timeout 收敛到 [MIN_TIMEOUT, MAX_TIMEOUT]。"""
    try:
        t = float(value)
    except (TypeError, ValueError):
        raise SolveError(ErrorCode.INVALID_REQUEST, "timeout 必须是数字", stage="validate")
    if t != t or t in (float("inf"), float("-inf")):  # NaN / inf
        raise SolveError(ErrorCode.INVALID_REQUEST, "timeout 必须是有限数字", stage="validate")
    return max(MIN_TIMEOUT, min(MAX_TIMEOUT, t))


#: 兼容旧名（`service.py` 内部与既有用例都用下划线版本）
_clamp_timeout = clamp_timeout


@dataclass(frozen=True)
class SolveRequest:
    """一次 `/solve` 请求的规范化形态。

    冻结（不可变）是刻意的：解析完成后它就是"这次请求意图"的唯一表述，
    下游（路由 / 后端 / 审计）只读不改，避免"同一个字段在链路里被人改了一半"。
    """

    url: str
    #: **原始** timeout 值（未夹取）。夹取发生在 `do_POST` 的 SSRF 守卫**之后**，
    #: 与拆分前完全一致 —— 否则 `{"url":"http://127.0.0.1/","timeout":"x"}` 这类
    #: 组合非法输入会从「SSRF 拒绝」变成「timeout 必须是数字」，改变了对外错误文案。
    timeout: object
    force: bool
    want: "Want"
    sitekey: str
    action: str
    cdata: str
    route_mode: str

    @property
    def want_value(self) -> str:
        """`want` 的字符串值（对外契约里用它，不是枚举对象）。"""
        return self.want.value


def parse_solve_request(raw: bytes, body_error: str = "") -> SolveRequest:
    """把 `(原始 body, 读取错误)` 解析成 `SolveRequest`；任何不合法都抛 `SolveError`。

    `body_error` 非空表示 `_read_body_raw` 已经判定这次请求的 body 有问题
    （超大 / chunked / Content-Length 非法…），此时不再尝试解析。

    注意：这里**只做语法/形状校验，不做策略夹取**。`timeout` 原样返回，
    由调用方在 SSRF 守卫之后 `clamp_timeout()` —— 与拆分前的顺序逐字一致。
    """
    if body_error:
        if body_error.startswith(BODY_TOO_LARGE_MSG):
            # v5.0 T-51：超大 body → 413（可重试），区别于 400（不该重试）
            raise SolveError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"请求体超过 {MAX_BODY_BYTES} 字节",
                stage="validate",
                hint=f"减小请求体到 {MAX_BODY_BYTES} 字节以内后重试；如需提高上限，启动时调 --max-body-bytes",
            )
        raise SolveError(ErrorCode.INVALID_REQUEST, body_error, stage="validate")

    req = parse_json_body(raw)

    url = req.get("url")
    if not isinstance(url, str) or not url:
        raise SolveError(ErrorCode.INVALID_REQUEST, "缺少 url 字段", stage="validate")
    if len(url) > MAX_URL_LEN:
        raise SolveError(ErrorCode.INVALID_REQUEST, f"url 超过 {MAX_URL_LEN} 字符", stage="validate")
    if not url.startswith(("http://", "https://")):
        raise SolveError(
            ErrorCode.INVALID_REQUEST, "url 必须是 http(s)", stage="validate",
            hint="需要带 scheme 的绝对地址，例如 https://example.com/",
        )

    force_raw = req.get("force", False)
    if not isinstance(force_raw, (bool, int)):
        raise SolveError(ErrorCode.INVALID_REQUEST, "force 必须是布尔值", stage="validate")

    # 要哪种产物：clearance（默认）/ awswaf / turnstile / recaptcha_v3
    want_raw = req.get("want", "")
    if not isinstance(want_raw, str):
        raise SolveError(ErrorCode.INVALID_REQUEST, "want 必须是字符串", stage="validate")
    try:
        want_enum = Want.parse(want_raw)
    except ValueError as e:
        raise SolveError(ErrorCode.INVALID_REQUEST, str(e), stage="validate") from e

    sitekey = req.get("sitekey", "") or ""
    action = req.get("action", "") or ""
    cdata = req.get("cdata", "") or ""
    for name, value in (("sitekey", sitekey), ("action", action), ("cdata", cdata)):
        if not isinstance(value, str) or len(value) > MAX_TOKEN_FIELD_LEN:
            raise SolveError(
                ErrorCode.INVALID_REQUEST,
                f"{name} 必须是不超过 {MAX_TOKEN_FIELD_LEN} 字符的字符串",
                stage="validate",
            )
    if want_enum in TOKEN_WANTS and not sitekey:
        raise SolveError(
            ErrorCode.INVALID_REQUEST, f"want={want_enum.value} 需要 sitekey", stage="validate",
            hint="从页面源码里取 data-sitekey，或 reCAPTCHA 的 api.js?render=<sitekey>",
        )

    # route：让探测结论驱动决策（v4.0 T-32）。不传等价于 fixed，行为与 v3 完全一致。
    route_mode = req.get("route", "fixed")
    if not isinstance(route_mode, str):
        raise SolveError(ErrorCode.INVALID_REQUEST, "route 必须是字符串", stage="validate")
    route_mode = route_mode.strip().lower() or "fixed"
    if route_mode not in ROUTE_MODES:
        raise SolveError(
            ErrorCode.INVALID_REQUEST, f"未知 route={route_mode!r}", stage="validate",
            hint=f"可选 {sorted(ROUTE_MODES)}；不传等价于 fixed（v3 行为）",
        )

    return SolveRequest(
        url=url,
        timeout=req.get("timeout", DEFAULT_TIMEOUT),
        force=bool(force_raw),
        want=want_enum,
        sitekey=sitekey,
        action=action,
        cdata=cdata,
        route_mode=route_mode,
    )
