"""`solver/request.py` 的纯函数单测（v5.0.1 D1 的直接收益）。

为什么这些用例值得单独存在：D1 之前，这些参数边界全都埋在 `do_POST` 的 175 行里，
想验一条"want=turnstile 但没给 sitekey 要报 400"，必须**起一个真实 HTTP 服务**再发请求。
下沉成纯函数之后，它们变成毫秒级的直接调用 —— 边界再多也不心疼。

约定：这里断言的是**对外契约**（error_code / stage / hint 关键字），不是实现细节。
文案里的 `hint` 只断言关键子串，避免改措辞就红一片。
"""

from __future__ import annotations

import json

import pytest

from solver.errors import ErrorCode, SolveError
from solver.request import (
    BODY_TOO_LARGE_MSG,
    DEFAULT_TIMEOUT,
    MAX_BODY_BYTES,
    MAX_TIMEOUT,
    MAX_URL_LEN,
    MIN_TIMEOUT,
    clamp_timeout,
    parse_json_body,
    parse_solve_request,
)


def body(**kw) -> bytes:
    return json.dumps(kw).encode()


def err(raw: bytes, body_error: str = "") -> SolveError:
    with pytest.raises(SolveError) as ei:
        parse_solve_request(raw, body_error)
    return ei.value


# ─────────────────────────────────────────── 正常路径


def test_minimal_request_defaults():
    req = parse_solve_request(body(url="https://example.com/"))
    assert req.url == "https://example.com/"
    assert req.timeout == DEFAULT_TIMEOUT
    assert req.force is False
    assert req.want_value == "clearance"
    assert req.sitekey == "" and req.action == "" and req.cdata == ""
    assert req.route_mode == "fixed"


def test_full_request_roundtrip():
    req = parse_solve_request(body(
        url="https://example.com/x", timeout=30, force=True, want="turnstile",
        sitekey="0x4AAA", action="login", cdata="abc", route="auto",
    ))
    assert req.timeout == 30
    assert req.force is True
    assert req.want_value == "turnstile"
    assert req.sitekey == "0x4AAA"
    assert req.action == "login"
    assert req.cdata == "abc"
    assert req.route_mode == "auto"


def test_request_is_immutable():
    """冻结的意义：解析完成后它是"这次请求意图"的唯一表述，下游只读。"""
    req = parse_solve_request(body(url="https://example.com/"))
    with pytest.raises(Exception):
        req.url = "https://evil/"  # type: ignore[misc]


@pytest.mark.parametrize("raw_route,expected", [
    ("auto", "auto"), ("AUTO", "auto"), ("  Auto  ", "auto"),
    ("pure", "pure"), ("browser", "browser"), ("direct", "direct"),
    ("fixed", "fixed"), ("", "fixed"), ("   ", "fixed"),
])
def test_route_mode_is_normalized(raw_route, expected):
    assert parse_solve_request(body(url="https://e.com/", route=raw_route)).route_mode == expected


# ─────────────────────────────────────────── url 边界


@pytest.mark.parametrize("payload", [
    {},
    {"url": ""},
    {"url": None},
    {"url": 123},
    {"url": ["https://e.com/"]},
])
def test_missing_or_invalid_url(payload):
    e = err(json.dumps(payload).encode())
    assert e.code is ErrorCode.INVALID_REQUEST
    assert e.stage == "validate"


def test_url_too_long():
    e = err(body(url="https://e.com/" + "a" * MAX_URL_LEN))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert str(MAX_URL_LEN) in e.message


@pytest.mark.parametrize("url", ["ftp://e.com/", "e.com", "//e.com/", "javascript:alert(1)"])
def test_scheme_must_be_http(url):
    e = err(body(url=url))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert "http(s)" in e.message


# ─────────────────────────────────────────── 其他字段边界


def test_force_must_be_boolish():
    e = err(body(url="https://e.com/", force="true"))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert "force" in e.message


def test_want_must_be_string():
    e = err(body(url="https://e.com/", want=["turnstile"]))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert "want" in e.message


def test_unknown_want_is_rejected():
    e = err(body(url="https://e.com/", want="nope"))
    assert e.code is ErrorCode.INVALID_REQUEST


@pytest.mark.parametrize("field", ["sitekey", "action", "cdata"])
def test_token_fields_have_length_cap(field):
    e = err(body(url="https://e.com/", **{field: "x" * 257}))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert field in e.message


@pytest.mark.parametrize("field", ["sitekey", "action", "cdata"])
def test_token_fields_must_be_string(field):
    e = err(body(url="https://e.com/", **{field: 42}))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert field in e.message


def test_token_want_requires_sitekey():
    """want=turnstile/recaptcha_v3/awswaf 没有 sitekey 是"注定解不出来"，必须入口就拒。"""
    e = err(body(url="https://e.com/", want="turnstile"))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert "sitekey" in e.message
    assert "data-sitekey" in (e.hint or "")


def test_unknown_route_is_rejected_with_options():
    e = err(body(url="https://e.com/", route="turbo"))
    assert e.code is ErrorCode.INVALID_REQUEST
    assert "fixed" in (e.hint or "")


# ─────────────────────────────────────────── timeout 夹取


@pytest.mark.parametrize("value", [1e9, 10**9, 0, -5, 30, "60"])
def test_timeout_passes_through_raw(value):
    """解析期**不夹取** timeout，原样带出。

    为什么这样设计（而不是在解析时就 clamp）：夹取一旦提前，`{"url":"http://127.0.0.1/x",
    "timeout":"x"}` 这类**组合非法**输入就会从「SSRF 拒绝」变成「timeout 必须是数字」，
    对外错误文案跟着变。夹取必须留在 SSRF 守卫**之后**，与拆分前逐字一致。
    """
    payload = {"url": "https://e.com/", "timeout": value}
    assert parse_solve_request(json.dumps(payload).encode()).timeout == value


def test_timeout_default_when_absent_or_null():
    assert parse_solve_request(body(url="https://e.com/")).timeout == DEFAULT_TIMEOUT
    assert parse_solve_request(body(url="https://e.com/", timeout=None)).timeout is None


@pytest.mark.parametrize("bad", ["abc", [1], {"a": 1}])
def test_clamp_timeout_rejects_non_numeric(bad):
    """非数字由 `clamp_timeout` 负责拒绝（调用方在 SSRF 之后调它）。"""
    with pytest.raises(SolveError) as ei:
        clamp_timeout(bad)
    assert ei.value.code is ErrorCode.INVALID_REQUEST
    assert "timeout" in ei.value.message


@pytest.mark.parametrize("value,expected", [
    (1e9, MAX_TIMEOUT),
    (10**9, MAX_TIMEOUT),
    (0, MIN_TIMEOUT),
    (-5, MIN_TIMEOUT),
    (30, 30),
    ("60", 60),
])
def test_clamp_timeout_bounds(value, expected):
    assert clamp_timeout(value) == expected


def test_clamp_timeout_rejects_nan_and_inf():
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(SolveError) as ei:
            clamp_timeout(bad)
        assert ei.value.code is ErrorCode.INVALID_REQUEST


# ─────────────────────────────────────────── body 层


@pytest.mark.parametrize("raw", [b"not json", b"[1,2,3]", b'"str"', b"null", b""])
def test_body_must_be_json_object(raw):
    e = err(raw)
    assert e.code is ErrorCode.INVALID_REQUEST


def test_payload_too_large_maps_to_413_not_400():
    """T-51 契约：超大 body 是"可减小后重试"，不是"请求坏了"。"""
    e = err(b"", f"{BODY_TOO_LARGE_MSG}:{MAX_BODY_BYTES}")
    assert e.code is ErrorCode.PAYLOAD_TOO_LARGE
    assert e.http_status == 413
    assert "--max-body-bytes" in (e.hint or "")


def test_other_body_errors_map_to_400():
    for msg in ("Content-Length 非法", "请求体为空", "不支持 Transfer-Encoding: chunked 请求体，请改用 Content-Length"):
        e = err(b"", msg)
        assert e.code is ErrorCode.INVALID_REQUEST
        assert e.http_status == 400


def test_parse_json_body_accepts_object():
    assert parse_json_body(b'{"a":1}') == {"a": 1}
