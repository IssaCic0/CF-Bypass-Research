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

#: 本服务**能解**的厂商（其余一律明确拒绝，而不是白等 45s）。
#: Cloudflare：cf_clearance / turnstile / jsd / managed 全部支持；
#: AWS WAF：支持取 `aws-waf-token`（`want=awswaf`）。
SOLVABLE_PROVIDER_IDS = frozenset({"detect-cloudflare", "detect-aws-waf"})

#: CF 挑战类型 → 偏好后端。
#: 一次性 token 类的挑战必须浏览器内取（纯协议拿不到 widget 的 token）。
_CHALLENGE_PREFERS = {
    "turnstile": "browser",
    "recaptcha": "browser",
    "interactive": "browser",
    "embedded": "browser",
    "managed": "pure",
    "jsd": "pure",
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
    if not isinstance(detect, dict):
        return set()
    out = set()
    for p in detect.get("providers") or []:
        if isinstance(p, dict) and p.get("id"):
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
    unsupported = sorted(ids - SOLVABLE_PROVIDER_IDS)
    if unsupported:
        names = sorted({str(p.get("name") or p.get("id")) for p in (detect.get("providers") or [])
                        if isinstance(p, dict) and str(p.get("id")) in set(unsupported)})
        return RouteDecision(
            mode="auto", action=ACTION_REJECT, reason="unsupported-provider",
            hint=("该站使用本服务不支持的防护（" + "、".join(names) + "）。"
                  "换盾方案比换后端有效：这里既没有对应的求解器，硬试只会白等一个完整超时。"),
            detail={"unsupported": unsupported, "providers": names},
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
