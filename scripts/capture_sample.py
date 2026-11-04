"""把"手工抓 CF 挑战样本"变成可复现的一步（v5.0.1 E2）。

**补的是什么洞**：本仓库下游的证据链很严（`evidence/*.json`、`findings.json` 逐条指向文件、
`policy.md` 写死门禁），但最上游那一步一直是**手工的**：

    README 原文：`curl -L -A "<UA>" "https://<target>/cdn-cgi/challenge-platform/scripts/jsd/main.js"`
    再从挑战页 HTML 里手工抠出 window._cf_chl_opt = {...}; 存为 chl_opt.js

手工步骤意味着：同一个人隔天再做一次，拿到的 UA、跳转结果、抠取方式都可能不同；
别人复现你的 `docs/challenges/` 结论时无从对齐。这个脚本把那一步变成
"一条命令 + 一份 manifest（含每个产物的 sha256）"。

**三条硬约束（对应 policy.md）**：
  1. `--dry-run` 是默认行为：不传 `--real` 只打印"会去抓哪些 URL、共几个请求"，零网络；
  2. 请求预算：默认上限 5，超过 20 直接拒绝（除非显式 `--allow-over-budget`）——
     真机预算属于不可再生资源，脚本级就要拦住"顺手多抓几个"；
  3. **累计失败 ≥5 立即全停**（与 policy.md §2 的真机实验纪律一致）。

用法：
    python scripts/capture_sample.py --url https://target/ --out samples/20261104-target          # 试运行
    python scripts/capture_sample.py --url https://target/ --out samples/x --real                  # 真抓
    python scripts/capture_sample.py --url https://target/ --out samples/x --real --ua "Mozilla/..." --max-requests 3

产物（目录结构固定，便于 diff 与复现）：
    <out>/manifest.json      元数据：URL/状态码/头/UA/时间/大小/sha256/我们的 detect 结论
    <out>/challenge_page.html 原始挑战页
    <out>/chl_opt.js        从 HTML 里抠出的 window._cf_chl_opt = {...}（可能不存在）
    <out>/main_0.js          第一个挑战脚本（jsd main.js / challenge-platform）
    <out>/main_1.js          …以此类推
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parent.parent
# 放进 path 的是**仓库根**（不是 solver/）：这样 `from solver.detect import ...` 才成立。
# 之前放的是 solver/ 且回退写 `from solver.detect import ...` —— 两条路在同一次运行里
# 只有一条能成立，回退永远是死的（复审 M4）。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 默认 UA：与 solver 的探测口径一致（同一份"我是什么浏览器"的说法，别两处不同）
DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0) Gecko/20100101 Firefox/156.0"
#: 默认请求预算。1 次挑战页 + 最多 4 个脚本。
DEFAULT_MAX_REQUESTS = 5
#: 硬上限：超过需要显式 --allow-over-budget（真机预算不可再生，脚本级设闸）
HARD_BUDGET = 20
#: 累计失败到这个数就全停（policy.md §2）
MAX_FAILURES = 5
#: 单次请求超时
DEFAULT_TIMEOUT = 20.0

#: 只保留这些响应头进 manifest（其余可能含 cookie / 追踪信息）
KEEP_HEADERS = (
    "content-type", "content-length", "cache-control", "cf-mitigated", "server",
    "location", "cf-ray", "set-cookie-present",
)

_CHL_OPT_RE = re.compile(
    r"window\._cf_chl_opt\s*=\s*\{(?:[^{}]|\{[^{}]*\})*\}\s*;?", re.S
)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)
#: 值得抓的挑战脚本特征（其余第三方脚本不抓，省预算）
_CHALLENGE_SCRIPT_HINTS = ("/cdn-cgi/challenge-platform/", "/cdn-cgi/chl/", "jsd/main.js")


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int = 0
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    error: str = ""
    elapsed: float = 0.0


def extract_chl_opt(html: str) -> str:
    """从挑战页 HTML 抠出 `window._cf_chl_opt = {...};`；找不到返回空串。"""
    match = _CHL_OPT_RE.search(html or "")
    return match.group(0) if match else ""


def find_challenge_scripts(html: str, base_url: str) -> list[str]:
    """从 HTML 里挑出**挑战相关**的脚本地址（绝对化、去重、保序）。

    只挑 `_CHALLENGE_SCRIPT_HINTS` 命中的：一次采集的预算是有限的，
    抓一堆第三方统计脚本既浪费又会让 manifest 变得没法对比。
    """
    out: list[str] = []
    for src in _SCRIPT_SRC_RE.findall(html or ""):
        absolute = urljoin(base_url, src.strip())
        lowered = absolute.lower()
        if not any(hint in lowered for hint in _CHALLENGE_SCRIPT_HINTS):
            continue
        if absolute not in out:
            out.append(absolute)
    return out


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_headers(raw) -> dict:
    """只留白名单头；`Set-Cookie` 只记"有没有"，**绝不记值**（policy.md §4）。"""
    picked: dict = {}
    for key, value in (raw or {}).items():
        lowered = key.lower()
        if lowered == "set-cookie":
            picked["set-cookie-present"] = "yes"
            continue
        if lowered in KEEP_HEADERS:
            picked[lowered] = str(value)[:200]
    return picked


def fetch(url: str, ua: str, timeout: float) -> FetchResult:
    started = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return FetchResult(
                url=url, ok=True, status=resp.status, body=body,
                headers=_safe_headers(dict(resp.headers)),
                elapsed=round(time.monotonic() - started, 3),
            )
    except urllib.error.HTTPError as e:
        # 4xx/5xx 也要把 body 留下来：挑战页本身就是 403/503
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            body = b""
        return FetchResult(
            url=url, ok=True, status=e.code, body=body,
            headers=_safe_headers(dict(e.headers or {})),
            elapsed=round(time.monotonic() - started, 3),
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {str(e)[:160]}",
                           elapsed=round(time.monotonic() - started, 3))


def detect_summary(page: FetchResult) -> dict:
    """顺手给出"这是什么盾"的结论（复用 solver/detect.py，不重写一套）。"""
    from solver.detect import cf_challenge_type, detect, recommend

    text = page.body.decode("utf-8", "replace")
    found = detect(status=page.status, headers=page.headers, body=text, url=page.url)
    ctype = cf_challenge_type(text, page.headers, page.url)
    rec = recommend(found, ctype)
    return {
        "providers": [{"id": d.id, "name": d.name, "confidence": d.confidence} for d in found],
        "cloudflare_challenge_type": ctype,
        "recommended_route": rec.get("route"),
        "reason": rec.get("reason"),
    }


def capture(url: str, out_dir: Path, *, ua: str, timeout: float, max_requests: int,
            max_scripts: int = 4) -> dict:
    """真抓一轮并落盘。返回 manifest（同时写进 out_dir/manifest.json）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict] = []
    failures = 0
    requests_made = 0

    def budget_left() -> bool:
        return requests_made < max_requests

    page = None
    if budget_left():
        requests_made += 1
        page = fetch(url, ua, timeout)
        if not page.ok:
            failures += 1
        else:
            out_dir.joinpath("challenge_page.html").write_bytes(page.body)
            artifacts.append({
                "file": "challenge_page.html", "url": url, "status": page.status,
                "bytes": len(page.body), "sha256": _sha256(page.body), "elapsed": page.elapsed,
            })
    if page is None or not page.ok:
        manifest = {
            "url": url, "user_agent": ua, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "requests_made": requests_made, "failures": failures, "artifacts": artifacts,
            "error": (page.error if page is not None else "预算为 0"),
        }
        out_dir.joinpath("manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    html = page.body.decode("utf-8", "replace")

    chl_opt = extract_chl_opt(html)
    if chl_opt:
        data = chl_opt.encode("utf-8")
        out_dir.joinpath("chl_opt.js").write_bytes(data)
        artifacts.append({
            "file": "chl_opt.js", "url": url, "status": page.status,
            "bytes": len(data), "sha256": _sha256(data), "source": "extracted-from-challenge_page.html",
        })

    script_urls = find_challenge_scripts(html, url)[: max(0, int(max_scripts))]
    fetched_scripts = 0
    for script_url in script_urls:
        if not budget_left() or failures >= MAX_FAILURES:
            break
        requests_made += 1
        got = fetch(script_url, ua, timeout)
        if not got.ok:
            failures += 1
            artifacts.append({"file": "", "url": script_url, "error": got.error})
            continue
        name = f"main_{fetched_scripts}.js"
        out_dir.joinpath(name).write_bytes(got.body)
        artifacts.append({
            "file": name, "url": script_url, "status": got.status,
            "bytes": len(got.body), "sha256": _sha256(got.body), "elapsed": got.elapsed,
        })
        fetched_scripts += 1

    manifest = {
        "url": url,
        "user_agent": ua,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "requests_made": requests_made,
        "max_requests": max_requests,
        "failures": failures,
        "page": {
            "status": page.status,
            "headers": page.headers,
            "bytes": len(page.body),
            "sha256": _sha256(page.body),
        },
        "detect": _safe_detect(page),
        "challenge_scripts_found": script_urls,
        "artifacts": artifacts,
        "notes": [
            "sha256 用来证明'这份样本没被改过'；换一次采集就会变（CF 的挑战脚本每次不同），"
            "所以复现时应当比对 detect 结论而不是比对 main_*.js 的哈希。",
            "Set-Cookie 只记'有没有'，不记值（policy.md §4：证据内禁止出现 token/cookie 值）。",
        ],
    }
    out_dir.joinpath("manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _safe_detect(page: FetchResult) -> dict:
    """算 detect 结论；失败也要留痕，但**不许**让整份 manifest 丢掉。

    真机采集的预算是不可再生的：如果 detect 这一步抛异常把 `capture()` 打穿，
    `challenge_page.html` 等产物已经落盘、manifest 却没了 —— 正好毁掉本脚本要解决的
    "可复现"问题（复审 M4）。
    """
    try:
        return detect_summary(page)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CF 挑战样本采集（默认 dry-run，不联网）")
    ap.add_argument("--url", required=True, help="目标 URL（挑战页地址）")
    ap.add_argument("--out", required=True, help="输出目录（产物 + manifest.json）")
    ap.add_argument("--ua", default=DEFAULT_UA, help="User-Agent（默认与 solver.probe 同口径）")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="单请求超时（秒）")
    ap.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS,
                    help=f"请求预算上限（默认 {DEFAULT_MAX_REQUESTS}，硬上限 {HARD_BUDGET}）")
    ap.add_argument("--max-scripts", type=int, default=4, help="最多抓几个挑战脚本")
    ap.add_argument("--real", action="store_true", help="真的发请求（不加就是 dry-run）")
    ap.add_argument("--allow-over-budget", action="store_true",
                    help=f"允许超过硬上限 {HARD_BUDGET}（需明确知道自己要花多少真机预算）")
    args = ap.parse_args(argv)

    if args.max_requests < 1:
        print("--max-requests 必须 ≥ 1", file=sys.stderr)
        return 2
    if args.max_requests > HARD_BUDGET and not args.allow_over_budget:
        print(
            f"拒绝：--max-requests {args.max_requests} 超过硬上限 {HARD_BUDGET}。"
            f"真机请求预算属于不可再生资源（policy.md §2），要突破请显式加 --allow-over-budget。",
            file=sys.stderr,
        )
        return 2

    if not args.real:
        print("[dry-run] 不会发出任何网络请求。真跑请加 --real。")
        print(f"[dry-run] 目标：{args.url}")
        print(f"[dry-run] UA：{args.ua}")
        print(f"[dry-run] 输出目录：{args.out}")
        print(f"[dry-run] 预算：最多 {args.max_requests} 个请求"
              f"（1 个挑战页 + 至多 {min(args.max_scripts, args.max_requests - 1)} 个挑战脚本）")
        print("[dry-run] 顺序：GET 挑战页 → 抠 window._cf_chl_opt → 抓 challenge-platform/jsd 脚本")
        print("[dry-run] 失败纪律：累计失败 ≥5 立即停止（policy.max_failures）")
        print(f"[dry-run] 预计产物：challenge_page.html / chl_opt.js / main_*.js / manifest.json")
        return 0

    out_dir = Path(args.out)
    started = time.monotonic()
    manifest = capture(
        args.url, out_dir, ua=args.ua, timeout=args.timeout,
        max_requests=args.max_requests, max_scripts=args.max_scripts,
    )
    elapsed = round(time.monotonic() - started, 2)
    print(f"完成：{out_dir}（{elapsed}s，{manifest['requests_made']} 个请求，"
          f"{manifest['failures']} 次失败）")
    for item in manifest["artifacts"]:
        if item.get("file"):
            print(f"  - {item['file']:<24} {item.get('bytes', 0):>8} 字节  "
                  f"sha256={item.get('sha256', '')[:16]}…")
        else:
            print(f"  - (失败) {item.get('url')}  {item.get('error', '')}")
    det = manifest.get("detect") or {}
    print(f"detect：route={det.get('recommended_route')} ctype={det.get('cloudflare_challenge_type')!r} "
          f"providers={[p['id'] for p in det.get('providers', [])]}")
    return 0 if manifest["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
