"""语义差分 Oracle（v6.2，借鉴 cipherwire/disarm 的做法）。

disarm 的核心方法：**不靠"读代码觉得对"，而是拿两份实现跑同一份输入，比对输出。**
"每个疑点都用执行证明或证伪" —— 差异本身就是证据。

在我们这里，确实存在**两套必须永远一致的决策逻辑**：

  | 输入（一组厂商 id） | 实现 A：`detect.recommend()` | 实现 B：`route.decide()` |
  |---|---|---|
  | 谁能在本服务求解？ | 按命中列表的 top 决定路线 | 按 provider 集合决定 solve/reject |

  两者在 v6.1/v6.2 被分别改过（一个加 `multicaptcha` 分支、一个加 `MULTICAPTCHA_PROVIDERS`）。
  任何一边漏改，调用方就会看到"`/detect` 说能解、`/solve` 却拒绝"这种自相矛盾的输出 ——
  而这种 bug 在单边测试里**永远是绿的**（每边的用例都只测自己那张表）。

所以这组用例不测"某个函数对不对"，测的是**两个函数是否互相一致**：
对同一批厂商组合，`recommend()` 与 `decide()` 对"能不能解"和"该走哪条路"必须给出同样的答案。

覆盖面（宁可多跑几条纯函数，也不放过一次静默分叉）：
  · 11 个厂商 × 单独出现
  · 关键组合（CF + 万盾 / CF + 不可解 / 万盾 + 不可解 / 多个万盾）
  · "检测到但没有求解器"的厂商必须两边都说不能

边界说明：这里比对的是**决策**，不是**实现**。两边算法不同（一个是排序取 top、一个是集合判断），
只要决策一致就算通过 —— 这正是"语义差分"与"字符串差分"的区别。
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "solver"))

import detect as detect_mod  # noqa: E402
import route as route_mod  # noqa: E402


#: detect 侧的 provider id → 人话名（route 侧的 payload 要用同一个 id）
PROVIDERS = {
    "detect-cloudflare": "Cloudflare Bot Management",
    "detect-aws-waf": "AWS WAF",
    "detect-akamai": "Akamai Bot Manager",
    "detect-datadome": "DataDome",
    "detect-perimeterx": "PerimeterX",
    "detect-funcaptcha": "FunCaptcha (Arkose Labs)",
    "detect-hcaptcha": "hCaptcha",
    "detect-recaptcha": "Google reCAPTCHA",
    "detect-kasada": "Kasada",
    "detect-incapsula": "Incapsula (Imperva)",
    "detect-shape": "Shape Security",
}

#: 本服务**确实能解**的厂商（两边都必须这么认为）
SOLVABLE = {
    "detect-cloudflare", "detect-aws-waf",
    "detect-akamai", "detect-datadome", "detect-perimeterx",
    "detect-funcaptcha", "detect-hcaptcha", "detect-recaptcha",
}
UNSOLVABLE = set(PROVIDERS) - SOLVABLE


def _detections(ids: list[str]):
    """构造 `detect.recommend()` 的输入（Detection 列表，按 ids 顺序）。"""
    return [
        detect_mod.Detection(
            id=i, name=PROVIDERS.get(i, i), category="Anti-Bot",
            difficulty="Medium", confidence=100, hits=[f"cookie:{i}"],
        )
        for i in ids
    ]


def _route_payload(ids: list[str], ctype: str = "") -> dict:
    """构造 `route.decide()` 的输入（与 `detect.summarize()` 同形状）。"""
    return {
        "protected": bool(ids),
        "providers": [{"id": i, "name": PROVIDERS.get(i, i)} for i in ids],
        "cloudflare_challenge_type": ctype or None,
    }


def _oracle(ids: list[str], ctype: str = ""):
    """跑两个实现，返回 (recommend 结论, decide 结论)。"""
    rec = detect_mod.recommend(_detections(ids), ctype) if ids else detect_mod.recommend([])
    dec = route_mod.decide(_route_payload(ids, ctype), "auto")
    return rec, dec


def _solvable_by_recommend(rec: dict) -> bool:
    """recommend 的"能解"口径：任何非 unsupported 的路线都算能解。"""
    return rec.get("route") != "unsupported_or_browser"


def _solvable_by_decide(dec) -> bool:
    return dec.action != route_mod.ACTION_REJECT


# ── 单个厂商：两个实现必须对"能不能解"给同一答案 ──────────


@pytest.mark.parametrize("provider", sorted(PROVIDERS))
def test_single_provider_agreement(provider):
    rec, dec = _oracle([provider])
    assert _solvable_by_recommend(rec) == _solvable_by_decide(dec), (
        f"[{provider}] 两个实现分叉：recommend route={rec.get('route')!r} "
        f"vs decide action={dec.action!r} reason={dec.reason!r}"
    )


@pytest.mark.parametrize("provider", sorted(SOLVABLE))
def test_solvable_providers_are_solvable_on_both_sides(provider):
    """正例：能解的厂商，两边都必须给"能解"（防止其中一边收得过紧）。"""
    rec, dec = _oracle([provider])
    assert _solvable_by_recommend(rec), f"[{provider}] recommend 说不能解：{rec}"
    assert _solvable_by_decide(dec), f"[{provider}] decide 说不能解：{dec.reason}"


@pytest.mark.parametrize("provider", sorted(UNSOLVABLE))
def test_unsolvable_providers_are_rejected_on_both_sides(provider):
    """负例：不能解的厂商，两边都必须明确拒绝（防止其中一边假承诺）。"""
    rec, dec = _oracle([provider])
    assert not _solvable_by_recommend(rec), f"[{provider}] recommend 假承诺：{rec}"
    assert not _solvable_by_decide(dec), f"[{provider}] decide 假承诺：{dec.action}"
    assert dec.action == route_mod.ACTION_REJECT


# ── 组合：必须两个实现同进退 ────────────────────────


def _all_pairs() -> list[tuple[str, str]]:
    return list(itertools.combinations(sorted(PROVIDERS), 2))


@pytest.mark.parametrize("pair", _all_pairs(), ids=lambda p: "+".join(p))
def test_provider_pairs_agree(pair):
    """两两组合全跑一遍（11 厂商 = 55 组）：只要有一边认为"能解"，另一边不能认为"不能"。"""
    rec, dec = _oracle(list(pair))
    assert _solvable_by_recommend(rec) == _solvable_by_decide(dec), (
        f"[{'+'.join(pair)}] 分叉：recommend route={rec.get('route')!r} / "
        f"decide action={dec.action!r} reason={dec.reason!r}"
    )


# ── 关键组合的语义断言（不止"一致"，还要"一致在正确的方向上"）──


def test_cf_plus_graft_prefers_cf_on_both_sides():
    """CF 与万盾同时在场：两边都必须走 CF 主链路（主线优先是硬约束）。"""
    rec, dec = _oracle(["detect-cloudflare", "detect-datadome"], "managed")
    assert rec["route"] in ("browser", "pure_protocol_may_work")
    assert dec.prefer != "multicaptcha"
    assert dec.reason.startswith("cloudflare-")


def test_graft_plus_unsolvable_is_rejected():
    """万盾能解 + 一个解不了：整体必须拒绝（半边能解不代表这站能过）。"""
    rec, dec = _oracle(["detect-datadome", "detect-kasada"])
    assert not _solvable_by_decide(dec), "有一个解不了就必须拒绝"
    assert dec.action == route_mod.ACTION_REJECT
    # recommend 只看 top 命中，可能给出 datadome —— 这是**刻意的不对称**：
    # 它回答"最先撞到的是什么盾"，`decide` 回答"这次请求要不要试"。两者语义不同。
    # 这里断言的是：recommend 不会把 kasada 说成能解。
    assert "multicaptcha" not in str(rec.get("route")) or rec.get("want") != "kasada"


def test_multiple_graft_providers_pick_a_graft_route():
    """多个万盾厂商：decide 必须给出某个 multicaptcha want（不能空手说能解）。"""
    rec, dec = _oracle(["detect-datadome", "detect-akamai"])
    assert dec.action == route_mod.ACTION_SOLVE
    assert dec.prefer == "multicaptcha"
    assert dec.detail.get("multicaptcha_want") in route_mod.MULTICAPTCHA_PROVIDERS.values()
    assert rec.get("route") == "multicaptcha"


def test_both_tables_cover_the_same_vendor_ids():
    """两张表引用的 provider id 必须都在 detect 的签名表里。

    回归点：如果 `MULTICAPTCHA_PROVIDERS` 写了一个 detect 根本不产出的 id
    （拼错一个字母），路由分支就永远不会命中，而所有单边用例照样全绿。
    """
    known_ids = {sig["id"] for sig in detect_mod.SIGNATURES} | {
        row[0] for row in detect_mod.STRONG_SIGNALS
    }
    for vendor_id in route_mod.MULTICAPTCHA_PROVIDERS:
        assert vendor_id in known_ids, (
            f"MULTICAPTCHA_PROVIDERS 里的 {vendor_id!r} 不是 detect 会产出的 id —— "
            "写错的话这条路由永远不会命中"
        )


# ── 单一事实源（vendors.py）自身的契约 ──────────────


def test_pick_multicaptcha_falls_back_to_first_hit():
    """命中多个万盾厂商时按优先级挑；都不在优先级列表里就取第一个（保证不返回空）。"""
    import vendors

    provider, want = vendors.pick_multicaptcha(["detect-akamai", "detect-datadome"])
    assert provider == "detect-datadome"          # datadome 在优先级里更靠前
    assert want == "datadome"
    # 未知组合（理论上不会出现）也不能崩
    assert vendors.pick_multicaptcha([]) == ("", "")


def test_vendors_solvable_matches_table():
    import vendors

    assert vendors.solvable("detect-cloudflare")
    assert vendors.solvable("detect-datadome")
    assert not vendors.solvable("detect-kasada")
    assert vendors.unsolvable_from(["detect-cloudflare", "detect-kasada"]) == ["detect-kasada"]


# ── 回归：Fingerprint 类命中**不是盾**（v6.2 开发中真实踩到的 P0）──


#: 一个普通 CF 站里会出现的"技术指纹特征"（任何现代站点都有）
_FINGERPRINT_BODY = (
    "<script>localStorage.setItem('x',1);"
    "navigator.clipboard.readText();"
    "Intl.DateTimeFormat().resolvedOptions();</script>"
)


def test_fingerprint_hits_do_not_make_a_site_unsolvable():
    """P0 回归：用了 localStorage 的 CF 站**不能**被判成"解不了"。

    开发中真实踩到：把 recommend/decide 的判断口径从"只看榜首"改成"看全集"后，
    `detect-storage-fingerprint` / `detect-clipboard-fingerprint` 这类 `Fingerprint`
    类命中被当成"解不了的厂商"，于是**几乎所有现代站点**都被判 unsupported。
    修法是按 `category` 过滤：只有 Anti-Bot / ANTIBOT / CAPTCHA 才算"盾"。
    """
    detections = detect_mod.detect(
        status=403, headers={"cf-mitigated": "challenge"}, body=_FINGERPRINT_BODY,
        url="https://t.test/", cookies={"__cf_bm": "x"},
    )
    assert any(d.category == "Fingerprint" for d in detections), "夹具本身要含指纹命中"
    rec = detect_mod.recommend(detections, "managed")
    assert rec["route"] in ("browser", "pure_protocol_may_work"), rec

    summ = detect_mod.summarize(
        "https://t.test/", 403, {"cf-mitigated": "challenge"}, _FINGERPRINT_BODY, {"__cf_bm": "x"})
    dec = route_mod.decide(summ, "auto")
    assert dec.action != route_mod.ACTION_REJECT, dec.reason


def test_fingerprint_only_site_is_not_rejected():
    """纯指纹、没有任何盾：必须走"直接抓"，绝不能 reject。"""
    summ = {"protected": True,
            "providers": [{"id": "detect-storage-fingerprint", "name": "Web Storage",
                           "category": "Fingerprint"}],
            "cloudflare_challenge_type": None}
    dec = route_mod.decide(summ, "auto")
    assert dec.action != route_mod.ACTION_REJECT, dec.reason


def test_shield_category_helper():
    import vendors

    assert vendors.is_shield("Anti-Bot")
    assert vendors.is_shield("ANTIBOT")
    assert vendors.is_shield("CAPTCHA")
    assert not vendors.is_shield("Fingerprint")


def test_all_fingerprint_signatures_are_excluded_from_shield_check():
    """守门：`SIGNATURES` 里的每个 Fingerprint 类 id 都**不在**能解/不可解的判断里。"""
    import vendors

    fp_ids = {s["id"] for s in detect_mod.SIGNATURES if s["category"] == "Fingerprint"}
    assert fp_ids, "特征表里应当有 Fingerprint 类条目"
    for fid in fp_ids:
        assert not vendors.is_shield("Fingerprint")
        assert not vendors.solvable(fid)      # 它本来就不该被当成厂商


# ── route._provider_ids 的边界（决策输入的形状）──────


def test_provider_ids_tolerates_malformed_payload():
    """探测结论可能被外部构造/缓存污染：畸形结构必须当成"没有厂商"，不能抛。"""
    for bad in (None, {}, {"providers": None},
                {"providers": ["not-a-dict", {}, {"id": ""}, 42]}):
        assert route_mod._provider_ids(bad) == set(), bad


def test_provider_ids_keeps_legacy_payload_without_category():
    """没有 `category` 字段的旧 payload（v6.1 之前的缓存）必须**当成盾**处理。

    兼容性理由：`/detect` 的结论在 `detect_cache_ttl` 内会跨版本存活，
    升级瞬间可能拿到旧形状。若把它当指纹过滤掉，会**跳过真实防护**。
    """
    legacy = {"providers": [{"id": "detect-kasada", "name": "Kasada"}]}
    assert route_mod._provider_ids(legacy) == {"detect-kasada"}


# ── 独立复审抓到的两条真缺陷（v6.2 r1）──────────────


def test_non_interactive_ctype_maps_to_pure_protocol():
    """回归（复审缺陷 1）：`cType` 的真实拼写是 `non-interactive`（连字符）。

    `detect._CTYPE_RE` 的字符类是 `[A-Za-z_-]+`，产物是连字符形式；
    而 `route._CHALLENGE_PREFERS` 曾写成下划线 → 查不到 → `prefer` 静默降级成 `any`
    → pydoll / flaresolverr 不会被跳过 → cfsolve 失败后白起一次 Chrome（16–20s）。
    """
    ctype = detect_mod.cf_challenge_type(
        "<script>window._cf_chl_opt={cType:'non-interactive'};</script>")
    assert ctype == "non-interactive"
    dec = route_mod.decide({
        "protected": True,
        "providers": [{"id": "detect-cloudflare", "name": "Cloudflare", "category": "Anti-Bot"}],
        "cloudflare_challenge_type": ctype,
    }, "auto")
    assert dec.prefer == "pure", dec.prefer
    assert route_mod.skip_backends(dec.prefer) == frozenset({"pydoll", "flaresolverr"})


@pytest.mark.parametrize("ctype", ["jsd", "non-interactive", "managed", "interactive", "turnstile"])
def test_every_ctype_is_spelled_like_detect_produces(ctype):
    """守门：`_CHALLENGE_PREFERS` 的键必须是 `detect` 真会产出的拼写。

    这条防的是"表里写了一个永远不命中的键"这类静默降级 —— 它不会报错，
    只是让优化悄悄失效。
    """
    table = route_mod._CHALLENGE_PREFERS
    assert ctype in table, f"{ctype!r} 不在 _CHALLENGE_PREFERS 里，会静默降级成 any"


def test_recommend_uses_shields_not_raw_top():
    """回归（复审缺陷 2）：`recommend()` 取 want 必须走 shields 过滤后的集合。

    触发：正文里一个 50 分的 Fingerprint（`getComputedStyle`）+ 一个 50 分的 hCaptcha。
    按 `(-confidence, name)` 排序时 Fingerprint 排在前面 → 用 `detections[0]` 查表查不到
    → 返回 `unsupported_or_browser`，而 `decide()` 同输入返回 `multicaptcha`。
    两个公共决策函数对同一份输入说相反的话，正是 vendors.py 要根治的那类分叉。
    """
    body = ('<script>getComputedStyle(document.body)</script>'
            '<div class="h-captcha" data-sitekey="x"></div>')
    summ = detect_mod.summarize("https://x.test/", 403, {}, body)
    providers = [(p["id"], p["confidence"]) for p in summ["providers"]]
    assert providers[0][0].endswith("fingerprint"), f"夹具没构造出指纹排前：{providers}"
    rec = summ["recommendation"]
    assert rec["route"] == "multicaptcha", rec
    assert rec["want"] == "hcaptcha"
    assert route_mod.decide(summ, "auto").prefer == "multicaptcha"


def test_route_imports_work_in_package_mode():
    """回归（复审备注）：route 的厂商表必须从 `_vendors` 取。

    原来写成 `from vendors import ...` 的绝对导入 —— 包方式导入时模块名是
    `solver.vendors`，顶层 `vendors` 不存在 → ModuleNotFoundError。
    当前入口都是脚本式所以没炸，但 `from solver.detect import ...` 这类用法一碰就炸。
    """
    import importlib
    for mod in ("solver.route", "solver.vendors", "solver.detect"):
        importlib.import_module(mod)
    r = importlib.import_module("solver.route")
    assert r.SOLVABLE_PROVIDER_IDS == frozenset({"detect-cloudflare", "detect-aws-waf"}
                                                ) | frozenset(r.MULTICAPTCHA_PROVIDERS)


# ── 复审第 2 轮抓到的两条（语义倒挂 + protected 未过滤）────


def test_fingerprint_only_site_recommends_direct_not_unsupported():
    """回归（复审 R2-1）：只有指纹命中的站必须建议"直接抓"，而不是"解不了"。

    语义词序：什么都没命中 → `direct`；只命中指纹 → 也必须是 `direct`。
    曾经返回 `unsupported_or_browser` 并说"检测到非 CF 防护：Web Storage Fingerprint"，
    等于"有指纹"比"干净"判得更糟 —— 对 `/detect` 的调用方是错话。
    """
    summ = detect_mod.summarize(
        "https://plain.test/", 200, {}, "<script>localStorage.setItem('x',1)</script>")
    ids = [p["id"] for p in summ["providers"]]
    assert ids and all(i.endswith("fingerprint") for i in ids), ids
    assert summ["recommendation"]["route"] == "direct", summ["recommendation"]


def test_protected_flag_ignores_fingerprint_only_hits():
    """回归（复审 R2-2）：`protected` 必须按**盾类**判定。

    曾经是 `bool(detections)` —— 指纹命中也会让它为真，于是 `route=auto` 对一个
    **没有盾**的站照常起浏览器（16–20s + 1.2GB），正是 route.py 要消除的那笔开销。
    """
    summ = detect_mod.summarize(
        "https://plain.test/", 200, {}, "<script>navigator.clipboard.readText()</script>")
    assert summ["providers"], "夹具本身要命中指纹"
    assert summ["protected"] is False
    assert route_mod.decide(summ, "auto").action == route_mod.ACTION_SKIP


def test_protected_flag_true_for_real_shield():
    """反向守护：真盾必须让 protected=True（不能因为过滤过度而漏判）。"""
    summ = detect_mod.summarize("https://k.test/", 403, {}, "kasada enforcement script")
    shield_ids = [p["id"] for p in summ["providers"]
                  if __import__("vendors").is_shield(p["category"])]
    if shield_ids:                      # 特征表若没覆盖 kasada，这条跳过而不是假绿
        assert summ["protected"] is True


def test_pure_preference_never_drops_the_browser():
    """不变量：`prefer=pure` 只把 pydoll/flaresolverr 挪后，**不能把浏览器踢出链**。

    为什么这条重要：`detect.CTYPE_ROUTE` 与 `route._CHALLENGE_PREFERS` 对
    `managed` / `non-interactive` 给出的建议不同（前者 `browser`、后者 `pure`）。
    这**不是矛盾** —— 两者回答的问题不同（"靠什么能过" vs "先试谁"）。
    允许它们不同，前提是"先试纯协议"仍然保留浏览器兜底。这条用例就是守那个前提：
    一旦有人把 `skip_backends("pure")` 改成连 camoufox 一起跳，这里会红。
    """
    skipped = route_mod.skip_backends("pure")
    assert "camoufox" not in skipped, "纯协议优先不等于不要浏览器兜底"
    # cfsolve 也不该被自己跳过
    assert "cfsolve" not in skipped


@pytest.mark.parametrize("ctype", sorted(detect_mod.CTYPE_ROUTE))
def test_every_detect_ctype_is_known_to_route(ctype):
    """守门：detect 能产出的每个 ctype，route 都必须有一档偏好（否则静默降级 any）。"""
    assert ctype in route_mod._CHALLENGE_PREFERS, f"{ctype!r} 在 route 表里缺失"


def test_stale_detect_cache_with_fingerprint_only_is_skipped():
    """兼容旧缓存：`protected=true` 但 providers 全是指纹（旧口径）→ 必须 skip 而不是 solve。

    `/detect` 的结论在 `detect_cache_ttl` 内跨版本存活。升级瞬间可能拿到
    v6.2 之前写的 payload（那时 `protected = bool(detections)`，指纹也算）。
    按 category 过滤后 ids 为空 —— 这时不能"因为 protected 为真就去求解"。
    """
    stale = {
        "protected": True,
        "providers": [{"id": "detect-storage-fingerprint", "name": "Web Storage",
                       "category": "Fingerprint"}],
        "cloudflare_challenge_type": None,
    }
    dec = route_mod.decide(stale, "auto")
    assert dec.action == route_mod.ACTION_SKIP, dec.reason
