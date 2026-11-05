"""`route=auto`：让 `/detect` 的结论驱动 `/solve` 的决策（v4.0 T-32）。

为什么需要这一层：
  `/detect` 已经能算出"对面是什么盾、建议走哪条路"（真机 3 站分类正确），
  `/solve` 也有 4 个后端。但**两者之间没有数据流** —— 调用方必须自己调两次、
  自己写 if。结果是：

    · 无盾站也被送进浏览器，白付 16–20s 与 1.2GB 内存；
    · 明确不支持的站（DataDome / PerimeterX / Kasada…）也被盲目求解 45s，
      最后返回一个 `unsolved`，调用方无法区分"我们不会"与"这次没解出来"。

这层是**纯函数**，不碰网络、不碰浏览器，所以可以 100% 离线单测。
决策表见 `plans/下一步改进指南.md` §7.3。

设计纪律：
  · 调研结论是"建议"，**不是命令**：任何异常/未知都退回"照常求解"，绝不因为探测失败而拒绝服务；
  · 只有两个动作值得改变主流程：`skip`（没盾，别浪费时间）与 `reject`（不会，别假装会）；
  · 剩下的都是**偏好提示**（`prefer`），由既有后端链自己决定怎么用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 允许的 `route` 取值。缺省（不传）等价于 `fixed` = v3 的行为，保证向后兼容。
ROUTE_MODES = frozenset({"auto", "direct", "pure", "browser", "fixed"})

#: 厂商能力表**只有一份**（v6.2 起在 `solver/vendors.py`）。
#: 为什么从本文件搬走：原来 `recommend()` 与 `decide()` 各有一份，v6.1/v6.2 分别改过，
#: 于是出现真实分叉（差分 Oracle 抓到：CF+kasada 被 recommend 说"能解"、被 decide 拒绝）。
#: 这里 re-export 保持既有引用零改动。
try:  # 包导入
    from . import vendors as _vendors
except ImportError:  # 直接以脚本运行
    import vendors as _vendors  # type: ignore[no-redef]

# ⚠️ 全部从 `_vendors` 取，**不再写 `from vendors import ...`**（独立复审抓到）。
# 那种绝对导入在"包方式导入"下会 ModuleNotFoundError（上面 try 分支成功后模块名是
# `solver.vendors`，顶层 `vendors` 并不存在）。当前所有入口都是脚本式，所以没炸，
# 但 `from solver.detect import ...` 这种写法一用在 route 上就会炸。
_MAIN_CHAIN = _vendors.MAIN_CHAIN_PROVIDERS
MULTICAPTCHA_PROVIDERS = _vendors.MULTICAPTCHA_PROVIDERS
SOLVABLE_PROVIDER_IDS = _vendors.SOLVABLE_PROVIDERS
_pick_multicaptcha = _vendors.pick_multicaptcha
_unsolvable_from = _vendors.unsolvable_from

#: CF 挑战类型 → **本次请求先试哪个后端**（顺序偏好，不是"能不能解"的判定）。
#:
#: ⚠️ 与 `detect.CTYPE_ROUTE` 的差异是**有意的**，两者回答的问题不同：
#:   · `detect.CTYPE_ROUTE` 回答"这类挑战**要靠什么**才能过"（对外建议，保守）；
#:   · 本表回答"后端链里**先试谁**"（纯顺序，`pure` 只是把 pydoll/flaresolverr 挪后，
#:     cfsolve 与 camoufox 仍在链上，失败会继续回落）。
#: 所以 `managed` 在这里是 `pure`（先试纯协议，实测同站比浏览器快 1.1s）而在
#: `detect` 那边是 `browser`（对外建议走浏览器更稳）—— 两者可以同时为真。
#: 真正的不变量是：**`pure` 不能把浏览器踢出链**（`skip_backends("pure")` 只跳
#: pydoll/flaresolverr）。`tests/test_differential_oracle.py` 有一条用例守它。
#: 要改这里的值，必须先有该 ctype 的实测通过率，不能凭直觉对齐。
_CHALLENGE_PREFERS = {
    "turnstile": "browser",
    "recaptcha": "browser",
    "interactive": "browser",
    "embedded": "browser",
    "managed": "pure",
    "jsd": "pure",
    "non-interactive": "pure",
    # 兼容旧拼写：万一是从别处（人工/缓存）传进来的下划线形式，别静默降级
    "non_interactive": "pure",
}

#: 动作
ACTION_SOLVE = "solve"
ACTION_SKIP = "skip"
ACTION_REJECT = "reject"


@dataclass
class RouteDecision:
    """一次路由决策。`as_dict()` 会原样进响应里的 `routed_by`，所以字段要稳定可读。"""

    mode: str
    action: str
    reason: str
    prefer: str = "any"          # any | pure | browser
    hint: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        out = {"mode": self.mode, "action": self.action, "reason": self.reason, "prefer": self.prefer}
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
        return out


def _provider_ids(detect: dict | None) -> set[str]:
    """从探测结论里取**盾类**厂商 id。

    ⚠️ 必须按 `category` 过滤：`Fingerprint` 类命中（localStorage / clipboard /
    Intl.DateTimeFormat …）是**技术特征**，任何现代站点都有。把它们当成"解不了的厂商"，
    会让几乎所有站被 `reject`（v6.2 开发中实测踩到：一个普通 CF 站只因用了 localStorage
    就被判 unsupported-provider）。没有 category 字段时按"是盾"处理（向后兼容旧 payload）。
    """
    if not isinstance(detect, dict):
        return set()
    out = set()
    for p in detect.get("providers") or []:
        if not isinstance(p, dict) or not p.get("id"):
            continue
        category = p.get("category")
        if category is not None and not _vendors.is_shield(str(category)):
            continue
        out.add(str(p["id"]))
    return out


def decide(detect: dict | None, mode: str = "auto") -> RouteDecision:
    """把探测结论翻译成一个决策。**纯函数**：同样的输入永远同样的输出。

    `detect=None` 表示"没有探测结果"（未探测 / 探测失败），此时一律照常求解。
    """
    mode = (mode or "fixed").strip().lower()
    if mode not in ROUTE_MODES:
        # 调用方写错了：退回 fixed（不抛异常——参数校验在 handler 层做，这里只保守决策）
        return RouteDecision(mode="fixed", action=ACTION_SOLVE, reason="unknown-mode")

    if mode == "fixed":
        return RouteDecision(mode="fixed", action=ACTION_SOLVE, reason="forced-fixed",
                             hint="按后端链顺序求解（v3 行为）")

    if mode == "direct":
        return RouteDecision(mode="direct", action=ACTION_SKIP, reason="forced-direct",
                             hint="调用方指定不求解：直接用目标站（若实际 403，改 route=browser）")

    if mode == "pure":
        return RouteDecision(mode="pure", action=ACTION_SOLVE, reason="forced-pure", prefer="pure",
                             hint="调用方指定纯协议优先；未配置纯协议后端时会自动回落浏览器")

    if mode == "browser":
        return RouteDecision(mode="browser", action=ACTION_SOLVE, reason="forced-browser", prefer="browser")

    # ── auto：看探测结论 ─────────────────────────────
    if not isinstance(detect, dict):
        return RouteDecision(mode="auto", action=ACTION_SOLVE, reason="detect-unavailable", prefer="any",
                             hint="探测不可用（未开启或失败），按默认后端链求解")

    if not detect.get("protected"):
        return RouteDecision(
            mode="auto", action=ACTION_SKIP, reason="no-protection", prefer="any",
            hint="探测未命中任何防护特征，直接请求目标站即可（省掉一次 16–20s 的浏览器求解）",
            detail={"status": detect.get("status")},
        )

    ids = _provider_ids(detect)
    # 兼容**旧缓存**：`/detect` 的结论会在 TTL 内跨版本存活。升级瞬间可能拿到
    # `protected=true`（旧口径 `bool(detections)`）但 providers 全是指纹的 payload ——
    # 按 category 过滤后 ids 为空。此时"没有任何可识别的盾 + 没有 challenge 类型"
    # 就该按无防护处理（skip），否则会对无盾站起浏览器。
    if detect.get("protected") and not ids and not detect.get("cloudflare_challenge_type"):
        return RouteDecision(
            mode="auto", action=ACTION_SKIP, reason="no-protection",
            hint="探测未命中任何**盾类**特征（可能只命中指纹类，或旧缓存口径），直接请求目标站即可",
        )
    # v6.1 万盾嫁接：captchas/ 能解的厂商不再算 unsupported
    # ⚠️ 顺序很重要：**先判不可解，再判怎么解**。
    # 反过来写过的版本有个真实缺陷（差分 Oracle 抓到）：一页同时命中 perimeterx（能解）
    # 与 kasada（解不了）时，multicaptcha 分支先命中就返回了 "solve"，
    # 等于对一个**确实过不去**的站假承诺 —— 调用方会白等一个完整超时。
    unsupported = _unsolvable_from(ids)
    if unsupported:
        names = sorted({str(p.get("name") or p.get("id")) for p in (detect.get("providers") or [])
                        if isinstance(p, dict) and str(p.get("id")) in set(unsupported)})
        return RouteDecision(
            mode="auto", action=ACTION_REJECT, reason="unsupported-provider",
            hint=("该站使用本服务不支持的防护（" + "、".join(names) + "）。"
                  "换盾方案比换后端有效：这里既没有对应的求解器，硬试只会白等一个完整超时。"),
            detail={"unsupported": unsupported, "providers": names},
        )

    _mc_id, _mc_want = _pick_multicaptcha(ids)
    if _mc_id and not ids & _MAIN_CHAIN:
        # 非 CF/AWS 的主防护 → 直接交给 captchas/ 子包（自动分发对应 want）
        return RouteDecision(
            mode="auto", action=ACTION_SOLVE, reason="multicaptcha",
            prefer="multicaptcha", detail={"multicaptcha_want": _mc_want, "provider": _mc_id},
            hint=f"检测到 {_mc_id}：自动分发到 captchas/（want={_mc_want}）",
        )

    if "detect-aws-waf" in ids:
        return RouteDecision(
            mode="auto", action=ACTION_SOLVE, reason="aws-waf", prefer="browser",
            hint="AWS WAF 令牌需要浏览器上下文；若调用方要 token，请把 want 设为 awswaf",
            detail={"aware_of_want": "awswaf"},
        )

    ctype = str(detect.get("cloudflare_challenge_type") or "").lower()
    prefer = _CHALLENGE_PREFERS.get(ctype, "any")
    return RouteDecision(
        mode="auto", action=ACTION_SOLVE, reason=f"cloudflare-{ctype or 'unknown'}", prefer=prefer,
        hint=("纯协议优先（零浏览器）" if prefer == "pure" else
              "该挑战类型需要浏览器内取 token" if prefer == "browser" else ""),
        detail={"challenge_type": ctype or None},
    )


#: 失败原因码（写进 `routed_by.escalations[].reason`，便于事后统计"哪一棒最常失败"）。
REASON_NO_CHALLENGE = "no-challenge"
REASON_PROFILE_EXPIRED = "profile-expired"
REASON_IP_BLOCKED = "ip-blocked"
REASON_TOO_SLOW = "too-slow"
REASON_NAVIGATION = "navigation-error"
REASON_UNSOLVABLE = "challenge-unsolvable"


def classify_failure(code: str, message: str = "") -> str:
    """把后端失败翻译成**可行动**的原因码。

    为什么要分类：`unsolved` 一个词里混着"这个站根本没盾"（不该再试）、
    "profile 过期了"（该换后端并告警）、"IP 被提分"（换后端没用，只能换出口）。
    不分类就只能整体重试，也就是烧求解。
    """
    low = (message or "").lower()
    if "not challenged" in low or "没有 cloudflare 挑战" in low:
        return REASON_NO_CHALLENGE
    if "rejected" in low or "profile" in low:
        return REASON_PROFILE_EXPIRED
    if "blocked" in low or "封禁" in low:
        return REASON_IP_BLOCKED
    if code == "timeout":
        return REASON_TOO_SLOW
    if code == "transport":
        return REASON_NAVIGATION
    return REASON_UNSOLVABLE


#: 哪些原因码**不值得**换后端再试（换了也一样，只会多烧一次求解）。
NO_ESCALATION_REASONS = frozenset({REASON_NO_CHALLENGE, REASON_IP_BLOCKED})


def should_escalate(reason: str) -> bool:
    """该不该换下一个后端。"""
    return reason not in NO_ESCALATION_REASONS


#: `prefer` → 需要**跳过**的后端名。
#: 之前 `prefer` 只是个提示字段，语义名不副实（叫 prefer 却不影响选谁）。
#: 现在真的跳：纯协议优先时不要先花 45s 试 pydoll/flaresolverr；
#: 浏览器优先时不要先用纯协议去撞（turnstile 这类纯协议根本拿不到 token）。
_PREFER_SKIPS = {
    "pure": frozenset({"pydoll", "flaresolverr"}),
    "browser": frozenset({"cfsolve"}),
}


def skip_backends(prefer: str) -> frozenset[str]:
    """按 `prefer` 返回本次请求应当跳过的后端名集合（空集=不跳）。"""
    return _PREFER_SKIPS.get((prefer or "").strip().lower(), frozenset())
