"""防护识别（探测目标站用的是什么盾），用于"先探测、再决定走哪条路"。

为什么需要（真实痛点）：
  · 纯协议只对 Cloudflare 有意义；对 Akamai/DataDome/PerimeterX 站点跑它纯属浪费。
  · 反过来，遇到 CF 的 `jsd` 老站可以直接走纯协议，而 `managed` 站必须上浏览器。
  · 现在有四条路（纯协议 / Camoufox / pydoll / FlareSolverr），
    **先知道对面是什么，才不会把最贵的操作浪费在错的目标上**。

规则来源：`scrapfly/Antibot-Detector`（MIT）的 `detectors/*/*.json` 里的**字面**特征
（cookie 名 / header / url / 文本）与置信度。用
`python scripts/gen_detect_signatures.py <Antibot-Detector 路径>` 可重新生成下表。

与上游的差别（我们只做服务端能做的事）：
  · 上游是浏览器扩展，能看 DOM / JS hook / window 属性；服务端只能看 HTTP 响应
    （状态码 / 头 / cookie / body），所以只用了这四类信号。
  · 因此结论是"**该站大概率有 X 防护**"，不是扩展那种逐项命中清单。
  · 对 Cloudflare 额外解析 `_cf_chl_opt.cType`（managed / jsd / interactive …），
    这一条决定"能不能走纯协议"。

置信度：0–100，取命中信号里的最大值；同时给出命中条数，便于识别"孤证"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SIGNATURES = [
    {
        'id': 'detect-akamai', 'name': 'Akamai Bot Manager',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', '_abck', 100), ('cookie', 'ak_bmsc', 60), ('cookie', 'sbsd', 100), ('cookie', 'sbsd_o', 100), ('cookie', 'bm_sz', 60), ('cookie', 'bm_sv', 60), ('cookie', 'bm_mi', 60), ('url', '/akam/', 100), ('url', '/.well-known/sbsd/', 100), ('url', '_sec/sbsd/', 100), ('content', 'ak_bmsc', 50), ('content', '_abck', 50), ('content', 'bmak.', 50), ('content', 'sensor_data', 50)],
    },
    {
        'id': 'detect-aws-waf', 'name': 'AWS WAF',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', 'aws-waf-token', 100), ('cookie', 'awswaf*', 100), ('url', '/challenge.js', 100), ('url', 'awswaf', 85), ('url', '/verify', 90), ('url', '/inputs?client=browser', 90), ('url', '/telemetry', 90), ('content', 'challenge.js', 50), ('content', 'awswaf', 50)],
    },
    {
        'id': 'detect-cheq', 'name': 'Cheq',
        'category': 'ANTIBOT', 'difficulty': 'Medium',
        "signals": [('url', 'clicktrue_invocation.js', 100), ('url', 'cheqzone.com', 100), ('url', 'ob.cheqzone.com', 100), ('url', 'cheq.ai', 95), ('content', 'CheqSdk', 50), ('content', '"CHEQ"', 50), ('content', 'cheq_invalidUsers', 50), ('content', 'cheq', 50)],
    },
    {
        'id': 'detect-cloudflare', 'name': 'Cloudflare Bot Management',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', '__cf_bm', 50), ('cookie', 'cf_clearance', 100), ('url', 'cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/', 90), ('url', 'challenges.cloudflare.com', 100)],
    },
    {
        'id': 'detect-datadome', 'name': 'DataDome',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', 'datadome', 100), ('header', 'x-datadome-cid', 100)],
    },
    {
        'id': 'detect-incapsula', 'name': 'Incapsula (Imperva)',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', 'incap_ses_', 100), ('cookie', 'visid_incap_', 95), ('cookie', 'nlbi_', 90), ('cookie', 'reese84', 100), ('cookie', 'utmvc', 100), ('url', 'incapsula.com', 100), ('url', '/_Incapsula_Resource', 100), ('content', '_Incapsula', 50), ('content', 'incapsula', 50)],
    },
    {
        'id': 'detect-kasada', 'name': 'Kasada',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('url', 'ips.js', 95), ('content', 'kasada', 50)],
    },
    {
        'id': 'detect-meetrics', 'name': 'Meetrics Check',
        'category': 'ANTIBOT', 'difficulty': 'Medium',
        "signals": [('url', 'mxcdn.net/bb-mx/serve/mtrcs', 100), ('url', 'mtrcs_', 95), ('url', 'meetrics.com', 90), ('content', 'meetricsGlobal', 50), ('content', 'suspicious_mouse_movement', 50), ('content', 'meetrics', 50)],
    },
    {
        'id': 'detect-ocule', 'name': 'Ocule',
        'category': 'ANTIBOT', 'difficulty': 'Medium',
        "signals": [('url', 'proxy.ocule.co.uk/script.js', 100), ('url', 'ocule.co.uk', 95), ('url', 'ocule', 75)],
    },
    {
        'id': 'detect-perimeterx', 'name': 'PerimeterX',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', '_px2', 100), ('cookie', '_px3', 100), ('cookie', '_pxhd', 100), ('cookie', '_pxvid', 100), ('url', 'collector-*.perimeterx.net', 100), ('url', '/api/v*/collector', 95), ('content', 'window._pxAppId', 50), ('content', 'pxInit', 50), ('content', '_pxAction', 50)],
    },
    {
        'id': 'detect-reblaze', 'name': 'Reblaze',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('cookie', 'rbzid', 100), ('cookie', 'rbzsessionid', 100), ('cookie', 'rbz*', 100), ('content', 'rbzid', 50), ('content', 'reblaze', 50)],
    },
    {
        'id': 'detect-shapesecurity', 'name': 'Shape Security',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('content', 'shapesecurity', 50)],
    },
    {
        'id': 'detect-sucuri', 'name': 'Sucuri WAF',
        'category': 'Anti-Bot', 'difficulty': 'Medium',
        "signals": [('content', 'sucuri', 50)],
    },
    {
        'id': 'detect-threatmetrix', 'name': 'ThreatMetrix',
        'category': 'ANTIBOT', 'difficulty': 'Medium',
        "signals": [('url', 'fp/check.js', 95), ('url', 'org_id=', 85), ('content', 'ThreatMetrix', 50)],
    },
    {
        'id': 'detect-aliexpress', 'name': 'AliExpress CAPTCHA',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'punish?x5secdata', 100), ('url', 'punish\\?x5secdata', 100), ('content', 'x5secdata', 50), ('content', 'aliexpress.com/punish', 50)],
    },
    {
        'id': 'detect-captchaeu', 'name': 'Captcha.eu',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'captcha.eu', 100), ('url', '/api/captcha', 100), ('url', 'captchaeu', 90), ('content', 'CaptchaEU', 50), ('content', 'captchaeu', 50), ('content', 'captcha.eu/widget', 50)],
    },
    {
        'id': 'detect-friendlycaptcha', 'name': 'Friendly Captcha',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'friendlycaptcha.com', 100), ('url', '/api/v1/puzzle', 95), ('url', 'friendly-js.pages.dev', 95), ('content', 'frc-captcha', 50), ('content', 'friendlyChallenge', 50), ('content', 'friendly-challenge', 50), ('content', 'FriendlyCaptcha', 50)],
    },
    {
        'id': 'detect-funcaptcha', 'name': 'FunCaptcha (Arkose Labs)',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'client-api.arkoselabs.com', 100), ('url', 'api.funcaptcha.com', 100), ('url', '/fc/api/nojs/', 95), ('url', '/enforcement.*.js', 90), ('content', 'window.arkoseCallback', 50), ('content', 'ArkoseEnforce', 50), ('content', 'funcaptcha', 50), ('content', 'setupEnforcement', 50)],
    },
    {
        'id': 'detect-geetest', 'name': 'GeeTest',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'api.geetest.com', 100), ('url', 'static.geetest.com', 100), ('url', '/gettype.php', 100), ('url', '/get.php', 50), ('content', 'window.initGeetest', 50), ('content', 'geetest', 50), ('content', 'getGeetest', 50), ('content', 'geetestUtils', 50)],
    },
    {
        'id': 'detect-hcaptcha', 'name': 'hCaptcha',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'hcaptcha.com', 100), ('url', '/checksiteconfig', 100), ('url', '/getcaptcha', 100), ('content', 'hcaptcha', 50), ('content', 'h-captcha', 50), ('content', 'hcaptcha.render', 50), ('content', 'hcaptcha.execute', 50)],
    },
    {
        'id': 'detect-qcloud', 'name': 'QCloud Captcha',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'turing.captcha.qcloud.com', 100), ('url', 'turing.captcha.gtimg.com', 100), ('url', 'tdc.js', 95), ('content', 'TencentCaptcha', 50), ('content', 'qcloud.captcha', 50), ('content', 'turing.captcha', 50)],
    },
    {
        'id': 'detect-recaptcha', 'name': 'Google reCAPTCHA',
        'category': 'CAPTCHA', 'difficulty': 'High',
        "signals": [('url', 'recaptcha/api', 100), ('url', 'gstatic.com/recaptcha', 100), ('url', 'google.com/recaptcha', 100), ('url', 'recaptcha.net', 100), ('content', 'grecaptcha', 50), ('content', 'recaptcha', 50), ('content', 'g-recaptcha', 50), ('content', 'grecaptcha.execute', 50), ('content', 'grecaptcha.render', 50)],
    },
    {
        'id': 'detect-battery-fingerprint', 'name': 'Battery API Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'getBattery', 50), ('content', 'batteryManager', 50)],
    },
    {
        'id': 'detect-clipboard-fingerprint', 'name': 'Clipboard API Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'navigator.clipboard', 50), ('content', 'readText', 50)],
    },
    {
        'id': 'detect-css-fingerprint', 'name': 'CSS Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'CSS.supports(', 50), ('content', 'getComputedStyle', 50)],
    },
    {
        'id': 'detect-gamepads-fingerprint', 'name': 'Gamepads API Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'getGamepads', 50), ('content', 'gamepadconnected', 50), ('content', 'Gamepad', 50)],
    },
    {
        'id': 'detect-geolocation-fingerprint', 'name': 'Geolocation Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'navigator.geolocation', 50), ('content', 'getCurrentPosition', 50), ('content', 'watchPosition', 50)],
    },
    {
        'id': 'detect-hardware-fingerprint', 'name': 'Hardware Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'hardwareConcurrency', 50), ('content', 'deviceMemory', 50), ('content', 'maxTouchPoints', 50)],
    },
    {
        'id': 'detect-indexeddb-fingerprint', 'name': 'IndexedDB Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'indexedDB.open', 50), ('content', 'IDBDatabase', 50)],
    },
    {
        'id': 'detect-orientation-fingerprint', 'name': 'Device Orientation Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'deviceorientation', 50), ('content', 'devicemotion', 50), ('content', 'DeviceOrientationEvent', 50)],
    },
    {
        'id': 'detect-storage-fingerprint', 'name': 'Web Storage Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'localStorage.setItem', 50), ('content', 'sessionStorage', 50), ('content', 'Storage.prototype', 50)],
    },
    {
        'id': 'detect-timezone-fingerprint', 'name': 'Timezone/Intl Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'Intl.DateTimeFormat', 50), ('content', 'resolvedOptions', 50)],
    },
    {
        'id': 'detect-usb-fingerprint', 'name': 'USB API Fingerprint',
        'category': 'Fingerprint', 'difficulty': 'Low',
        "signals": [('content', 'navigator.usb', 50), ('content', 'getDevices', 50), ('content', 'requestDevice', 50)],
    },
]

@dataclass
class Detection:
    """一个厂商的命中结果。"""

    id: str
    name: str
    category: str
    difficulty: str
    confidence: int
    hits: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "difficulty": self.difficulty,
            "confidence": self.confidence,
            "hits": self.hits,
        }


#: `_cf_chl_opt.cType` → （人话, 该走哪条路）
CTYPE_ROUTE = {
    "managed": ("CF managed challenge（静默型，需真实浏览器环境）", "browser"),
    "interactive": ("CF interactive challenge（可能需点选）", "browser"),
    "embedded": ("CF embedded challenge（iframe 内 widget）", "browser"),
    "non-interactive": ("CF non-interactive（等它自己放行即可）", "browser"),
    "jsd": ("CF jsd（旧式非交互检测）", "pure_protocol_may_work"),
    "turnstile": ("CF Turnstile widget（一次性 token）", "browser_token"),
}

_CTYPE_RE = re.compile(r"""cType['\"]?\s*[:=]\s*['\"]([A-Za-z_-]+)['\"]""")

#: 我们自己补的**强特征**（比上游表更贴合 2026 年 CF 的实际形态）。
#: 上游表里的 CF 特征只有 `__cf_bm` / `cf_clearance` cookie，遇到"还没拿到 cookie 的挑战页"会漏判。
STRONG_SIGNALS = [
    ("detect-cloudflare", "Cloudflare", "Anti-Bot", "Medium", "header", "cf-mitigated", 100),
    ("detect-cloudflare", "Cloudflare", "Anti-Bot", "Medium", "text", "_cf_chl_opt", 95),
    ("detect-cloudflare", "Cloudflare", "Anti-Bot", "Medium", "text", "/cdn-cgi/challenge-platform/", 95),
    ("detect-cloudflare", "Cloudflare", "Anti-Bot", "Medium", "text", "challenges.cloudflare.com", 85),
    ("detect-aws-waf", "AWS WAF", "Anti-Bot", "Medium", "text", "aws-waf-token", 95),
]


def cf_challenge_type(body: str, headers: dict | None = None, url: str = "") -> str:
    """尽力判断 CF 挑战类型；判断不出返回空串。"""
    headers = headers or {}
    lowered = {k.lower() for k in headers}
    text = (body or "")[:400_000]
    low_url = (url or "").lower()
    if "challenges.cloudflare.com/turnstile" in text.lower() or "/turnstile/" in low_url:
        return "turnstile"
    match = _CTYPE_RE.search(text)
    if match:
        return match.group(1).lower()
    if "cf-mitigated" in lowered:
        return "managed"
    if "cdn-cgi/challenge-platform" in text or "cdn-cgi/challenge-platform" in low_url:
        return "managed"
    return ""


def detect(
    status: int = 0,
    headers: dict | None = None,
    body: str = "",
    url: str = "",
    cookies: dict | None = None,
) -> list[Detection]:
    """按特征表匹配，返回按置信度降序的命中列表。"""
    headers = headers or {}
    cookies = cookies or {}
    lowered_headers = {k.lower(): str(v) for k, v in headers.items()}
    text = (body or "")[:400_000].lower()
    # 先跑强特征（命中的话置信度更高，也覆盖"挑战页还没下 cookie"的情况）
    strong: dict[str, Detection] = {}
    for det_id, name, category, difficulty, kind, token, confidence in STRONG_SIGNALS:
        matched = False
        if kind == "header":
            matched = token.lower() in lowered_headers
        elif kind == "cookie":
            matched = any(_cookie_matches(n, token) for n in cookies)
        else:
            matched = token.lower() in text
        if not matched:
            continue
        entry = strong.get(det_id)
        if entry is None:
            strong[det_id] = Detection(
                id=det_id, name=name, category=category, difficulty=difficulty,
                confidence=confidence, hits=[kind + ":" + token],
            )
        else:
            entry.confidence = max(entry.confidence, confidence)
            entry.hits.append(kind + ":" + token)

    found: list[Detection] = list(strong.values())
    for signature in SIGNATURES:
        hits: list[str] = []
        best = 0
        for kind, token, confidence in signature["signals"]:
            if kind == "cookie":
                if any(_cookie_matches(name, token) for name in cookies):
                    hits.append("cookie:" + token)
                    best = max(best, confidence)
            elif kind == "header":
                if token.lower() in lowered_headers:
                    hits.append("header:" + token)
                    best = max(best, confidence)
            else:
                needle = token.lower()
                if needle and needle in text:
                    hits.append(kind + ":" + token)
                    best = max(best, confidence)
        if hits:
            existing = next((d for d in found if d.id == signature["id"]), None)
            if existing is not None:
                existing.confidence = max(existing.confidence, best)
                for h in hits[:8]:
                    if h not in existing.hits:
                        existing.hits.append(h)
                continue
            found.append(
                Detection(
                    id=signature["id"],
                    name=signature["name"],
                    category=signature["category"],
                    difficulty=signature["difficulty"] or "",
                    confidence=best,
                    hits=hits[:8],
                )
            )
    found.sort(key=lambda d: (-d.confidence, d.name))
    return found


def _cookie_matches(name: str, pattern: str) -> bool:
    """支持上游表里的 `awswaf*` 这种尾部通配。"""
    if pattern.endswith("*"):
        return name.lower().startswith(pattern[:-1].lower())
    return name.lower() == pattern.lower()


def recommend(detections: list[Detection], ctype: str = "") -> dict:
    """把"对面是什么"翻译成"我们该怎么做"。"""
    if not detections and not ctype:
        return {
            "route": "direct",
            "reason": "未命中已知防护特征——大概率没有盾，直接抓",
            "hint": "若实际拿到 403/挑战页，说明该厂商未被特征表覆盖，请补规则",
        }

    names = {d.id for d in detections}
    top = detections[0] if detections else None

    if "detect-cloudflare" in names or ctype:
        if ctype == "turnstile":
            return {"route": "browser_token", "want": "turnstile",
                    "reason": "检测到 Turnstile widget",
                    "hint": "POST /solve 加 want=turnstile + sitekey"}
        if ctype in CTYPE_ROUTE:
            text, route = CTYPE_ROUTE[ctype]
            return {
                "route": route,
                "reason": text,
                "hint": ("纯协议（cfsolve）可能可用：先试它、失败回落浏览器"
                         if route == "pure_protocol_may_work"
                         else "用浏览器引擎（默认 Camoufox）"),
            }
        # CF 在场但没看到挑战标记：先直接抓，被拦了再走求解
        return {"route": "direct_unless_blocked",
                "reason": "站点使用 Cloudflare，但当前响应里没有挑战标记",
                "hint": "先直接抓；若拿到 403 → 用 POST /solve（默认 clearance）"}
    if "detect-aws-waf" in names:
        return {"route": "browser_cookie", "want": "awswaf", "reason": "检测到 AWS WAF",
                "hint": "POST /solve 加 want=awswaf"}
    assert top is not None      # 走到这里说明有命中
    return {
        "route": "unsupported_or_browser",
        "reason": "检测到非 CF 防护：" + top.name + "（难度 " + top.difficulty + "）",
        "hint": "本仓库求解只覆盖 Cloudflare / AWS WAF；这类站建议换入口或人工处理",
    }


def summarize(
    url: str,
    status: int = 0,
    headers: dict | None = None,
    body: str = "",
    cookies: dict | None = None,
) -> dict:
    """一步到位：探测 + 挑战类型 + 建议。"""
    headers = headers or {}
    detections = detect(status=status, headers=headers, body=body, url=url, cookies=cookies)
    looks_cf = any(d.id == "detect-cloudflare" for d in detections) or (
        "cf-mitigated" in {k.lower() for k in headers}
    )
    ctype = cf_challenge_type(body, headers, url) if looks_cf else ""
    return {
        "url": url,
        "status": status,
        "protected": bool(detections),
        "providers": [d.as_dict() for d in detections],
        "cloudflare_challenge_type": ctype or None,
        "recommendation": recommend(detections, ctype),
    }
