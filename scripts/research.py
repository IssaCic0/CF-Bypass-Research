"""research.py — 离线分析台统一入口（T-67/T-68/T-73/T-74，stdlib only）。

本文件随 T-68 先落地 `assertions` 子命令的矩阵输出函数
（`assertions_matrix`，供测试 import 调用，不拼 shell）；
`doctor / build / stage2 / vm / diff` 由 T-67 扩展。

assertions 矩阵：
  变体哈希 × 断言 → pass/fail/skip，落
  `plans/证据/research-assertions-<日期>.json`（只含结论，不含样本内容）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import corpus  # noqa: E402

EVIDENCE_DIR = REPO / "plans" / "证据"

# ---- T-67 目标二进制（只编这 5 个 example，其余按需；全量编译拖 10 分钟以上）----
TARGET_BINARIES = (
    "stage2_snapshot",
    "vm_snapshot",
    "disassemble",
    "vocabulary",
    "probe_plan",
)
DEFAULT_UPSTREAM = r"D:\cfsolve-upstream"
PROFILE_REL = Path("profiles") / "safari-26-ios.json"


class ResearchError(Exception):
    """阻塞施工的错误（doctor FAIL / build 前置缺失 / 二进制缺失）。"""


def find_cargo():
    """定位 cargo（where.exe）；找不到返回 None。独立函数便于 monkeypatch。"""
    try:
        out = subprocess.run(["where.exe", "cargo"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().splitlines()[0].strip()
    return None


def resolve_upstream(explicit):
    upstream = explicit or os.environ.get("CFSOLVE_UPSTREAM") or DEFAULT_UPSTREAM
    if not Path(upstream).is_dir():
        raise ResearchError(
            f"cfsolve upstream 不存在: {upstream}\n"
            f"修复: git clone https://github.com/chris124567/cfsolve {upstream}"
            " 或 set CFSOLVE_UPSTREAM=<path>")
    return upstream


STAGE2_ASSERTIONS = (
    "dispatcher", "collection_fn", "probe_state", "counter", "token", "input",
)
SOURCE_ASSERTIONS = ("A_inline_chl_opt", "B_static_token", "C_encoder_shape")

# stage2 层的结构断言只能拿 stage2 响应程序来判。会话若只停在入口脚本
# （反混淆完成、但没走到 probe POST），这些断言"不可判定"而不是"失败"——
# 把缺输入报成 fail 会让人误以为 CF 换了结构（v5.0.2 的同类教训）。
STAGE2_MARKERS = (
    "response.decoded.txt", "second-response.decoded.txt", "third-response.decoded.txt",
)


def _has_stage2_program(session_dir: Path) -> bool:
    return any((session_dir / name).is_file() for name in STAGE2_MARKERS)


def _stage2_hits(js: str) -> dict:
    return {
        "dispatcher": "dispatcher" in js,
        "collection_fn": "collection" in js,
        "probe_state": "probe" in js,
        "counter": "counter" in js,
        "token": "token" in js,
        "input": "input" in js,
    }


def _source_hits(js: str) -> dict:
    return {
        "A_inline_chl_opt": "_cf_chl_opt" in js,
        "B_static_token": "setRequestHeader" in js and "cf-chl" in js,
        "C_encoder_shape": ("alphabet" in js and "modulus" in js) or "encoder" in js,
    }


def _run_all(js: str) -> dict:
    out = _stage2_hits(js)
    out.update(_source_hits(js))
    return out


def assertions_matrix(
    out_path: str | Path | None = None,
    include_synthetic: bool = False,
) -> dict:
    """计算断言矩阵并（可选）写证据 JSON。

    默认只读 corpus/INDEX.json 里的 `real` 样本（合成样本不参与
    "结构假设是否被现实满足"的判定）；`include_synthetic=True` 供
    测试侧在合成语料上自检矩阵框架本身。
    """
    idx = corpus._load_index()
    entries = [
        e for e in idx.get("entries", [])
        if e.get("real") or include_synthetic
    ]
    variants = []
    totals = {"pass": 0, "fail": 0, "skip": 0}
    all_names = tuple(STAGE2_ASSERTIONS) + tuple(SOURCE_ASSERTIONS)

    if not entries:
        # 无样本：全部 skip，reason 固定含「待授权清单」
        totals["skip"] = len(all_names)
    for e in entries:
        session_dir = corpus.CORPUS_ROOT / e["id"]
        # 反混淆产物名随 CF/cfsolve 版本变（iframe.* → orchestrator.*）：
        # 写死文件名会让"有真样本"也全 skip，等于断言矩阵永远空转。
        js_path = corpus.pick_deobf(session_dir)
        row: dict = {"id": e["id"], "assertions": {}}
        if js_path is None:
            for name in all_names:
                row["assertions"][name] = "skip"
                totals["skip"] += 1
        else:
            js = js_path.read_text(encoding="utf-8", errors="replace")
            hits = _run_all(js)
            has_stage2 = _has_stage2_program(session_dir)
            for name in all_names:
                if name in STAGE2_ASSERTIONS and not has_stage2:
                    row["assertions"][name] = "skip"
                    totals["skip"] += 1
                    continue
                status = "pass" if hits.get(name) else "fail"
                row["assertions"][name] = status
                totals[status] += 1
            if not has_stage2:
                row["skip_reason"] = (
                    "样本未走到 stage2（无 response.decoded.txt）："
                    "stage2 结构断言不可判定，需一份到 probe POST 的完整会话"
                )
        variants.append(row)

    report = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # 如实标注断言强度：当前实现是**字面量子串存在性**，不是 AST 结构校验。
        # 它能回答"这个结构标记还在不在脚本里"，回答不了"结构形态有没有变"。
        # 下一版接 stage2_snapshot 的真实分析输出才能升级强度（见指南 T-72）。
        "strength": "literal-substring",
        "strength_note": (
            "断言为字面量子串存在性（如 'counter' in js），不是 AST 结构校验；"
            "通过 ≠ 结构未变，只说明该标记仍出现。升级路径见 plans/下一步改进指南.md T-72"
        ),
        "assertions": list(all_names),
        "variants": variants,
        "totals": totals,
        "skip_reason": (
            "corpus empty: no real session snapshot; "
            "see plans/下一步改进指南.md §6 待授权清单"
            if not entries else None
        ),
    }
    if out_path is not None:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    # 服务侧（/health.research、/metrics）只认 corpus/assertions.json 这份摘要。
    # 不写它的话，/metrics 的断言计数永远是 0 —— 那正是"指标说谎"。
    if corpus.CORPUS_ROOT.is_dir():
        latest = None
        for e in idx.get("entries", []):
            captured = e.get("captured_utc")
            if captured and (latest is None or captured > latest):
                latest = captured
        diff_files = sorted(EVIDENCE_DIR.glob("research-diff-*.md"))
        last_diff = None
        if diff_files:
            # 取 mtime 最新的那份：文件名按 id 字典序排，不代表"最近生成"
            newest = max(diff_files, key=lambda p: p.stat().st_mtime)
            last_diff = datetime.fromtimestamp(
                newest.stat().st_mtime, timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = {
            "generated_utc": report["generated_utc"],
            # 两个口径必须分开：defined = 断言**定义**条数（9），
            # total = **判定次数**（跨变体求和 = pass+fail+skip）。
            # 混用一个会让"≥2 个变体"时 sum != total，被误判成不一致而清零。
            "assertions_defined": len(all_names),
            "assertions_total": totals["pass"] + totals["fail"] + totals["skip"],
            "pass": totals["pass"],
            "fail": totals["fail"],
            "skip": totals["skip"],
            "last_snapshot_at": latest,
            # 差分报告本身也是研究资产：给出最近一份报告的生成时间（没有就 None）
            "last_diff_at": last_diff,
            # 有 skip 就必须说清原因：顶层无 skip_reason 时给出 per-variant 的指向，
            # 否则 /health 会显示"6 条 skip 但 note 为空"，等于让人去猜。
            "skip_reason": report["skip_reason"] or (
                f"{totals['skip']} 条断言 skip：逐个样本原因见本文件 variants[].skip_reason"
                if totals["skip"] else None
            ),
            "variants": len(entries),
        }
        try:
            (corpus.CORPUS_ROOT / "assertions.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass  # 写不了摘要不影响矩阵本身；服务侧会读不到并给 note
    return report


# ================= T-67 doctor / build / stage2 / vm / diff / gaps =================

def _check(name, ok, detail, fix):
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail, "fix": fix}


def _find_libclang():
    """bindgen 需要 libclang：先看 LIBCLANG_PATH，再看 LLVM 默认位置与 pip 的 clang 包。

    返回 libclang.dll 的完整路径，找不到返回 None。
    """
    cands = []
    env = os.environ.get("LIBCLANG_PATH")
    if env:
        cands.append(Path(env))
    cands += [Path(r"C:\Program Files\LLVM\bin"), Path(r"C:\Program Files (x86)\LLVM\bin")]
    try:  # pip install libclang 会把 dll 放在 <site-packages>/clang/native
        import clang as _clang_pkg  # noqa: F401

        cands.append(Path(_clang_pkg.__file__).parent / "native")
    except Exception:
        pass
    for d in cands:
        for name in ("libclang.dll", "clang.dll", "libclang.so", "libclang.dylib"):
            if (d / name).is_file():
                return str(d / name)
    return None


def _find_nasm():
    """BoringSSL 的汇编源需要 nasm；PATH 里没有时看常见安装目录。"""
    path = shutil.which("nasm")
    if path:
        return path
    for cand in (r"C:\Program Files\NASM\nasm.exe", r"C:\Program Files (x86)\NASM\nasm.exe"):
        if Path(cand).is_file():
            return cand
    return None


def _stale_cmake_cache(upstream):
    """探测 generator 冲突的残留 CMakeCache，返回 (说明, 待删目录) 或 None。

    btls-sys 的 build.rs 在 Windows 上要求 Visual Studio generator；若
    `out/build/CMakeCache.txt` 里记着 Ninja（上一次换工具链留下的），cmake 会报
    "Does not match the generator used previously: Ninja" 并让 cargo panic。
    这条在本轮实测中真实发生过，因此必须由 doctor 提前拦下而不是留给 cargo 甩栈。
    """
    build_root = Path(upstream) / "target" / "release" / "build"
    if not build_root.is_dir():
        return None
    for cache in build_root.glob("btls-sys-*/out/build/CMakeCache.txt"):
        try:
            text = cache.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = re.search(r"^CMAKE_GENERATOR:INTERNAL=(.+)$", text, re.MULTILINE)
        if not m:
            continue
        generator = m.group(1).strip()
        if sys.platform == "win32" and not generator.lower().startswith("visual studio"):
            return (f"残留 CMakeCache 的 generator 是 {generator}，与 Windows 上要求的 "
                    f"Visual Studio generator 冲突", str(cache.parent))
    return None


def cmd_doctor(cfsolve_upstream=None):
    """逐项检查施工前置（G0），返回报告 dict（含 exit_code）。"""
    checks = []
    cargo = find_cargo()
    checks.append(_check("cargo", cargo is not None, cargo or "not found",
                         "rustup install (https://rustup.rs) 并确认 cargo 在 PATH"))
    upstream = cfsolve_upstream or os.environ.get("CFSOLVE_UPSTREAM") or DEFAULT_UPSTREAM
    checks.append(_check("cfsolve_upstream", Path(upstream).is_dir(), upstream,
                         f"git clone https://github.com/chris124567/cfsolve {upstream}"
                         " 或 set CFSOLVE_UPSTREAM=<path>"))
    release = Path(upstream) / "target" / "release"
    for exe in TARGET_BINARIES:
        exe_path = _binary_path(upstream, exe)
        checks.append(_check(f"binary:{exe}", exe_path is not None,
                             str(exe_path) if exe_path else
                             str(release / f"{exe}.exe") + " 或 " +
                             str(release / "examples" / f"{exe}.exe"),
                             f"python scripts/research.py build --cfsolve-upstream {upstream}"))
    corpus_dir = REPO / "corpus"
    if not corpus_dir.is_dir():
        try:
            corpus_dir.mkdir(parents=True)
        except OSError:
            pass
    checks.append(_check("corpus_writable", corpus_dir.is_dir() and os.access(corpus_dir, os.W_OK),
                         str(corpus_dir), f"mkdir {corpus_dir} 并检查写权限"))
    profile = Path(upstream) / PROFILE_REL
    checks.append(_check("profile", profile.exists(), str(profile),
                         f"缺少 {profile}；见 docs/录制-profile-操作手册.md"))
    # build 前置：btls-sys 的 BoringSSL 走 cmake（Windows 上另需 nasm 汇编）。
    # 不在这里检查，cargo 会甩一段 cmake panic，看不出该装什么。
    cmake = shutil.which("cmake")
    checks.append(_check("cmake", cmake is not None, cmake or "not found",
                         "安装 CMake（https://cmake.org/download/）并确认 cmake 在 PATH"))
    if sys.platform == "win32":
        nasm = _find_nasm()
        checks.append(_check("nasm", nasm is not None, nasm or "not found",
                             '安装 NASM（https://www.nasm.us/）到 "C:\\Program Files\\NASM" 或加入 PATH；'
                             "BoringSSL 的汇编源需要它（build 会自动把它注入子进程 PATH）"))
        libclang = _find_libclang()
        checks.append(_check("libclang", libclang is not None, libclang or "not found",
                             "bindgen 需要 libclang.dll：pip install libclang"
                             "（dll 落在 <site-packages>/clang/native），或装 LLVM 后设 LIBCLANG_PATH"
                             "（build 会自动注入探测到的路径）"))
    stale = _stale_cmake_cache(upstream)
    checks.append(_check("cmake_cache", stale is None,
                         stale[0] if stale else "no stale CMakeCache",
                         f"rm -rf {stale[1] if stale else '<btls-sys 的 out/build>'}"
                         " 后重跑 build：generator 变了但 CMakeCache.txt 还是旧的"))
    exit_code = 0 if all(c["status"] == "PASS" for c in checks) else 1
    return {"checks": checks, "exit_code": exit_code, "upstream": upstream}


def cmd_build(cfsolve_upstream=None):
    """只编 5 个目标 example；upstream 缺失直接拒绝且不执行任何 cargo 命令。

    注意顺序：先 resolve_upstream（纯路径检查，无 subprocess），再 find_cargo
    —— 这样 upstream 缺失时可以保证一个 cargo 进程都没被拉起。"""
    upstream = resolve_upstream(cfsolve_upstream)
    cargo = find_cargo()
    if cargo is None:
        raise ResearchError("cargo 不可用；修复: rustup install")
    args = [cargo, "build", "--release"]
    for exe in TARGET_BINARIES:
        args += ["--example", exe]
    # 自动注入 btls-sys 的构建前置，避免让用户手抄环境变量：
    # bindgen 要 libclang.dll（LIBCLANG_PATH），BoringSSL 汇编要 nasm 在 PATH。
    # 本轮实测：先撞 generator 冲突 → 再撞 libclang 缺失，两次都是这个链条上的。
    env = dict(os.environ)
    injected = {}
    if not env.get("LIBCLANG_PATH"):
        libclang = _find_libclang()
        if libclang:
            env["LIBCLANG_PATH"] = str(Path(libclang).parent)
            injected["LIBCLANG_PATH"] = env["LIBCLANG_PATH"]
    if sys.platform == "win32":
        nasm = _find_nasm()
        if nasm and str(Path(nasm).parent) not in env.get("PATH", ""):
            env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path(nasm).parent)
            injected["PATH+nasm"] = str(Path(nasm).parent)
    proc = subprocess.run(args, cwd=upstream, env=env)
    return {"returncode": proc.returncode, "command": args, "upstream": upstream,
            "env_injected": injected}


def _binary_path(upstream, name):
    """定位目标二进制：bin 在 target/release/，example 在 target/release/examples/。

    本轮实测：cargo build --example 的产物落在 examples/ 子目录，
    只在 release/ 找会让"构建成功"也报缺失（G0 永远过不了）。
    """
    release = Path(upstream) / "target" / "release"
    for cand in (release / f"{name}.exe", release / "examples" / f"{name}.exe"):
        if cand.exists():
            return cand
    for cand in (release / name, release / "examples" / name):
        if cand.exists():
            return cand
    return None


def _binary(upstream, name):
    path = _binary_path(upstream, name)
    if path is None:
        raise ResearchError(
            f"二进制缺失: {Path(upstream) / 'target' / 'release' / (name + '.exe')}"
            "（也不在 examples/ 下）\n修复: python scripts/research.py build"
        )
    return path


def _session_dir(corpus_id):
    session = REPO / "corpus" / corpus_id
    if not session.is_dir():
        raise ResearchError(f"corpus 会话目录不存在: {session}")
    return session


def cmd_stage2(corpus_id, cfsolve_upstream=None):
    """跑 stage2_snapshot，输出规范化 JSON（6 类分析 outcome/error 逐项）。"""
    upstream = resolve_upstream(cfsolve_upstream)
    proc = subprocess.run([str(_binary(upstream, "stage2_snapshot")), str(_session_dir(corpus_id))],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ResearchError(f"stage2_snapshot 失败: {proc.stderr}")
    return {"corpus_id": corpus_id, "snapshot": json.loads(proc.stdout)}


def cmd_vm(corpus_id, fixture=False, cfsolve_upstream=None):
    """跑 vm_snapshot；--fixture 只抽定义 VM 的那个函数。"""
    upstream = resolve_upstream(cfsolve_upstream)
    session = _session_dir(corpus_id)
    script = corpus.pick_deobf(session)
    if script is None:
        raise ResearchError(f"缺少反混淆脚本（*.deobfuscated.js）: {session}")
    exe = _binary(upstream, "vm_snapshot")
    if fixture:
        args = [str(exe), "--fixture", str(script)]
    else:
        program = session / "response.decoded.txt"
        args = [str(exe), str(script), str(program) if program.exists() else "-"]
    proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise ResearchError(f"vm_snapshot 失败: {proc.stderr}")
    return {"corpus_id": corpus_id, "fixture": fixture, "output": proc.stdout}


# ================= T-70 gaps / T-75 diff =================

ANALYZE_DIR = REPO / "tools" / "cfsolve-patches" / "analyze"
MAX_REPORT_LINE = 200
_STRING_LITERAL_RE = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'((?:[^\'\\\n]|\\.)*)\'')
DIFF_SECTIONS = (
    "1. 元信息",
    "2. AST 形态变更",
    "3. 字段名集合变更",
    "4. 断言矩阵变更",
    "5. 建议动作",
)


def _analyzer(name):
    """按需加载 tools/cfsolve-patches/analyze 下的分析器（目录名带 `-`，不能直接 import）。"""
    if str(ANALYZE_DIR) not in sys.path:
        sys.path.insert(0, str(ANALYZE_DIR))
    return importlib.import_module(name)


def _clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _snapshot(directory):
    """读取一份会话快照的摘要（脚本原文只在内存里用，不进报告）。"""
    d = Path(directory)
    js = corpus.pick_deobf(d)
    exists = js is not None and js.is_file()
    data = js.read_bytes() if exists else b""
    text = js.read_text(encoding="utf-8", errors="replace") if exists else ""
    manifest = {}
    manifest_path = d / "manifest.json"
    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            manifest = {}
    target = manifest.get("target") if isinstance(manifest.get("target"), dict) else {}
    return {
        "dir": d,
        "name": d.name,
        "text": text,
        "has_script": exists,
        # stage2 的结构断言只能拿 stage2 程序判；样本没走到 stage2 时它们是
        # "不可判定"（skip），不是"失败"——否则报告会让人去为不存在的故障写补丁。
        "has_stage2": _has_stage2_program(d),
        "captured_utc": manifest.get("captured_utc") or manifest.get("added_utc") or "unknown",
        "host": target.get("host") or "unknown",
        "size": len(data),
        "sha8": hashlib.sha256(data).hexdigest()[:8] if data else "unknown",
    }


def _shape_counts(text, filename):
    if not text:
        return {}
    report = _analyzer("alias_shapes").analyze_source(text, filename)
    return {sig: info["count"] for sig, info in report.items()}


def _field_names(text):
    """deobf 后出现的字符串字面量集合（运行时字段名解码最直观的指纹）。"""
    if not text:
        return set()
    masked = _analyzer("primitive_gap").mask_source(text, mask_strings=False)
    names = set()
    for m in _STRING_LITERAL_RE.finditer(masked):
        value = m.group(1) if m.group(1) is not None else m.group(2)
        if value:
            names.add(value)
    return names


def _assertion_states(text, has_stage2=True):
    """stage2 六条断言的判定：没有 stage2 程序 → skip（不可判定），不报 fail。"""
    if not text or not has_stage2:
        return {name: "skip" for name in STAGE2_ASSERTIONS}
    hits = _stage2_hits(text)
    return {name: ("pass" if hits.get(name) else "fail") for name in STAGE2_ASSERTIONS}


def _patches_for_assertion(assertion, patches):
    """断言 → 补丁映射（来自 PATCHES.json 的 assertion_ids）。"""
    matched = []
    for patch in patches:
        for assertion_id in patch.get("assertion_ids") or []:
            tokens = {t for t in re.split(r"[-_]", assertion_id) if t}
            if assertion in tokens or assertion in assertion_id:
                matched.append(patch)
                break
    return matched


def _shape_rows(shapes_a, shapes_b):
    rows = []
    for signature in sorted(set(shapes_a) | set(shapes_b)):
        count_a = shapes_a.get(signature, 0)
        count_b = shapes_b.get(signature, 0)
        if count_a == count_b:
            continue
        if count_a == 0:
            change = "新增"
        elif count_b == 0:
            change = "消失"
        elif count_b >= 2 * count_a and count_b - count_a >= 3:
            change = "暴涨"
        else:
            change = "变更"
        rows.append((signature, count_a, count_b, change))
    return rows


def build_diff_report(dir_a, dir_b, patches=None, generated_utc=None):
    """两份快照 → 五段 Markdown 报告（确定性；除生成时间外逐字节一致）。"""
    snap_a, snap_b = _snapshot(dir_a), _snapshot(dir_b)
    patch_list = load_patches() if patches is None else list(patches)
    shapes_a = _shape_counts(snap_a["text"], snap_a["name"])
    shapes_b = _shape_counts(snap_b["text"], snap_b["name"])
    fields_a = _field_names(snap_a["text"])
    fields_b = _field_names(snap_b["text"])
    states_a = _assertion_states(snap_a["text"], snap_a.get("has_stage2", True))
    states_b = _assertion_states(snap_b["text"], snap_b.get("has_stage2", True))
    stamp = generated_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = [
        f"# 研究快照差分报告：{_clip(snap_a['name'], 60)} → {_clip(snap_b['name'], 60)}",
        "",
        f"生成时间(UTC): {stamp}",
        "",
        f"## {DIFF_SECTIONS[0]}",
        "",
        "| 项目 | A | B |",
        "| --- | --- | --- |",
        f"| 抓取时间 | {_clip(snap_a['captured_utc'], 40)} | {_clip(snap_b['captured_utc'], 40)} |",
        f"| 目标 eTLD+1 | {_clip(snap_a['host'], 40)} | {_clip(snap_b['host'], 40)} |",
        f"| 脚本字节数 | {snap_a['size']} | {snap_b['size']} |",
        f"| sha256 前 8 位 | {snap_a['sha8']} | {snap_b['sha8']} |",
        f"| 脚本存在 | {'是' if snap_a['has_script'] else '否'} | "
        f"{'是' if snap_b['has_script'] else '否'} |",
        "",
        f"## {DIFF_SECTIONS[1]}",
        "",
    ]
    shape_rows = _shape_rows(shapes_a, shapes_b)
    lines.append(f"- 形态签名数：A={len(shapes_a)}，B={len(shapes_b)}，变更={len(shape_rows)}。")
    if shape_rows:
        lines += ["", "| 形态签名 | A 次数 | B 次数 | 变化 |", "| --- | --- | --- | --- |"]
        for signature, count_a, count_b, change in shape_rows:
            lines.append(
                f"| {_clip(signature, 120)} | {count_a} | {count_b} | {change} |")
    else:
        lines.append("- 无形态频次变更。")

    added = sorted(fields_b - fields_a)
    removed = sorted(fields_a - fields_b)
    lines += ["", f"## {DIFF_SECTIONS[2]}", ""]
    lines.append(f"- 共同字段名：{len(fields_a & fields_b)} 个；新增 {len(added)}；"
                 f"消失 {len(removed)}。")
    lines.append("- 新增字段名：")
    lines += [f"  - `{_clip(name, 80)}`" for name in added[:40]] or ["  - （无）"]
    if len(added) > 40:
        lines.append(f"  - ……另有 {len(added) - 40} 项（见证据 JSON）")
    lines.append("- 消失字段名：")
    lines += [f"  - `{_clip(name, 80)}`" for name in removed[:40]] or ["  - （无）"]
    if len(removed) > 40:
        lines.append(f"  - ……另有 {len(removed) - 40} 项（见证据 JSON）")

    lines += ["", f"## {DIFF_SECTIONS[3]}", "",
              "> 判据强度：**literal-substring**（字面量子串存在性），不是 AST 结构校验。",
              "> `pass` 只表示该标记仍出现，**不表示结构形态未变**；`skip` = 该侧样本未走到 stage2，不可判定。",
              "",
              "| 断言 | A | B | 转移 |", "| --- | --- | --- | --- |"]
    for name in STAGE2_ASSERTIONS:
        state_a, state_b = states_a[name], states_b[name]
        transition = "无变化" if state_a == state_b else f"{state_a}→{state_b}"
        lines.append(f"| {name} | {state_a} | {state_b} | {transition} |")

    lines += ["", f"## {DIFF_SECTIONS[4]}", ""]
    suggestions = []
    for name in STAGE2_ASSERTIONS:
        state_a, state_b = states_a[name], states_b[name]
        if state_a == state_b:
            continue  # 状态没变（含 fail→fail、pass→pass、skip→skip）不需要动作
        if state_b == "skip" or state_a == "skip":
            # 任一侧不可判定 ⇒ 不许建议「新增补丁」。B 没走到 stage2 就要求补丁，
            # 等于让人为"数据不存在"去改算法。
            suggestions.append(
                f"- `{name}`：{state_a} → {state_b}；不可判定（一侧样本未走到 stage2），"
                "不产生补丁建议 —— 需先补一份到 probe POST 的完整会话。")
            continue
        if state_b == "pass":
            matched = _patches_for_assertion(name, patch_list)
            where = ("、".join(f"`{p['id']}`" for p in matched) if matched else "无登记补丁")
            suggestions.append(
                f"- `{name}`：{state_a} → pass；已恢复（相关补丁 {where} 仍在生效，无需动作）。")
            continue
        matched = _patches_for_assertion(name, patch_list)
        if matched:
            detail = "；".join(
                f"`{p['id']}`（assertion_ids: {', '.join(p.get('assertion_ids') or [])}）"
                for p in matched)
            suggestions.append(
                f"- `{name}`：{state_a} → {state_b}；建议动作：检查/回归补丁 {detail}。")
        else:
            suggestions.append(
                f"- `{name}`：{state_a} → {state_b}；建议动作：PATCHES.json 无对应补丁登记，"
                "需新增补丁并在 PATCHES.json 里登记断言（否则补丁净收益不可见）。")
    lines += suggestions or ["- 无 fail/skip 断言，无需动作。"]

    markdown = "\n".join(lines) + "\n"
    over = [ln for ln in markdown.splitlines() if len(ln) > MAX_REPORT_LINE]
    if over:  # 报告禁止含长脚本片段：这里兜底截断，保证任何一行都不超限
        markdown = "\n".join(
            ln if len(ln) <= MAX_REPORT_LINE else ln[: MAX_REPORT_LINE - 1] + "…"
            for ln in markdown.splitlines()
        ) + "\n"
    return {
        "markdown": markdown,
        "sections": [f"## {title}" for title in DIFF_SECTIONS],
        "meta": {"a": {k: v for k, v in snap_a.items() if k not in ("text", "dir")},
                 "b": {k: v for k, v in snap_b.items() if k not in ("text", "dir")}},
        "shape_changes": shape_rows,
        "fields_added": added,
        "fields_removed": removed,
        "assertions": {"a": states_a, "b": states_b},
    }


def cmd_diff(id_a, id_b, cfsolve_upstream=None):
    """两份 corpus 快照 → 人类可读 Markdown 差分报告（T-75，纯离线，不需要二进制）。"""
    report = build_diff_report(_session_dir(id_a), _session_dir(id_b))
    safe = lambda value: re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value))  # noqa: E731
    out = EVIDENCE_DIR / f"research-diff-{safe(id_a)}-{safe(id_b)}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report["markdown"], encoding="utf-8", newline="\n")
    report.update({"id_a": id_a, "id_b": id_b, "path": str(out)})
    return report


def cmd_gaps(corpus_id, cfsolve_upstream=None, as_json=False):
    """VM 内建原语缺口清单（T-70）：扫描 deobf 脚本，对 VM 注册表求差集。"""
    session = _session_dir(corpus_id)
    js = corpus.pick_deobf(session)
    if js is None:
        raise ResearchError(f"缺少反混淆脚本（*.deobfuscated.js）: {session}")
    primitive_gap = _analyzer("primitive_gap")
    vm_dir = os.environ.get(primitive_gap.ENV_VM_DIR)
    if not vm_dir:
        vm_dir = str(Path(resolve_upstream(cfsolve_upstream)) / "src" / "vm")
    try:
        report = primitive_gap.find_gaps(js, vm_dir)
    except primitive_gap.RegistryError as exc:  # 环境缺失必须 FAIL，不许静默空集
        raise ResearchError(str(exc)) from exc
    report["corpus_id"] = corpus_id
    return report



# ================= T-73 run（预算护栏） =================

STAGE_FILES = (
    ("second-response.decoded.txt", "stage2_received"),
    ("response.decoded.txt", "payload_sent"),
    ("iframe.deobfuscated.js", "deobfuscated"),
    # 新版 cfsolve 的入口脚本叫 orchestrator.*（2026-11-04 真机实测）
    ("orchestrator.deobfuscated.js", "deobfuscated"),
    ("iframe.js", "captured"),
    ("orchestrator.js", "captured"),
)


def infer_stage(directory):
    """输入目录文件快照 → 最后到达阶段。可测函数（T-73 核心）。"""
    names = {p.name for p in Path(directory).iterdir()} if Path(directory).is_dir() else set()
    for file_name, stage in STAGE_FILES:
        if file_name in names:
            return stage
    return "pending"


def _make_job(max_mb):
    """复用 run_limited.py 的 Job Object 语义；pywin32 缺失返回 None（不设护栏）。"""
    try:
        import win32job
    except ImportError:
        print("WARNING: pywin32 unavailable; memory cap NOT enforced.", file=sys.stderr)
        return None
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    basic = info["BasicLimitInformation"]
    basic["LimitFlags"] = basic.get("LimitFlags", 0) | 0x00000100 | 0x00002000
    info["ProcessMemoryLimit"] = max_mb * 1024 * 1024
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def _peak_rss_mb(job, max_mb):
    """从 Job Object 读 PeakJobMemoryUsed（字节→MB）；取不到返回 None（policy §4：不许填估计值）。"""
    if job is None:
        return None
    try:
        import win32job
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        peak = info.get("PeakJobMemoryUsed") or info.get("PeakProcessMemoryUsed")
        if peak:
            return round(peak / (1024 * 1024), 1)
    except (ImportError, OSError, KeyError):
        pass
    return None


def run_wrapped(command, max_mb, max_sec, cwd=None, _no_job=False, out_dir=None):
    """T-73 预算护栏运行：时间-内存-阶段三元边界，terminated_by_limit 而非裸 returncode。

    必须用 `communicate()`：`Popen(PIPE) + wait()` 全程不读管道时，子进程输出超过
    管道缓冲（~64KB）就会阻塞在 write 上，被误判成"超时"（历史缺陷，复审点名两次）。
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(command, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"command": command, "returncode": None, "terminated_by_limit": False,
                "limit_kind": None, "peak_rss_mb": None, "elapsed_sec": 0.0,
                "stdout": "", "stderr": str(exc), "stage": "pending"}

    limit_kind = None
    job = None if _no_job else (_make_job(max_mb) if os.name == "nt" else None)
    if job is not None:
        import win32job
        win32job.AssignProcessToJobObject(job, int(proc._handle))  # noqa: SLF001

    peak_mb = None
    try:
        out, err = proc.communicate(timeout=max_sec)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        code = proc.returncode
        limit_kind = "time"
    finally:
        # 峰值必须在 TerminateJobObject **之前**查：terminate 之后进程记录已销毁
        if job is not None:
            peak_mb = _peak_rss_mb(job, max_mb)
            import win32job
            win32job.TerminateJobObject(job, 0)

    out = out or ""
    err = err or ""
    # Job Object 超内存的真实表现（Windows 实测，`已验证`）：
    # 子进程分配 commit 超过 cap 时分配调用直接失败 → MemoryError → exit 1，
    # PeakProcessMemoryUsed 不会超过 cap（失败分配不计入记账）。
    if (
        limit_kind is None
        and job is not None
        and code not in (0, None)
        and "MemoryError" in err
    ):
        limit_kind = "memory"

    return {
        "command": command,
        "returncode": code,
        "terminated_by_limit": limit_kind is not None,
        "limit_kind": limit_kind,
        "peak_rss_mb": peak_mb,  # 取不到必须 None，不许编造
        "elapsed_sec": round(time.monotonic() - started, 3),
        "stdout": out[-4000:],
        "stderr": err[-4000:],
        # 阶段只能从**本次运行自己的输出目录**推断；拿仓库 corpus 去猜是错的
        "stage": infer_stage(out_dir) if out_dir else "unknown",
    }


# ================= T-74 patch =================

PATCHES_JSON = REPO / "tools" / "cfsolve-patches" / "PATCHES.json"
PATCH_DIR = REPO / "tools" / "cfsolve-patches"


def load_patches(path=None):
    path = Path(path) if path else PATCHES_JSON
    if not path.exists():
        raise ResearchError(f"补丁清单不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["patches"] if isinstance(data, dict) else data


def check_patch(patch_path, upstream=None, signature_symbols=None):
    """对 upstream 工作副本 git apply --check + 标志性符号 upstreamed 检测（T-74）。"""
    upstream = Path(upstream) if upstream else Path(resolve_upstream(None))
    result = {"patch": str(patch_path), "status": "applicable", "conflict_files": []}
    symbols = signature_symbols or []
    if symbols:
        hits = sum(1 for sym in symbols
                   if subprocess.run(["git", "grep", "-l", "-F", sym, "--", "src"],
                                     cwd=upstream, capture_output=True, text=True, encoding="utf-8", errors="replace").returncode == 0)
        if hits == len(symbols):
            result["status"] = "upstreamed"
            return result
    if not Path(patch_path).exists():
        result["status"] = "missing"
        return result
    apply_check = subprocess.run(["git", "apply", "--check", str(Path(patch_path).resolve())],
                                 cwd=upstream, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if apply_check.returncode != 0:
        result["status"] = "conflict"
        for line in (apply_check.stderr or "").splitlines():
            if "patch failed:" in line:
                result["conflict_files"].append(line.split("patch failed:", 1)[1].strip())
            elif "error:" in line and ("No such file" in line or "does not apply" in line):
                # "error: <path>: No such file or directory" / "error: <path> does not apply"
                result["conflict_files"].append(line.split("error:", 1)[1].strip())
    return result


def cmd_patch(mode, cfsolve_upstream=None):
    """patch --check / --verify。verify 依赖 T-68 断言集，未落位前报 blocked。"""
    patches = load_patches()
    upstream = resolve_upstream(cfsolve_upstream)
    if mode == "check":
        reports = []
        for p in patches:
            report = check_patch(str(PATCH_DIR / p["file"]), upstream=upstream)
            report["id"] = p["id"]
            reports.append(report)
        return {"mode": "check", "patches": reports}
    if mode == "verify":
        return {"mode": "verify", "status": "blocked",
                "reason": "T-68 结构断言集未落位；verify 依赖可执行断言"}
    raise ResearchError(f"未知 patch 模式: {mode}")


# ================= CLI 子命令分发 =================


def main(argv=None):
    ap = argparse.ArgumentParser(prog="research", description="离线分析台统一入口（T-67/T-68/T-73/T-74）")
    ap.add_argument("--cfsolve-upstream", default=None,
                    help="cfsolve 上游目录（默认回退 D:/cfsolve-upstream，可 env CFSOLVE_UPSTREAM 覆盖）")
    # 子命令也接受 --cfsolve-upstream：doctor 的修复提示把该参数写在子命令之后，
    # 只挂在顶层会让照抄提示的人得到 "unrecognized arguments"。
    # default=SUPPRESS 保证子命令未显式给出时不覆盖顶层已解析的值。
    _common = argparse.ArgumentParser(add_help=False)
    _common.add_argument("--cfsolve-upstream", default=argparse.SUPPRESS,
                         help="cfsolve 上游目录（同顶层参数，两种书写顺序均可）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_a = sub.add_parser("assertions", help="断言矩阵输出", parents=[_common])
    p_a.add_argument("--json", dest="json_out", default=None,
                     help="证据 JSON 输出路径（默认 plans/证据/research-assertions-<日期>.json）")
    sub.add_parser("doctor", help="检查施工前置（G0）", parents=[_common])
    sub.add_parser("build", help="编译 5 个目标 example", parents=[_common])
    p = sub.add_parser("stage2", help="stage2 快照", parents=[_common])
    p.add_argument("corpus_id")
    p = sub.add_parser("vm", help="VM 快照", parents=[_common])
    p.add_argument("corpus_id")
    p.add_argument("--fixture", action="store_true")
    p = sub.add_parser("diff", help="两份快照差分", parents=[_common])
    p.add_argument("id_a")
    p.add_argument("id_b")
    p = sub.add_parser("gaps", help="VM 原语缺口", parents=[_common])
    p.add_argument("corpus_id")
    p.add_argument("--json", action="store_true", dest="as_json")
    p = sub.add_parser("run", help="预算护栏运行（T-73）", parents=[_common])
    p.add_argument("--max-mb", type=int, default=4096)
    p.add_argument("--max-sec", type=int, default=1800)
    p.add_argument("--out-dir", default=None,
                   help="本次运行的会话输出目录（用于阶段推断；不传则 stage=unknown）")
    # 位置参数不能叫 cmd —— 子命令 dest 已经是 cmd，同名会把子命令名覆盖成待执行命令
    p.add_argument("argv", nargs=argparse.REMAINDER, help="要执行的命令（在 -- 之后）")
    p = sub.add_parser("patch", help="补丁集管理（T-74）", parents=[_common])
    p.add_argument("--check", action="store_true")
    p.add_argument("--verify", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "assertions":
        out = args.json_out or (
            EVIDENCE_DIR / f"research-assertions-{datetime.now(timezone.utc):%Y-%m-%d}.json"
        )
        report = assertions_matrix(out_path=out)
        t = report["totals"]
        print(
            f"断言矩阵：pass={t['pass']} fail={t['fail']} skip={t['skip']}"
            + (f"  ({report['skip_reason']})" if report["skip_reason"] else "")
        )
        print(f"断言强度：{report['strength']} —— {report['strength_note']}")
        for v in report["variants"]:
            bad = [k for k, s in v["assertions"].items() if s != "pass"]
            print(f"  {v['id']}: " + ("all pass" if not bad else f"fail/skip: {bad}"))
        return 0 if t["fail"] == 0 else 1

    upstream = getattr(args, "cfsolve_upstream", None)
    try:
        if args.cmd == "doctor":
            report = cmd_doctor(upstream)
            for c in report["checks"]:
                print(f"[{c['status']}] {c['name']}: {c['detail']}")
                if c["status"] == "FAIL":
                    print(f"       修复: {c['fix']}")
            return report["exit_code"]
        if args.cmd == "build":
            return cmd_build(upstream)["returncode"]
        if args.cmd == "stage2":
            print(json.dumps(cmd_stage2(args.corpus_id, upstream), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "vm":
            print(json.dumps(cmd_vm(args.corpus_id, args.fixture, upstream), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "diff":
            report = cmd_diff(args.id_a, args.id_b, upstream)
            print(report["markdown"])
            print(f"已写入: {report['path']}")
            return 0
        if args.cmd == "gaps":
            report = cmd_gaps(args.corpus_id, upstream, args.as_json)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2)
                  if args.as_json else _analyzer("primitive_gap").format_table(report))
            return 0
        if args.cmd == "run":
            argv = [a for a in (args.argv or []) if a != "--"]
            if not argv:
                print("ERROR: 缺少要执行的命令（用法：research.py run -- <cmd...>）",
                      file=sys.stderr)
                return 2
            result = run_wrapped(argv, args.max_mb, args.max_sec, out_dir=args.out_dir)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3 if result["terminated_by_limit"] else 0
        if args.cmd == "patch":
            mode = "verify" if args.verify else "check"
            report = cmd_patch(mode, upstream)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if mode == "check":
                return 0 if all(x["status"] in ("applicable", "upstreamed") for x in report["patches"]) else 1
            return 0
    except ResearchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    # Windows 控制台默认 GBK：中文提示与 ↔/✅ 会乱码或 UnicodeEncodeError。
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
