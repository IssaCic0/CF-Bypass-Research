"""从 scrapfly/Antibot-Detector（MIT）的 detectors/*/*.json 重新生成特征表。

用法：
    python scripts/gen_detect_signatures.py D:\\参考项目\\Antibot-Detector

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


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    rows = extract(sys.argv[1])
    if not rows:
        print("没抽到任何特征——检查路径是否指向 Antibot-Detector 仓库根目录")
        return 1
    block = render(rows)
    source = io.open(OUT, encoding="utf-8").read()
    start = source.index(HEADER_MARK)
    end = source.index("\n\n@dataclass", start)
    io.open(OUT, "w", encoding="utf-8", newline="\n").write(source[:start] + block + source[end:])
    print(f"已更新 {OUT}：{len(rows)} 个厂商")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
