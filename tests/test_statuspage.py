"""只读状态页（`GET /`）的行为契约测试（v6.2 新增）。

为什么现在才补：`solver/statuspage.py` 的覆盖率一直是 100%，但那是**假象** ——
模块体只是一个字符串常量，import 一次就"全覆盖"，而页面里的 JS 逻辑**一条都没被测过**。
上一轮 UI 截图验收就是这么揪出"服务已死但卡片仍用绿色"的（自动化断言全绿）。

这组用例覆盖两件能离线验证的事：
  1. **HTTP 契约**：`GET /` 免鉴权、带 CSP/no-store/nosniff、不回任何服务端数据；
  2. **前端逻辑的静态契约**：新状态/新字段必须在页面里有对应渲染路径
     （`wedged` 的徽章样式、原因、处置动作；`captcha_types`；`multicaptcha_inflight`）。
     真正"按钮能不能按成功"只能靠真浏览器验收（`scripts/verify_ui_e2e.py`）。
"""

from __future__ import annotations


from solver.statuspage import STATUS_PAGE_HTML, STATUS_PAGE_HTML_BYTES
from test_service import _get, live_server  # noqa: F401 —— fixture 需被收集


# ── HTTP 契约 ─────────────────────────────────────


def test_status_page_is_public_and_html(live_server):  # noqa: F811
    base = live_server
    status, body = _get(base, "/")
    assert status == 200
    assert "<!doctype html>" in body.lower()
    assert "CF 求解服务" in body


def test_status_page_carries_security_headers(live_server):  # noqa: F811
    base = live_server
    status, body = _get(base, "/")
    assert status == 200
    # 头部断言在 test_service 的 httplib 层做不了，这里用一次原始请求补上
    import urllib.request
    req = urllib.request.Request(base + "/")
    with urllib.request.urlopen(req, timeout=10) as r:
        h = r.headers
        csp = h.get("Content-Security-Policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp
        assert h.get("Cache-Control") == "no-store"
        assert h.get("X-Content-Type-Options") == "nosniff"


def test_status_page_embeds_no_server_data():
    """页面是静态的：不含 token、不含 cookie、不含任何真实域名。"""
    assert "CF_SOLVE_TOKEN" in STATUS_PAGE_HTML  # 只在文案里提到"去 .env 取"
    assert "cf_clearance=" not in STATUS_PAGE_HTML
    assert "127.0.0.1" not in STATUS_PAGE_HTML


def test_status_page_length_matches_bytes():
    assert len(STATUS_PAGE_HTML_BYTES) == len(STATUS_PAGE_HTML.encode("utf-8"))


# ── 前端逻辑的静态契约（v6.2 回归：新状态必须真的被渲染）──────


def test_wedged_has_its_own_badge_style():
    """wedged 必须有专属样式：它是唯一"等不会好"的状态，不能和 down 混用配色。

    回归的缺陷：v6.2 加了 `state=wedged`，但 CSS 里没有 `.badge.wedged` ——
    真 Chrome 实测徽章渲染成默认深色（rgb(31,35,40)），**服务已死却看起来正常**。
    """
    assert ".badge.wedged" in STATUS_PAGE_HTML
    # 必须是"实心反色"（比 down 更刺眼），不是只换个文字颜色
    import re
    rule = re.search(r"\.badge\.wedged\s*\{([^}]*)\}", STATUS_PAGE_HTML)
    assert rule, "找不到 .badge.wedged 规则"
    body = rule.group(1)
    assert "background" in body and "color:#fff" in body.replace(" ", "")


def test_wedged_reason_and_action_are_rendered():
    """卡死原因与处置动作必须出现在页面上 —— 只显示一个英文词等于没说。"""
    assert "wedged_reason" in STATUS_PAGE_HTML
    assert "wedge_restart" in STATUS_PAGE_HTML
    assert "退出码 75" in STATUS_PAGE_HTML


def test_multicaptcha_inflight_is_rendered_separately():
    """嫁接层在途必须与主链路分开展示（两者超时/产物语义不同）。"""
    assert "multicaptcha_inflight" in STATUS_PAGE_HTML
    assert "captchas/" in STATUS_PAGE_HTML


def test_captcha_types_are_rendered_in_their_own_card():
    """11 个扩展类型是 v6.1 交付的核心能力，运维面板必须能看见。

    回归（看截图才发现）：塞进健康卡里会换行 4 行，把那张卡撑得比旁边高一大截。
    现在独立成一张全宽卡 + 标签式排版。
    """
    assert "captcha_types" in STATUS_PAGE_HTML
    assert 'id="captcha-types"' in STATUS_PAGE_HTML
    assert "card wide" in STATUS_PAGE_HTML
    assert ".chip" in STATUS_PAGE_HTML


def test_setkv_restores_its_class():
    """回归：`setEmpty()` 会把 dl 的 class 改成 empty，setKV 必须还原。

    实测后果：class 停在 "empty" → `display:grid` 失效 → 键值从两列退化成上下堆叠
    （控制台截图里 state / wedged 各占一行就是这么来的）。
    """
    assert 'el.className = "kv"' in STATUS_PAGE_HTML


def test_inflight_table_is_cleared_before_render():
    """回归：mc 为空时没清空容器 → 上一轮的表格会残留、新表格被追加在它后面。

    这是写这版渲染逻辑时实际引入又修掉的缺陷（`insertAdjacentHTML` 需要干净容器）。
    """
    assert 'inflightTableEl.innerHTML = ""' in STATUS_PAGE_HTML


def test_status_page_has_no_write_surface():
    """只读页零写入：没有表单、没有 POST、没有按钮触发的写操作。"""
    lowered = STATUS_PAGE_HTML.lower()
    assert "<form" not in lowered
    assert "method=\"post\"" not in lowered and "method='post'" not in lowered
    assert "fetch(\"/solve" not in lowered and "fetch('/solve" not in lowered


def test_status_page_uses_textcontent_not_innerhtml_for_values():
    """DOM 写入面收敛：动态值走 textContent/setKV，不拼 innerHTML（防 XSS）。"""
    assert "dd.textContent" in STATUS_PAGE_HTML
    # 唯一允许的 innerHTML 是表格结构（值都过了 esc()）
    assert "esc(" in STATUS_PAGE_HTML
