"""扫描一批站点，统计 Cloudflare 挑战类型 cType 分布。

目的：判断「纯协议 jsd 求解器」当前覆盖的挑战类型在真实站点中还占多少。
"""

from __future__ import annotations

import concurrent.futures as cf
import re
import sys

import httpx

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
H = {
    "User-Agent": UA,
    "Accept-Encoding": "gzip, deflate",
    "accept-language": "zh-CN,zh;q=0.9",
}

SITES = """
steamdb.info www.glassdoor.com upwork.com www.trustpilot.com www.sofascore.com
www.zillow.com www.etsy.com www.epicgames.com www.imdb.com www.booking.com
www.expedia.com www.tripadvisor.com www.udemy.com www.coursera.org www.khanacademy.org
www.quora.com www.reddit.com www.9gag.com www.dailymotion.com www.vimeo.com
www.twitch.tv www.ebay.com www.walmart.com www.target.com www.bestbuy.com
www.homedepot.com www.ikea.com www.zara.com www.nike.com www.adidas.com
www.bloomberg.com www.reuters.com www.wsj.com www.forbes.com www.theguardian.com
www.npr.org www.bbc.com www.spiegel.de www.lemonde.fr www.elpais.com
www.crunchbase.com www.zoominfo.com www.apollo.io www.hubspot.com www.salesforce.com
www.atlassian.com www.notion.so www.airtable.com www.figma.com www.canva.com
""".split()


def probe(site: str) -> tuple[str, str, str, str, int]:
    """返回 (site, status, cf-mitigated, cType, 页面长度)。"""
    try:
        with httpx.Client(headers=H, timeout=10, follow_redirects=False) as c:
            r = c.get("https://" + site + "/")
        body = r.text[:200000]
        ctype = ""
        if "_cf_chl_opt" in body:
            m = re.search(r"cType[\"']?\s*:\s*[\"']([a-z_]+)", body)
            ctype = m.group(1) if m else "?"
        return (site, str(r.status_code), str(r.headers.get("cf-mitigated") or ""), ctype, len(body))
    except Exception as e:  # noqa: BLE001
        return (site, "ERR", type(e).__name__, "", 0)


def main() -> None:
    rows = []
    with cf.ThreadPoolExecutor(10) as ex:
        for row in ex.map(probe, SITES):
            rows.append(row)
    chall = [r for r in rows if r[3]]
    print(f"探测 {len(rows)} 站，触发 CF 挑战页（含 _cf_chl_opt）{len(chall)} 站\n")
    print(f"{'site':26s} {'status':6s} {'cf-mitigated':12s} {'cType':10s} len")
    for r in sorted(rows, key=lambda x: (x[3] == "", x[0])):
        mark = "  <=" if r[3] else ""
        print(f"{r[0]:26s} {r[1]:6s} {r[2]:12s} {r[3] or '-':10s} {r[4]}{mark}")
    from collections import Counter

    dist = Counter(r[3] for r in chall)
    print("\ncType 分布:", dict(dist))


if __name__ == "__main__":
    main()
