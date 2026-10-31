"""静态分析 CF jsd main.js：手动扫描字符串字面量（避开正则转义坑）。"""

from __future__ import annotations

import glob
import json
import re
import sys

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
H = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate", "accept-language": "zh-CN,zh;q=0.9"}


def scan_strings(text: str) -> list[str]:
    """扫描 JS 字符串字面量（手写状态机，支持 \\ 转义）。"""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in ("'", '"'):
            quote = ch
            i += 1
            buf: list[str] = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    buf.append(text[i : i + 2])
                    i += 2
                    continue
                if c == quote:
                    i += 1
                    break
                buf.append(c)
                i += 1
            out.append("".join(buf))
        else:
            i += 1
    return out


def analyze(name: str, text: str) -> None:
    strs = scan_strings(text)
    uniq = sorted(set(strs), key=len, reverse=True)
    print(f"\n===== {name}: len={len(text)} 字符串 {len(strs)} 去重 {len(uniq)} =====")
    print("  最长 8 条字符串:")
    for s in uniq[:8]:
        print(f"    len={len(s):4d} {s[:140]!r}")
    mid = [s for s in uniq if 48 <= len(s) <= 96]
    print(f"  长度 48-96（码表候选）{len(mid)} 条:")
    for s in mid[:6]:
        printable = sum(1 for c in s if c.isprintable())
        print(f"    len={len(s)} printable={printable} {s[:80]!r}")
    kws = [
        "oneshot",
        "jsd/",
        "challenge-platform",
        "atob",
        "btoa",
        "fromCharCode",
        "charCodeAt",
        "sendBeacon",
        "XMLHttpRequest",
        "fetch(",
        "_cf_chl_opt",
        "soco4",
        "location.href",
    ]
    print("  关键字命中:", {k: text.count(k) for k in kws})


def main() -> None:
    files = sorted(glob.glob("main_0_*.js")) + ["main_steamdb.js"]
    for f in files:
        try:
            analyze(f, open(f, encoding="utf-8").read())
        except FileNotFoundError:
            print(f"缺文件 {f}")
    # 顺带抓一份 steamdb 的 challenge 页上下文（真 challenge 时页面里会带 cType / ray）
    with httpx.Client(headers=H, timeout=25, follow_redirects=False) as c:
        for u in ("https://steamdb.info/",):
            r = c.get(u)
            body = r.text
            print(f"\n===== {u} status={r.status_code} cf-mitigated={r.headers.get('cf-mitigated')} =====")
            print("  cf-ray:", r.headers.get("cf-ray"))
            print("  页面里 jsd 出现次数:", body.count("jsd"), "| challenge-platform:", body.count("challenge-platform"))
            m = re.search(r"cType[\"']?\s*[:=]\s*[\"'](\w+)", body)
            print("  cType:", m.group(1) if m else None)
            open("challenge_page.html", "w", encoding="utf-8").write(body)


if __name__ == "__main__":
    main()
