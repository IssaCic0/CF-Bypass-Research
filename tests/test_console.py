"""T-78：单文件只读控制台 `GET /console`。

五条硬约束（每条都有用例）：
  1. 零写入：无 form / POST
  2. 零新依赖：HTML 全部内联在 solver/console_html.py 的字符串常量里
  3. token 保护：与其它端点同一套 Bearer；页面不含 token，前端 prompt() 现问存 sessionStorage
  4. CSP + 无内联事件：CSP 头 default-src 'none'；无 innerHTML / onclick=
  5. 数据脱敏：只渲染白名单字段（不渲染 last_error 原文等）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import service  # noqa: E402
import console_html  # noqa: E402
from test_service import live_server, _get, _post  # noqa: E402,F401

HOST = "https://example.com/"


def test_console_requires_token(live_server):
    """无 token → 401（/console 不像 / 那样免鉴权）。"""
    status, body = _get(live_server, "/console")
    assert status == 401


def test_console_with_token_returns_html(live_server):
    status, body = _get(live_server, "/console", token="test-token")
    assert status == 200
    assert body.lstrip().lower().startswith("<!doctype html")
    assert "只读控制台" in body


def test_console_html_has_no_write_surface(live_server):
    """灵魂用例：返回 HTML 不得含任何写面。"""
    status, body = _get(live_server, "/console", token="test-token")
    assert status == 200
    low = body.lower()
    assert "<form" not in low
    assert 'method="post"' not in low and "method='post'" not in low
    assert "method: \"post\"" not in low and "method: 'post'" not in low
    assert "innerhtml" not in low
    assert "onclick=" not in low
    assert "outerhtml" not in low
    assert "document.write" not in low


def test_console_sets_csp_and_no_store(live_server):
    """CSP 头存在且含 default-src 'none'；Cache-Control: no-store。"""
    import urllib.request
    req = urllib.request.Request(f"{live_server}/console")
    req.add_header("Authorization", "Bearer test-token")
    with urllib.request.urlopen(req, timeout=10) as r:
        csp = r.headers.get("Content-Security-Policy", "")
        cache = r.headers.get("Cache-Control", "")
        xcto = r.headers.get("X-Content-Type-Options", "")
    assert "default-src 'none'" in csp
    assert cache == "no-store"
    assert xcto == "nosniff"


def test_console_does_not_embed_token_or_cookies(live_server, monkeypatch):
    """页面体内不含 token 值，也不含 cf_clearance 值。"""
    # 把服务 token 换成可识别的哨兵值再请求
    token = "sentinel-token-4f8a2b"
    old = service.Handler.token
    service.Handler.token = token
    try:
        import urllib.request
        req = urllib.request.Request(f"{live_server}/console")
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8")
    finally:
        service.Handler.token = old
    assert token not in body
    assert "cf_clearance" not in body


def test_console_page_renders_whitelist_fields_only():
    """前端 JS 只读白名单字段（错误原文与黑名单字段绝不出现）。"""
    js = console_html.CONSOLE_PAGE_HTML
    # 错误**原文**绝不允许被前端读取（服务端白名单是第一道，前端不读是第二道）。
    # 注意 last_error_at（时间戳）是允许的 —— 断言用词边界，不误伤。
    import re as _re
    assert not _re.search(r"(?<![_a-zA-Z])last_error(?![_a-zA-Z])", js), "前端不得读 last_error 原文"
    # 允许的白名单数据源字段
    for f in ("state", "solves", "cache_hits", "failures", "cached_domains",
              "uptime_sec", "corpus_variants"):
        assert f in js, f"白名单字段 {f} 应被渲染"


def test_console_has_four_sections_with_empty_states():
    """四个区块与各自的空态文案都在。"""
    html = console_html.CONSOLE_PAGE_HTML
    for section in ("服务状态", "缓存与命中", "研究资产", "最近错误"):
        assert section in html, f"缺区块 {section}"
    assert "未收到 /health 响应：检查服务是否启动" in html
    assert "缓存为空：尚未解过任何域" in html
    assert "corpus 为空：需一次性抓取授权" in html
    assert "无错误" in html


def test_console_no_external_resources():
    """零新依赖：不得引用任何外部资源（cdn/静态文件/网络字体）。"""
    html = console_html.CONSOLE_PAGE_HTML
    assert not re.search(r'(src|href)\s*=\s*["\'](https?:)?//', html)
    assert "<link" not in html.lower() or 'rel="stylesheet" href="/' not in html.lower()
