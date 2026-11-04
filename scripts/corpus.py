"""corpus.py — 会话快照留存与归档（v6.0 T-66，stdlib only）。

把 cfsolve 写出的会话目录规范化进 `corpus/<UTC日期>-<8位内容哈希>/`：
  - add <session-dir> [--real] [--note ...]：入库 + manifest.json + INDEX.json 摘要
  - list                                    ：打印所有变体
  - verify [--id <id>]                      ：逐文件校验 sha256，篡改/缺失 exit 1
  - redact --check <dir>                    ：扫描敏感明文，命中 exit 1（入库闸门）

脱敏硬规则（policy §4：证据内禁止 token / cookie 值）：
  - cf_clearance 值 → 只保留长度
  - Cookie 头 → 丢弃值
  - URL query → 丢弃
  - 站点 → 只留 eTLD+1

`corpus/` 不入库（.gitignore）；入库的是 `corpus/INDEX.json` 元数据摘要。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CORPUS_ROOT = REPO / "corpus"

SNAPSHOT_FILES = (
    "iframe.js",
    "iframe.deobfuscated.js",
    # 新版 CF / 新版 cfsolve 把入口脚本叫 orchestrator.*（2026-11-04 真机实测），
    # 只认 iframe.* 会把真实会话误判成"不完整目录"。
    "orchestrator.js",
    "orchestrator.deobfuscated.js",
    "response.decoded.txt",
    "second-response.decoded.txt",
    "third-response.decoded.txt",
    "options.json",
    "capture.json",
)
# 反混淆产物是唯一必需项；文件名随 CF 版本变，因此按后缀识别而不是写死名字。
REQUIRED_SUFFIX = ".deobfuscated.js"


def pick_deobf(session_dir: Path) -> Path | None:
    """挑出会话里的反混淆脚本（优先 iframe.*，再 orchestrator.*，再任意 *.deobfuscated.js）。"""
    for name in ("iframe.deobfuscated.js", "orchestrator.deobfuscated.js"):
        cand = session_dir / name
        if cand.is_file():
            return cand
    found = sorted(p for p in session_dir.glob(f"*{REQUIRED_SUFFIX}") if p.is_file())
    return found[0] if found else None

# ---------------------------------------------------------------- 脱敏硬规则

_CLEARANCE_RE = re.compile(r"cf_clearance=([^;\s'\"]+)", re.IGNORECASE)
_COOKIE_PAIR_RE = re.compile(r"([A-Za-z0-9_-]+)=[^;\r\n]*")
_URL_QUERY_RE = re.compile(r"([?&])([A-Za-z0-9_.%-]+)=[^&\s'\"<>)\\]*")
_ETLD_HOST_RE = re.compile(
    r"^(?:[a-z0-9-]+\.)+([a-z0-9-]+\.[a-z]{2,})$", re.IGNORECASE
)


def redact_clearance(text: str) -> str:
    def repl(m: re.Match) -> str:
        return f"cf_clearance=[len:{len(m.group(1))}]"

    return _CLEARANCE_RE.sub(repl, text)


def redact_cookie(text: str) -> str:
    return _COOKIE_PAIR_RE.sub(r"\1=[REDACTED]", text)


def redact_url_query(text: str) -> str:
    out = _URL_QUERY_RE.sub(r"\1", text)
    return re.sub(r"\?&?$", "", out)  # 全部参数被丢后残留的 ? / ?&


def redact_text(text: str) -> str:
    """对**单行/单值**文本做脱敏：clearance 值→长度、cookie 值→REDACTED、query→丢弃。

    注意：本函数只可用于**单个字符串值**（JSON 的 value、单行 metadata），
    绝不可对整篇文件做替换 —— `redact_cookie` 的 `[^;\r\n]*` 会把无分号的
    键值对一路吃到行尾，整篇替换会产出残缺 JSON / 被改写的 JS。
    """
    return redact_url_query(redact_cookie(redact_clearance(text)))


def redact_json_text(text: str) -> str | None:
    """结构化脱敏一份 JSON 文本：只改字符串**值**，不改结构、不改键名。

    解析失败返回 None（调用方据此退化为"原样保留 + 靠闸门拒绝"）。
    """
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None

    def walk(value):
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        return value

    return json.dumps(walk(obj), ensure_ascii=False, indent=2)


def redact_host(host: str) -> str:
    """只留 eTLD+1；识别不了就原样返回（保守：不伪造）。"""
    m = _ETLD_HOST_RE.match(host.strip().lower().rstrip("."))
    if not m:
        return host
    e = m.group(1)
    if host.lower().endswith("." + e):
        return e
    return host


# ---------------------------------------------------------------- 敏感明文扫描


def scan_secrets(dir_path: Path) -> list[dict]:
    """扫描快照内是否含 cf_clearance 明文 / token / cookie 值 / URL 查询串。

    返回命中列表 [{file, line, kind}]；报告里**绝不回显命中值本身**。
    """
    hits: list[dict] = []
    for f in sorted(dir_path.rglob("*")):
        if not f.is_file() or f.name in ("manifest.json",):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # 脱敏占位符（[len:N] / [REDACTED]）本身不是泄露 —— 先抹掉再扫，
            # 否则"已脱敏的文件"会被自己的占位符判成命中，闸门变成永远红灯。
            probe = re.sub(r"\[len:\d+\]", "", line).replace("[REDACTED]", "")
            kinds = []
            # 覆盖 `cf_clearance=xxx` 与 JSON 的 `"cf_clearance": "xxx"` 两种形态：
            # 只认前者会让以 JSON 存的 cookie 全部漏检（真实 capture.json 就是这种）。
            if _CLEARANCE_RE.search(probe) or re.search(
                r'(?i)["\']?cf_clearance["\']?\s*[:=]\s*["\']?[A-Za-z0-9_.\-]{8,}', probe
            ):
                kinds.append("cf_clearance_plaintext")
            if re.search(
                r"(?i)['\"]?(api[_-]?key|token|clientkey|secret)['\"]?\s*[:=]\s*['\"][^'\"]{8,}",
                probe,
            ):
                kinds.append("token_assignment")
            # 要求 `=` 后面**确实还有值**：脱敏后残留的 `cf_clearance=`（空值）
            # 不能反过来把自己判成命中，否则闸门对"已脱敏"的文件永远红灯。
            if re.search(
                r"(?i)['\"]?cookie['\"]?\s*[:=]\s*['\"]?[^'\"\s=]+=[^'\"\s,}\]]", probe
            ):
                kinds.append("cookie_header")
            if re.search(r"(?i)authorization['\"]?\s*[:=]\s*['\"]?bearer\s+\S{8,}", probe):
                kinds.append("authorization_bearer")
            if "setRequestHeader" in line and re.search(
                r"(?i)['\"](cf-chl|cf-chl-[a-z]+)['\"]", line
            ):
                # 静态 token 串本身是研究对象，不算泄露；仅记录
                pass
            for kind in kinds:
                hits.append({"file": f.name, "line": i, "kind": kind})
    return hits


# ---------------------------------------------------------------- 工具


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _redact_options(sess_dir: Path) -> dict:
    """从 options.json / capture.json 提取脱敏后的目标信息。"""
    target: dict = {"host": "unknown"}
    for name in ("options.json", "capture.json"):
        f = sess_dir / name
        if not f.exists():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for key in ("page", "url", "host", "target"):
            v = data.get(key)
            if isinstance(v, str) and v:
                if "://" in v or "/" in v:
                    v = redact_url_query(v)
                    m = re.search(r"(?<=://)([^/?#]+)", v)
                    if m:
                        target["host"] = redact_host(m.group(1))
                        target["page"] = redact_url_query(v)
                        return target
                target["host"] = redact_host(v)
    return target


# ---------------------------------------------------------------- 子命令


def cmd_add(
    session_dir: str,
    real: bool = False,
    note: str = "",
    _rc: bool = False,
    allow_secrets: bool = False,
) -> dict | tuple[int, str]:
    """把会话目录规范化进 corpus/。`--real` 由调用方显式传入；合成样本禁止。"""
    sess = Path(session_dir)
    if not sess.is_dir():
        err = f"session 目录不存在: {sess}"
        return (1, err) if _rc else {"ok": False, "error": err}
    deobf = pick_deobf(sess)
    if deobf is None:
        err = ("拒绝入库：缺少反混淆脚本（*.deobfuscated.js）"
               "（不是完整 cfsolve 会话目录）")
        return (1, err) if _rc else {"ok": False, "error": err}

    deobf_hash = _sha256(deobf.read_bytes())[:8]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = CORPUS_ROOT / f"{date}-{deobf_hash}"

    if dest.exists() and (dest / "manifest.json").exists():
        result = {"ok": True, "dir": str(dest), "idempotent": True}
        return result if not _rc else (0, json.dumps(result))
    if dest.exists():
        shutil.rmtree(dest)  # 同哈希但不完整，重建

    files: list[dict] = []
    total = 0
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    dest.mkdir(parents=True)
    for name in SNAPSHOT_FILES:
        src = sess / name
        if not src.exists():
            continue
        raw = src.read_bytes()
        data = raw
        # 只对 JSON 做**结构化**脱敏（改值不改结构）；JS / 文本一律不改写，
        # 免得正则把研究样本改坏 —— 文本里若仍有明文，下面的闸门会拒绝入库。
        if name.endswith(".json"):
            try:
                redacted = redact_json_text(raw.decode("utf-8"))
            except UnicodeDecodeError:
                redacted = None
            if redacted is not None:
                data = redacted.encode("utf-8")
        (dest / name).write_bytes(data)
        files.append({"name": name, "sha256": _sha256(data), "size": len(data)})
        total += len(data)

    # 入库闸门：脱敏之后仍命中明文 → 默认拒绝入库（不是"提示一下继续"）。
    hits = scan_secrets(dest)
    if hits and not allow_secrets:
        shutil.rmtree(dest, ignore_errors=True)
        err = (
            f"拒绝入库：脱敏后仍命中 {len(hits)} 处敏感明文"
            f"（{hits[0]['file']}:{hits[0]['line']} {hits[0]['kind']} 等）；"
            "请人工清理后重试，或显式 --allow-secrets（不推荐）"
        )
        return (1, err) if _rc else {"ok": False, "error": err, "redact_hits": hits}

    manifest = {
        "id": dest.name,
        "captured_utc": _now_utc(),
        "added_utc": _now_utc(),
        "source_sha8": deobf_hash,
        "real": bool(real),
        "note": note,
        "target": _redact_options(sess),
        "files": files,
        "total_bytes": total,
        "redact_hits": hits,
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_index()
    result = {"ok": True, "dir": str(dest), "idempotent": False,
              "redact_hits": len(hits)}
    return result if not _rc else (0, json.dumps(result))


def _load_index() -> dict:
    idx_path = CORPUS_ROOT / "INDEX.json"
    if idx_path.exists():
        try:
            return json.loads(idx_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"updated_utc": None, "entries": []}


def _write_index() -> None:
    """corpus/INDEX.json：只有元数据（哈希 / 字段数 / 大小 / 时间 / real 标记），无内容。"""
    entries = []
    if CORPUS_ROOT.is_dir():
        for d in sorted(CORPUS_ROOT.iterdir()):
            if not d.is_dir() or d.name == "INDEX.json":
                continue
            mf = d / "manifest.json"
            if not mf.exists():
                continue
            try:
                m = json.loads(mf.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            entries.append({
                "id": m["id"],
                "real": m.get("real", False),
                "files": len(m.get("files", [])),
                "total_bytes": m.get("total_bytes", 0),
                "captured_utc": m.get("captured_utc"),
                "target_host": m.get("target", {}).get("host"),
                "redact_hits": len(m.get("redact_hits", [])),
            })
    idx = {"updated_utc": _now_utc(), "entries": entries}
    CORPUS_ROOT.mkdir(parents=True, exist_ok=True)
    (CORPUS_ROOT / "INDEX.json").write_text(
        json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_list() -> int:
    idx = _load_index()
    entries = idx.get("entries", [])
    if not entries:
        print("corpus 为空（无任何样本）")
        return 0
    print(f"{'id':<24} {'real':<5} {'files':>5} {'bytes':>10}  host")
    for e in entries:
        print(
            f"{e['id']:<24} {str(e.get('real', False)):<5} "
            f"{e.get('files', 0):>5} {e.get('total_bytes', 0):>10}  "
            f"{e.get('target_host', '-')}"
        )
    return 0


def cmd_verify(corpus_id: str | None = None) -> int:
    """逐文件校验 sha256；篡改/缺失 → exit 1。"""
    root = CORPUS_ROOT
    if not root.is_dir():
        print("corpus 目录不存在", file=sys.stderr)
        return 1
    targets = (
        [root / corpus_id] if corpus_id
        else [d for d in root.iterdir() if d.is_dir()]
    )
    rc = 0
    for d in sorted(targets):
        mf = d / "manifest.json"
        if not mf.exists():
            print(f"[FAIL] {d.name}: 缺 manifest.json", file=sys.stderr)
            rc = 1
            continue
        m = json.loads(mf.read_text(encoding="utf-8"))
        for f in m.get("files", []):
            p = d / f["name"]
            if not p.exists():
                print(f"[FAIL] {d.name}/{f['name']}: 文件缺失", file=sys.stderr)
                rc = 1
                continue
            actual = _sha256(p.read_bytes())
            if actual != f["sha256"]:
                print(
                    f"[FAIL] {d.name}/{f['name']}: sha256 不匹配 "
                    f"(expect {f['sha256'][:8]}.. got {actual[:8]}..)",
                    file=sys.stderr,
                )
                rc = 1
    if rc == 0:
        print(f"[OK] 校验通过：{len(targets)} 个变体，全部 sha256 匹配")
    return rc


def cmd_redact_check(dir_path: str | Path) -> int:
    """入库闸门：扫描敏感明文，命中 exit 1 并报文件与行号（不回显值）。"""
    d = Path(dir_path)
    if not d.is_dir():
        print(f"目录不存在: {d}", file=sys.stderr)
        return 2
    hits = scan_secrets(d)
    if not hits:
        print(f"[OK] {d.name}: 未发现敏感明文")
        return 0
    for h in hits:
        print(f"[HIT] {h['file']}:{h['line']}  kind={h['kind']}", file=sys.stderr)
    print(
        f"共 {len(hits)} 处命中；值已隐去（policy §4）。"
        "请先脱敏再入库。",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="会话快照归档（T-66）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="入库一个 cfsolve 会话目录")
    p_add.add_argument("session_dir")
    p_add.add_argument("--real", action="store_true",
                       help="标记为真实抓取样本（合成样本禁止）")
    p_add.add_argument("--note", default="")
    p_add.add_argument("--allow-secrets", action="store_true",
                       help="脱敏后仍有明文时仍然入库（不建议；默认拒绝）")

    p_ck = sub.add_parser("redact", help="敏感明文扫描")
    p_ck.add_argument("--check", dest="check_dir", required=True)

    sub.add_parser("list", help="列出所有变体")
    p_v = sub.add_parser("verify", help="sha256 校验")
    p_v.add_argument("--id", dest="corpus_id", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "add":
        r = cmd_add(args.session_dir, real=args.real, note=args.note,
                    allow_secrets=args.allow_secrets)
        if isinstance(r, dict) and not r.get("ok"):
            print(r.get("error"), file=sys.stderr)
            return 1
        print(json.dumps(r, ensure_ascii=False))
        return 0
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "verify":
        return cmd_verify(args.corpus_id)
    if args.cmd == "redact":
        return cmd_redact_check(args.check_dir)
    return 2


if __name__ == "__main__":
    # Windows 控制台默认 GBK：中文提示会乱码。
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
