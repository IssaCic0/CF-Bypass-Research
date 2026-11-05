"""厂商能力表 —— 「这个盾本服务能不能解」的**单一事实源**。

为什么必须单独一个模块（v6.2，来自语义差分 Oracle 的实测发现）：
  · 这张知识原来**存在两份**：`detect.recommend()` 里一张 if/else 映射，
    `route.decide()` 里另一组 frozenset/dict。
  · 两份在 v6.1/v6.2 被分别改过，于是出现了真实分叉（`tests/test_differential_oracle.py`
    跑 55 组厂商组合当场抓到）：
      - `detect-cloudflare + detect-kasada`：recommend 说"用浏览器能解"，
        decide 说"拒绝：kasada 解不了" —— **假承诺**。
      - `detect-kasada + detect-perimeterx`：decide 先命中 multicaptcha 分支、
        没走 unsupported 检查，于是对一个**确实过不去**的站说了"能解"。
  · 这类缺陷在单边测试里永远是绿的（每边只测自己那张表），只有差分才看得见。

  **根治不是"记得两边一起改"，而是让它只有一份。**

三个集合的语义：
  · `MAIN_CHAIN`      主链路能解（Cloudflare / AWS WAF，走 Solver 或后端链）
  · `MULTICAPTCHA`    万盾嫁接层能解（captchas/ 子包，按 vendor → want 分发）
  · 其余一律**不能解** —— 不做"也许能"的模糊地带。
"""

from __future__ import annotations

#: 主链路能解的厂商 id（与 detect.py 的签名 id 对齐）
MAIN_CHAIN_PROVIDERS: frozenset[str] = frozenset({
    "detect-cloudflare",
    "detect-aws-waf",
})

#: 万盾嫁接层（captchas/）能解的厂商 → 推荐 want。
#: 值必须存在于 `solver/multicaptcha.py` 的 `CAPTCHA_TYPES` 里（有测试守）。
MULTICAPTCHA_PROVIDERS: dict[str, str] = {
    "detect-akamai": "akamai",
    "detect-perimeterx": "perimeterx",
    "detect-datadome": "datadome",
    "detect-funcaptcha": "arkose",
    "detect-recaptcha": "recaptcha_x",
    "detect-hcaptcha": "hcaptcha",
}

#: 自动分发时的优先级（同页命中多个万盾厂商时先试谁）。
#: 顺序依据：Arkose/PerimeterX 的闸门最"硬"（拦截式），DataDome/Akamai 多为静默打分，
#: 先解硬的能尽早暴露"其实过不去"，避免在软盾上白花时间。
MULTICAPTCHA_PRIORITY: tuple[str, ...] = (
    "detect-funcaptcha", "detect-perimeterx", "detect-datadome",
    "detect-akamai", "detect-hcaptcha", "detect-recaptcha",
)

#: 只有这些**类别**的命中才代表"对面有盾"。`Fingerprint` 类（localStorage / clipboard /
#: Intl.DateTimeFormat …）是**技术指纹特征**，任何现代站点都会命中 —— 把它当成"解不了的厂商"
#: 会让几乎所有站被误判（v6.2 开发中真的踩到了：一个普通 CF 站只因用了 localStorage
#: 就被判 `unsupported_or_browser`）。这条过滤是本模块最容易被忽略的约束。
SHIELD_CATEGORIES: frozenset[str] = frozenset({"Anti-Bot", "ANTIBOT", "CAPTCHA"})


def is_shield(category: str) -> bool:
    """这个类别的命中是不是"真的有盾"。"""
    return str(category) in SHIELD_CATEGORIES


#: 全部**能解**的厂商（主链路 ∪ 嫁接层）。用于"有没有解不了的"这一判断。
SOLVABLE_PROVIDERS: frozenset[str] = MAIN_CHAIN_PROVIDERS | frozenset(MULTICAPTCHA_PROVIDERS)


def solvable(provider_id: str) -> bool:
    return provider_id in SOLVABLE_PROVIDERS


def unsolvable_from(ids) -> list[str]:
    """从一组厂商 id 里挑出**本服务解不了**的（排序后返回，保证输出确定）。"""
    return sorted({str(i) for i in ids if not solvable(str(i))})


def pick_multicaptcha(ids) -> tuple[str, str]:
    """在命中的万盾厂商里挑一个，返回 (provider_id, want)。没有命中时返回 ("", "")。"""
    hit = [i for i in ids if i in MULTICAPTCHA_PROVIDERS]
    if not hit:
        return "", ""
    for preferred in MULTICAPTCHA_PRIORITY:
        if preferred in hit:
            return preferred, MULTICAPTCHA_PROVIDERS[preferred]
    return hit[0], MULTICAPTCHA_PROVIDERS[hit[0]]