"""挑战类型策略：同一个浏览器求解路径，收集不同的产物。

为什么要抽象这一层（而不是在 `_do_solve` 里堆 if）：
  · 四类脚本型挑战的**产物不同**：`cf_clearance` 是 cookie、`aws-waf-token` 是 cookie、
    Turnstile 是一次性 token（页面里的隐藏 input）、reCAPTCHA v3 是一次性 token（`grecaptcha.execute` 的返回值）。
  · 但它们的**获取方式同构**：真浏览器打开页面 → 等/触发浏览器自己算出产物 → 读出来。
    所以只需要"每类一个收集器"，而不是每类一套求解流程。
  · 缓存语义也不同：cookie 类可以按域复用（约 30 分钟），**token 类是一次性的，绝不能缓存**
    （Turnstile ~300s 且绑页面会话；reCAPTCHA v3 ~120s 且绑 action）。混淆这两者会发出
    注定失败的凭证 —— 这正是"看起来有缓存其实全无效"的典型来源。

真机验证状态（诚实披露）：
  · `clearance` 有真机实测（A 组 24/25，见 plans/证据/）。
  · `awswaf` / `turnstile` / `recaptcha_v3` **只有离线单测**（注入假浏览器），
    尚未做真机验证 —— 因为 V-1 的 50 请求预算已在 clearance 上用完。
"""

from __future__ import annotations

from enum import Enum


class Want(str, Enum):
    """要收集的产物类型。字符串值即对外 API 的 `want` 参数。"""

    CLEARANCE = "clearance"        # Cloudflare WAF → cf_clearance cookie
    AWSWAF = "awswaf"             # AWS WAF → aws-waf-token cookie
    TURNSTILE = "turnstile"       # Turnstile widget → cf-turnstile-response token
    RECAPTCHA_V3 = "recaptcha_v3"  # reCAPTCHA v3 → g-recaptcha token

    @classmethod
    def parse(cls, raw: str | None) -> "Want":
        """解析 `want` 参数；空值默认 clearance（保持向后兼容）。"""
        value = (raw or "").strip().lower()
        if not value:
            return cls.CLEARANCE
        for member in cls:
            if member.value == value:
                return member
        raise ValueError(f"未知 want={raw!r}；可选：{[m.value for m in cls]}")


#: 收集"cookie 类"产物时需要等的 cookie 名。
COOKIE_FIELD: dict[Want, str] = {
    Want.CLEARANCE: "cf_clearance",
    Want.AWSWAF: "aws-waf-token",
}

#: 一次性 token 类产物 —— 绝不进缓存。
TOKEN_WANTS = frozenset({Want.TURNSTILE, Want.RECAPTCHA_V3})

#: 允许缓存的产物类型（cookie 类）。
CACHEABLE_WANTS = frozenset(COOKIE_FIELD)

#: 各类型在响应里的产物字段名。
RESULT_FIELD: dict[Want, str] = {
    Want.CLEARANCE: "cf_clearance",
    Want.AWSWAF: "aws_waf_token",
    Want.TURNSTILE: "token",
    Want.RECAPTCHA_V3: "token",
}

#: 一次性 token 的**名义**寿命（秒）。仅用于告诉调用方"尽快用"，不代表可缓存。
NOMINAL_TTL: dict[Want, int] = {
    Want.TURNSTILE: 300,
    Want.RECAPTCHA_V3: 120,
}

#: 各类型在响应里的"字段名/输入名"（方便调用方知道把 token 塞到哪个表单字段）。
TOKEN_FIELD_NAME: dict[Want, str] = {
    Want.TURNSTILE: "cf-turnstile-response",
    Want.RECAPTCHA_V3: "g-recaptcha-response",
}

#: 读取 Turnstile token 的 DOM 选择器（官方 widget 会写进同名隐藏 input）。
TURNSTILE_SELECTOR = "[name=cf-turnstile-response]"

#: 读取 reCAPTCHA v3 token 的 DOM 选择器（execute 后落到同名 textarea）。
RECAPTCHA_SELECTOR = "[name=g-recaptcha-response]"


#: Turnstile 存根页：在**目标 origins** 下渲染官方 widget。
#:
#: 两个关键点（都是踩过才知道的）：
#:  1) 必须在**目标 origin** 下渲染 —— token 绑 origin，换个域拿到的交上去会被拒。
#:     所以用 `route.fulfill` 拦截目标文档 URL、直接返回这段 HTML。
#:  2) 必须用 **`render=explicit` + 主动 `turnstile.render()`**。
#:     `render=explicit` 意味着关闭自动渲染，只放一个 `.cf-turnstile` div 是**不会**出 token 的
#:     （真机上这么写了，白等 60 秒）。要么用隐式模式让它扫 `.cf-turnstile`，要么显式调用 render。
#:     这里选显式：能拿到 callback 回调，token 直接落到 `window.__cf_token`，比轮询 DOM 稳。
TURNSTILE_STUB_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>challenge</title>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>
</head><body>
<div id="ts"></div>
<script>
  window.__cf_token = "";
  window.__cf_err = "";
  (function boot() {
    if (!window.turnstile || typeof window.turnstile.render !== 'function') {
      return setTimeout(boot, 100);
    }
    try {
      window.turnstile.render('#ts', {
        sitekey: __SITEKEY__,
__OPTIONS__
        callback: function (t) { window.__cf_token = t || ""; },
        'error-callback': function (e) { window.__cf_err = String(e); }
      });
    } catch (e) { window.__cf_err = String(e); }
  })();
</script>
</body></html>"""


def _js_str(value: str) -> str:
    """把字符串安全地嵌进内联 <script> 的 JS 字面量里。

    只做 JSON 转义**不够**：`</script>` 会提前闭合脚本块（经典 XSS 向量），
    所以额外把 `<` `>` `&` 转成 unicode 转义 —— 在 JS 字符串里语义完全一致。
    """
    import json as _json

    s = _json.dumps(value, ensure_ascii=True)
    return s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def build_turnstile_stub(sitekey: str, action: str = "", cdata: str = "") -> str:
    """构造 Turnstile 存根页（所有外部输入都经 _js_str 转义，杜绝注入）。"""
    if not sitekey or len(sitekey) > 256:
        raise ValueError("sitekey 缺失或过长")
    opts = []
    if action:
        if len(action) > 256:
            raise ValueError("action 过长")
        opts.append(f"        action: {_js_str(action)},")
    if cdata:
        if len(cdata) > 256:
            raise ValueError("cdata 过长")
        opts.append(f"        cdata: {_js_str(cdata)},")
    # 模板里全是 JS 花括号，**不能**用 str.format（会当成占位符报错），改用占位符替换
    return (
        TURNSTILE_STUB_TEMPLATE
        .replace("__SITEKEY__", _js_str(sitekey))
        .replace("__OPTIONS__", "\n".join(opts))
    )


#: 依次尝试从页面里取 Turnstile token：callback 全局变量 → DOM 隐藏 input。
READ_TURNSTILE_TOKEN = """
() => {
  if (window.__cf_token) return window.__cf_token;
  const el = document.querySelector('[name=cf-turnstile-response]');
  if (el && el.value) return el.value;
  return window.__cf_err ? ('ERR:' + window.__cf_err) : '';
}
"""

def _attr(value: str) -> str:
    if len(value) > 256 or any(ch in value for ch in '"<>\n\r\t'):
        raise ValueError("action/cdata 含非法字符")
    return value


#: 确保 reCAPTCHA 客户端存在（有些站点只有 v3 的 script，或还没加载）。
ENSURE_RECAPTCHA_JS = """
(key) => {
  if (typeof grecaptcha !== 'undefined' && typeof grecaptcha.execute === 'function') return 'present';
  const exists = Array.from(document.scripts || []).some(
    (s) => (s.src || '').indexOf('recaptcha') !== -1
  );
  if (!exists) {
    const s = document.createElement('script');
    s.src = 'https://www.google.com/recaptcha/api.js?render=' + encodeURIComponent(key);
    document.head.appendChild(s);
  }
  return 'injected';
}
"""

#: **点火式**执行 grecaptcha.execute：立刻返回，把结果写进全局变量。
#:
#: 为什么不用 `async () => await grecaptcha.execute(...)`：那个 Promise 在某些页面上
#: 会长久不 resolve（真机实测把 5s 变成 58s），而 Playwright 的 evaluate 会一直等它。
#: 改成"点火 + 轮询全局"就没有这个耦合。
START_RECAPTCHA = """
(args) => {
  window.__rc_token = window.__rc_token || '';
  window.__rc_err = '';
  if (typeof grecaptcha === 'undefined' || typeof grecaptcha.execute !== 'function') {
    return 'missing';
  }
  try {
    grecaptcha.ready(function () {
      try {
        var p = grecaptcha.execute(args.key, args.action ? {action: args.action} : {});
        if (p && typeof p.then === 'function') {
          p.then(function (t) { window.__rc_token = t || ''; })
           .catch(function (e) { window.__rc_err = String(e); });
        } else {
          window.__rc_token = p || '';
        }
      } catch (e) { window.__rc_err = String(e); }
    });
    return 'started';
  } catch (e) { window.__rc_err = String(e); return 'error'; }
}
"""

#: 轮询 reCAPTCHA token：全局变量 → DOM 隐藏字段（有些站点只回填 DOM）。
READ_RECAPTCHA = """
() => {
  if (window.__rc_token) return window.__rc_token;
  const el = document.querySelector('[name=g-recaptcha-response]');
  if (el && el.value) return el.value;
  return window.__rc_err ? ('ERR:' + window.__rc_err) : '';
}
"""
