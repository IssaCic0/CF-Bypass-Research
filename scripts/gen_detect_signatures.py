"""从 scrapfly/Antibot-Detector（MIT）的 detectors/*/*.json 重新生成特征表。

用法：
    python scripts/gen_detect_signatures.py D:\\参考项目\\Antibot-Detector [--commit <sha>]
    python scripts/gen_detect_signatures.py --check [D:\\参考项目\\Antibot-Detector]

为什么单独做成脚本：特征表会随上游更新；把生成过程留痕，比手抄一份 JSON 更可维护，
也避免"表是哪来的说不清"。
"""

from __future__ import annotations

import glob
import io
import json
import os
import re
import sys

KINDS = ("cookie", "header", "url", "text", "content", "script", "dom")

_PLAIN = re.compile(r"^\^?([A-Za-z0-9_.:-]+)\$?$")


def literal(pattern: str) -> str | None:
    """上游有些规则是 `^datadome$` 这种「无元字符的正则」——剥掉锚点即可当字面串用。

    只接受"去掉 ^ $ 后全是字母数字/下划线/点/冒号/连字符"的模式，避免把真正则误当字面。
    """
    if not pattern:
        return None
    match = _PLAIN.match(pattern)
    return match.group(1) if match else None
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "solver", "detect.py")
HEADER_MARK = "SIGNATURES = ["


def extract(root: str) -> list[dict]:
    rows = []
    for path in sorted(glob.glob(os.path.join(root, "detectors", "*", "*.json"))):
        try:
            data = json.load(io.open(path, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        detection = data.get("detection") or {}
        signals = []
        for kind in KINDS:
            for item in detection.get(kind) or []:
                confidence = item.get("confidence") or 0
                if kind == "cookie":
                    name = item.get("name") or ""
                    if not name:
                        continue
                    if item.get("nameRegex"):
                        name = literal(name) or ""
                    if name:
                        signals.append((kind, name, confidence))
                elif kind == "header":
                    name = item.get("name") or ""
                    if not name:
                        continue
                    if item.get("nameRegex"):
                        name = literal(name) or ""
                    if name:
                        signals.append((kind, name, confidence))
                else:
                    text = item.get("text") or item.get("value") or ""
                    if not text:
                        continue
                    if item.get("textRegex") or item.get("valueRegex"):
                        text = literal(text) or ""
                    if text:
                        signals.append((kind, text, confidence))
        if signals:
            rows.append(
                {
                    "id": data.get("id"),
                    "name": data.get("name"),
                    "category": data.get("category"),
                    "difficulty": data.get("difficulty"),
                    "signals": signals,
                }
            )
    return rows


def render(rows: list[dict]) -> str:
    out = [HEADER_MARK]
    for row in rows:
        signals = ", ".join(
            f"({kind!r}, {token!r}, {confidence})" for kind, token, confidence in row["signals"][:14]
        )
        out.append("    {")
        out.append(f"        'id': {row['id']!r}, 'name': {row['name']!r},")
        out.append(f"        'category': {row['category']!r}, 'difficulty': {row['difficulty']!r},")
        out.append(f'        "signals": [{signals}],')
        out.append("    },")
    out.append("]")
    return "\n".join(out)


def _render_meta(rows: list[dict], commit: str) -> str:
    """生成 SIGNATURES_SOURCE 常量块（T-77：特征表的新鲜度元数据）。"""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "SIGNATURES_SOURCE = {",
        f"    'repo': 'scrapfly/Antibot-Detector',",
        f"    'commit': {commit!r},  # 上游 HEAD sha；无 git 时如实写 unknown",
        f"    'generated_at': {now!r},",
        f"    'count': {len(rows)},",
        "}",
        "",
    ]
    return "\n".join(lines)


def _read_current_meta(source: str) -> dict:
    """从 detect.py 源码里读出当前 SIGNATURES_SOURCE（生成器不 import solver）。"""
    match = re.search(r"^SIGNATURES_SOURCE = \{(.*?)^\}", source, re.S | re.M)
    if not match:
        return {}
    body = match.group(1)
    meta: dict = {}
    for key, val in re.findall(r"'(\w+)':\s*(.+?),", body):
        val = val.strip()
        if val.startswith(("'", '"')):
            meta[key] = val.strip("'\"")
        else:
            try:
                meta[key] = int(val)
            except ValueError:
                meta[key] = val
    return meta


def _check(upstream: str | None) -> int:
    """--check：重新生成并与当前表逐项比较。上游不在 → SKIP（不许静默通过）。"""
    if not upstream or not os.path.isdir(os.path.join(upstream, "detectors")):
        print(f"SKIP：上游目录不在或缺少 detectors/（{upstream or '未提供路径'}）——本次无法核对特征表新鲜度")
        print("SKIP：状态=待验证（CI 绿不等于表还是上游最新；请在有上游 checkout 的环境补跑）")
        return 0
    rows = extract(upstream)
    if not rows:
        print(f"SKIP：在 {upstream} 没抽到任何特征——路径可能不是 Antibot-Detector 仓库根目录；状态=待验证")
        return 0
    block = render(rows)
    source = io.open(OUT, encoding="utf-8").read()
    start = source.index(HEADER_MARK)
    end = source.index("\n\n@dataclass", start)
    current = source[start:end]
    if current != block:
        print(f"不一致：重新生成的特征表与 {OUT} 中的表有差异（{len(rows)} 个厂商）。")
        cur_lines, new_lines = current.splitlines(), block.splitlines()
        import difflib

        diff = list(difflib.unified_diff(cur_lines, new_lines, "current", "regenerated", lineterm=""))
        for line in diff[:80]:
            print(line)
        if len(diff) > 80:
            print(f"…（共 {len(diff)} 行 diff，已截断）")
        print("若差异是上游正常更新：跑生成脚本刷新表 + 更新 SIGNATURES_SOURCE；否则排查谁改了表。")
        return 1
    meta = _read_current_meta(source)
    problems = []
    if not meta:
        problems.append("detect.py 缺 SIGNATURES_SOURCE 常量")
    else:
        if meta.get("count") != len(rows):
            problems.append(f"count={meta.get('count')} 与实际 {len(rows)} 不符")
        if not meta.get("commit"):
            problems.append("commit 为空")
    if problems:
        print("不一致：元数据问题——" + "；".join(problems))
        return 1
    print(f"一致：特征表与上游 {len(rows)} 个厂商逐项一致，元数据完整。")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--check":
        return _check(args[1] if len(args) > 1 else None)
    commit = "unknown"
    if "--commit" in args:
        idx = args.index("--commit")
        commit = args[idx + 1] if len(args) > idx + 1 else "unknown"
        del args[idx:idx + 2]
    if len(args) < 1:
        print(__doc__)
        return 2
    upstream = args[0]
    if not os.path.isdir(os.path.join(upstream, "detectors")):
        print(f"SKIP：上游目录不在或缺少 detectors/（{upstream}）——未写入任何内容；状态=待验证")
        return 0
    rows = extract(upstream)
    if not rows:
        print("没抽到任何特征——检查路径是否指向 Antibot-Detector 仓库根目录")
        return 1
    block = _render_meta(rows, commit) + render(rows)
    source = io.open(OUT, encoding="utf-8").read()
    meta_start = source.index("SIGNATURES_SOURCE = {")
    start = source.index(HEADER_MARK)
    end = source.index("\n\n@dataclass", start)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(
        source[:meta_start] + block + source[end:])
    print(f"已更新 {OUT}：{len(rows)} 个厂商，commit={commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
