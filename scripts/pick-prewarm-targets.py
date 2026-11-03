"""从候选域名清单自动筛出 prewarm 目标（批量 /detect → 分类 → 生成名单）。

解决的问题：prewarm 的目标域必须是「当前 persona 会弹挑战、且我们的回落链能解」的域。
拿无盾站或不支持的站当 prewarm 目标只会白烧超时（glassdoor 实测：60s 超时、renewed=0）。

这个脚本把「选目标」自动化：

  1. 读入候选域名清单（一行一个，`#` 注释，支持 `https://` 前缀）
  2. 逐个调运行中服务的 `GET /detect?url=...`（复用服务端的守卫/缓存/超时控制）
  3. 按服务的 `recommend()` 结果分类：
       · 入选 prewarm —— 有挑战且可解：
           · managed / interactive / embedded / non-interactive（route=browser）
           · jsd（route=pure_protocol_may_work，纯协议可解）
       · 排除，并给出原因：
           · 无防护（route=direct）——prewarm 无意义
           · CF 在场但无挑战标记（route=direct_unless_blocked）——先直接抓即可
           · 非 CF 防护（route=unsupported_or_browser，如 DataDome）——本仓库不支持
           · Turnstile token（route=browser_token）——一次性 token，无 cookie 可保温
  4. 输出：`--out` 名单文件（`--prewarm-file` 直接可吃）+ 人类可读报告（含排除原因）

探测失败（网络/超时/5xx）的域单独归为一类，不计入名单——宁可漏选，不能选错。

用法：
  # 干跑（不发请求，只解析清单）
  python scripts/pick-prewarm-targets.py --solver http://127.0.0.1:8878 \
      --token-file .env --input hosts.txt --dry-run

  # 真实探测
  python scripts/pick-prewarm-targets.py --solver http://127.0.0.1:8878 \
      --token-file .env --input hosts.txt --out prewarm-targets.txt --timeout 15
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover - 非 CPython 无此方法
    pass

#: route → (是否入选, 人话原因)。与 solver/detect.py 的 recommend() 输出对齐。
ROUTE_VERDICT: dict[str, tuple[bool, str]] = {
    "pure_protocol_may_work": (True, "CF jsd：纯协议可解"),
    "browser": (True, "CF 挑战：回落链可解"),
    "browser_token": (False, "Turnstile 一次性 token：无 cookie 可保温，prewarm 无意义"),
    "direct": (False, "无防护：prewarm 无意义"),
    "direct_unless_blocked": (False, "CF 在场但无挑战标记：先直接抓即可，无需保温"),
    "unsupported_or_browser": (False, "非 CF 防护：本仓库求解不覆盖"),
}

#: 归类后的状态标签
PICKED = "picked"
SKIPPED = "skipped"
FAILED = "failed"
DRY_RUN = "dry-run"


def read_candidates(path: str) -> list[str]:
    """读候选清单：一行一个；去 `#` 注释、空行；剥掉 scheme 保留 URL 形态。"""
    candidates: list[str] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            url = line if "://" in line else f"https://{line}"
            host = (urllib.parse.urlparse(url).hostname or "").strip()
            # Hostname 必须像域名：允许字母/数字/点/连字符（容忍 IDN 前缀 xn--）。
            if not host or " " in host or not all(c.isalnum() or c in ".-" for c in host):
                continue
            if host in seen:
                continue
            seen.add(host)
            candidates.append(url)
    return candidates


def load_token(token_file: str) -> str:
    try:
        for line in open(token_file, encoding="utf-8"):
            if line.startswith("CF_SOLVE_TOKEN="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError as error:
        raise ValueError(f"token 文件不存在：{token_file}（先启动一次服务让它自动生成）") from error
    raise ValueError(f"{token_file} 里没有 CF_SOLVE_TOKEN= 行")


def detect_one(
    solver: str, url: str, token: str, timeout: int
) -> tuple[str, dict | None, str | None]:
    """调一次 /detect。返回 (状态, 结果或 None, 错误说明)。"""
    endpoint = solver.rstrip("/") + "/detect?url=" + urllib.parse.quote(url, safe="")
    request = urllib.request.Request(endpoint, headers={"Authorization": f"Bearer {token}"})
    try:
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        elapsed = time.perf_counter() - started
        return "ok", payload, f"{elapsed:.2f}s"
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:120]
        return "http_error", None, f"HTTP {error.code}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return "network_error", None, f"{type(error).__name__}: {error}"[:120]


def classify(result: dict) -> tuple[str, str]:
    """按 recommendation 的 route 归类。返回 (PICKED/SKIPPED, 原因)。"""
    recommendation = result.get("recommendation")
    if not recommendation:
        return SKIPPED, "服务未给出建议（响应缺 recommendation 字段）"
    route = recommendation.get("route", "")
    picked, reason = ROUTE_VERDICT.get(route, (False, f"未知 route：{route}（服务端版本不匹配？）"))
    return (PICKED if picked else SKIPPED), reason


def run(args: argparse.Namespace) -> int:
    token = load_token(args.token_file)
    candidates = read_candidates(args.input)
    print(f"候选 {len(candidates)} 个域（来自 {args.input}）")
    if args.dry_run:
        for url in candidates:
            print(f"  will detect: {url}")
        print("dry-run：未发任何请求")
        return 0

    results: list[dict] = []
    counts = {PICKED: 0, SKIPPED: 0, FAILED: 0}
    for index, url in enumerate(candidates, 1):
        status, payload, detail = detect_one(args.solver, url, token, args.timeout)
        if status != "ok":
            counts[FAILED] += 1
            results.append({"url": url, "state": FAILED, "reason": detail})
            print(f"[{index}/{len(candidates)}] {url}  ⚠ {FAILED}: {detail}")
            continue
        state, reason = classify(payload)
        counts[state] += 1
        providers = ", ".join(p["name"] for p in payload.get("providers", [])) or "无"
        results.append(
            {
                "url": url,
                "state": state,
                "reason": reason,
                "route": (payload.get("recommendation") or {}).get("route", ""),
                "status": payload.get("status"),
                "providers": providers,
                "detect_elapsed": detail,
            }
        )
        mark = "✓ 入选" if state == PICKED else "✗ 排除"
        print(f"[{index}/{len(candidates)}] {url}  {mark}: {reason}（{providers}）")

    picked = [r for r in results if r["state"] == PICKED]

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            for item in picked:
                handle.write(item["url"] + "\n")
        print(f"\n名单已写入 {args.out}（{len(picked)} 个域，--prewarm-file 可直接吃）")

    print(
        f"\n=== 汇总：入选 {counts[PICKED]} / 排除 {counts[SKIPPED]} / 失败 {counts[FAILED]}"
        f"（共 {len(candidates)}）==="
    )
    for item in results:
        if item["state"] != PICKED:
            print(f"  ✗ {item['url']}: {item['reason']}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"counts": counts, "results": results}, handle, ensure_ascii=False, indent=1)
        print(f"结构化结果已写入 {args.json_out}")

    if counts[PICKED] == 0 and not args.dry_run:
        print()
        print("⚠ 没有筛出任何 prewarm 目标——检查候选清单是否为受 CF 保护且可解的域")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--solver", default="http://127.0.0.1:8878", help="运行中的求解服务地址")
    parser.add_argument("--token-file", default=".env", help="含 CF_SOLVE_TOKEN= 的文件")
    parser.add_argument("--input", required=True, help="候选域名清单（一行一个，# 注释）")
    parser.add_argument("--out", help="prewarm 名单输出文件（--prewarm-file 格式）")
    parser.add_argument("--json-out", help="结构化结果输出（json）")
    parser.add_argument("--timeout", type=int, default=15, help="单站 /detect 超时秒数（默认 15）")
    parser.add_argument("--dry-run", action="store_true", help="只解析清单打印计划，不发请求")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
