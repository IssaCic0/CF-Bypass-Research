"""hypothesis 性质测试（T-81）。

两条性质（对应 plans/下一步改进指南.md T-81 的验收标准）：

1. `/solve` 请求解析面：任意 bytes body / 任意 body_error 组合喂给
   `parse_solve_request`，**永不抛 SolveError 之外的异常** —— 只可能
   成功返回 `SolveRequest` 或抛结构化的 `SolveError`。
   （真实缺陷的形态是：`KeyError` / `UnicodeDecodeError` / `RecursionError`
   之类裸异常冒到 HTTP handler 外面 → 500 而不是 400。）

2. 审计字段白名单面：任意 dict 经过 `EventLog.record`（eventlog.ALLOWED_FIELDS
   白名单过滤）后，`snapshot()` 里的输出字段**必然 ⊆ 白名单**，且值经过
   `_scrub` 收敛（长度 ≤ MAX_VALUE_LEN、可 JSON 序列化）。
   （真实缺陷的形态是：凭证字段漏进 `/logs` 对外可读缓冲。）

Profile：`cfsolve`（max_examples=50），由 `tests/conftest.py` 注册。
默认直接跑 = 50 examples；CI 想加压可 `--hypothesis-profile=ci`（200）。
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from solver.eventlog import ALLOWED_FIELDS, MAX_VALUE_LEN
from solver.errors import SolveError
from solver.request import parse_solve_request

# ---------------------------------------------------------------------------
# 性质 1：/solve 请求解析面 —— 永不崩溃，只返回结构化错误
# ---------------------------------------------------------------------------

# 合法 JSON 值（含嵌套）——覆盖"字段类型全错"的情形
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=300),
    lambda children: st.lists(children, max_size=5)
    | st.dictionaries(st.text(max_size=30), children, max_size=5),
    max_leaves=8,
)

# 任意 bytes：合法 JSON / 截断 JSON / 二进制垃圾 / 空串 全覆盖
arbitrary_bodies = st.one_of(
    st.binary(max_size=512),
    json_values.map(lambda v: json.dumps(v).encode("utf-8", "surrogatepass")),
    json_values.map(lambda v: json.dumps(v).encode("utf-8", "ignore")[:40]),
)

# body_error：模拟 _read_body_raw 可能给出的任意错误文案
arbitrary_body_errors = st.one_of(
    st.just(""),
    st.text(max_size=120),
    st.just("请求体超过 65536 字节"),  # 精确命中 BODY_TOO_LARGE_MSG 前缀
    st.just("Content-Length 头非法"),
)


@settings(
    deadline=None,
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(raw=arbitrary_bodies, body_error=arbitrary_body_errors)
def test_parse_solve_request_never_crashes_only_structured_errors(raw, body_error):
    """性质 1：任意 (raw, body_error) 下，parse_solve_request 只会
    返回 SolveRequest 或抛 SolveError —— 任何其他异常都是缺陷。"""
    try:
        req = parse_solve_request(raw, body_error)
    except SolveError:
        return  # 结构化错误 = 符合契约
    # 成功路径：契约要求必须拿到了合法 url
    assert isinstance(req.url, str) and req.url.startswith(("http://", "https://"))
    assert len(req.url) <= 2048


# ---------------------------------------------------------------------------
# 性质 2：审计字段白名单面 —— 输出字段 ⊆ 白名单
# ---------------------------------------------------------------------------

# 白名单内外的任意字段名 + 任意值（含超长串、对象、None、控制字符）
field_names = st.one_of(
    st.sampled_from(sorted(ALLOWED_FIELDS)),
    st.text(min_size=1, max_size=30).filter(lambda s: s not in ALLOWED_FIELDS),
)
field_values = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**63), max_value=2**63),
    st.floats(allow_nan=True),
    st.text(max_size=500),
    st.binary(max_size=100),
    st.lists(st.integers(), max_size=5),
)


@settings(deadline=None, max_examples=50)
@given(fields=st.dictionaries(field_names, field_values, max_size=12))
def test_eventlog_output_fields_always_within_whitelist(fields):
    """性质 2：任意 dict 进 EventLog.record，snapshot 输出的每条事件
    字段 ⊆ ALLOWED_FIELDS，且值已收敛（str 形式 ≤ MAX_VALUE_LEN）。"""
    from solver.eventlog import EventLog

    log = EventLog(maxlen=4)
    log.record(**fields)
    snap = log.snapshot(lines=10)
    assert snap["total"] == 1
    for line in snap["lines"]:
        unknown = set(line) - ALLOWED_FIELDS
        assert not unknown, f"白名单外字段泄漏: {unknown}（输入 keys: {sorted(fields)}）"
        for key, value in line.items():
            # _scrub 后：非基本类型必须被 str() 化，且长度受控
            assert value is None or isinstance(value, (bool, int, float, str))
            if isinstance(value, str):
                assert len(value) <= MAX_VALUE_LEN


def test_hypothesis_profile_registered():
    """冒烟：确认 profile 注册可用（防 conftest 注册写错被静默忽略）。"""
    from hypothesis import settings as hsettings

    profile = hsettings.get_profile("cfsolve")
    assert profile.max_examples == 50
