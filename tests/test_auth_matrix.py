"""鉴权矩阵：每个受保护端点都必须**真的**要求 token（复审 M3 的落地做法）。

为什么要有这个文件：`scripts/validate-openapi.mjs` 只能校验"规格里声明了要鉴权"，
它**看不到源码里到底有没有调 `_authorized()`**。于是"规格说要鉴权、实现忘了鉴权"
这种偏离会在双向比对里照样通过 —— 门禁给了虚假安全感。

复审建议的更强做法就是这条：起一次真实服务，逐个端点发**无 token** 请求断言 401。
成本极低（不联网、不启浏览器），但它是**唯一**能证明"鉴权真的在生效"的检查。
加新端点时忘了鉴权 → 这个文件立刻红。
"""

from __future__ import annotations

import json

import pytest

from test_service import _get, _post, live_server  # noqa: F401 —— fixture 需被收集

#: 受保护端点：(方法, 路径, payload)
PROTECTED = [
    ("GET", "/detect?url=https://example.com/", None),
    ("GET", "/status", None),
    ("GET", "/logs", None),
    ("GET", "/stats", None),
    ("GET", "/metrics", None),
    ("POST", "/detect", {"url": "https://example.com/"}),
    ("POST", "/solve", {"url": "https://example.com/"}),
    ("POST", "/shutdown", {}),
]

#: 刻意公开的端点：必须免鉴权可用（容器探活 + 状态页外壳）
PUBLIC_GET = ["/", "/health"]

#: 兼容层：**鉴权失败也要 HTTP 200**（生态客户端只看 errorId），所以单独一组
COMPAT = [
    ("/createTask", {"task": {}}),
    ("/getTaskResult", {"taskId": "x"}),
]


def _call(base, method, path, payload, token=None):
    if method == "GET":
        status, body = _get(base, path, token=token)
    else:
        status, body = _post(base, path, payload, token=token)
    try:
        return status, json.loads(body) if isinstance(body, str) else body
    except Exception:
        return status, {}


@pytest.mark.parametrize("method,path,payload", PROTECTED)
def test_protected_route_requires_token(live_server, method, path, payload):
    """无 token 必须 401 —— 这条就是"鉴权真的在生效"的证据。"""
    status, body = _call(live_server, method, path, payload)
    assert status == 401, f"{method} {path} 无 token 竟然返回 {status}（应 401）"
    assert body.get("error_code") == "unauthorized", body


@pytest.mark.parametrize("path", PUBLIC_GET)
def test_public_route_is_reachable_without_token(live_server, path):
    status, _ = _get(live_server, path)
    assert status == 200, f"{path} 应当免鉴权可用，实际 {status}"


@pytest.mark.parametrize("path,payload", COMPAT)
def test_compat_route_without_credential_is_error_id_1_not_401(live_server, path, payload):
    """兼容层的鉴权失败必须是 200 + errorId=1（生态客户端不看 HTTP 状态码）。"""
    status, body = _post(live_server, path, payload)
    assert status == 200, f"{path} 应当 200（业务错误经 errorId 表达），实际 {status}"
    assert body.get("errorId") == 1
    assert body.get("errorCode") == "ERROR_KEY_DOES_NOT_EXIST"


def test_unknown_path_without_token_is_401_not_404(live_server):
    """未知路径对**未授权者**回 401 而不是 404：404 会泄露"哪些路径存在"（端点枚举）。"""
    status, _ = _call(live_server, "GET", "/definitely-not-a-route", None)
    assert status == 401


def test_unknown_path_with_token_is_404(live_server):
    status, _ = _call(live_server, "GET", "/definitely-not-a-route", None, token="test-token")
    assert status == 404


def test_wrong_token_is_rejected_everywhere(live_server):
    """错的 token 与没有 token 等价 —— 都不能因为"带了 Authorization 头"就放行。"""
    for method, path, payload in PROTECTED[:5]:
        status, body = _call(live_server, method, path, payload, token="wrong-token")
        assert status == 401, f"{method} {path} 用错 token 返回了 {status}"


def test_compat_non_ascii_client_key_does_not_500(live_server):
    """非 ASCII clientKey 不能把服务打成 500（`compare_digest` 对 str 会抛 TypeError）。"""
    status, body = _post(live_server, "/createTask",
                         {"clientKey": "密码🔑", "task": {"type": "AntiCloudflareTask",
                                                          "websiteURL": "https://e.com/"}})
    assert status == 200, f"非 ASCII clientKey 导致 {status}（应 200 + errorId=1）"
    assert body.get("errorId") == 1
    assert body.get("errorCode") == "ERROR_KEY_DOES_NOT_EXIST"
