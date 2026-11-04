"""T-67/T-73/T-74 验收测试：scripts/research.py 全部离线，tmp_path + monkeypatch。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import research  # noqa: E402

TARGET_EXES = list(research.TARGET_BINARIES)


def _fake_toolchain(tmp_path: Path, monkeypatch) -> Path:
    """造一个假 upstream：cargo 假 exe + 5 个目标二进制 + profiles + corpus 可写。"""
    upstream = tmp_path / "cfsolve-upstream"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    target = upstream / "target" / "release"
    target.mkdir(parents=True)
    for exe in TARGET_EXES:
        (target / f"{exe}.exe").write_bytes(b"MZ fake")
    (upstream / "profiles").mkdir()
    (upstream / "profiles" / "safari-26-ios.json").write_text("{}", encoding="utf-8")
    fake_cargo = bin_dir / ("cargo.exe" if os.name == "nt" else "cargo")
    fake_cargo.write_text("@echo ok", encoding="utf-8")
    monkeypatch.setattr(research, "find_cargo", lambda: str(fake_cargo))
    # doctor 的 cmake / nasm 探测不能让用例结果取决于宿主机装没装这两样。
    monkeypatch.setattr(research.shutil, "which", lambda name: str(bin_dir / f"{name}.exe"))
    return upstream


def _stale_cache(upstream: Path, generator: str) -> Path:
    """在假 upstream 里造一份指定 generator 的 btls-sys CMakeCache。"""
    out_build = upstream / "target" / "release" / "build" / "btls-sys-deadbeef" / "out" / "build"
    out_build.mkdir(parents=True)
    cache = out_build / "CMakeCache.txt"
    cache.write_text(
        "# For build in directory: fake\n"
        "CMAKE_BUILD_TYPE:STRING=Release\n"
        f"CMAKE_GENERATOR:INTERNAL={generator}\n",
        encoding="utf-8",
    )
    return out_build


# ---------- T-67 doctor ----------

def test_research_doctor_fails_without_cargo(tmp_path, monkeypatch):
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    monkeypatch.setattr(research, "find_cargo", lambda: None)
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] != 0
    names = {c["name"] for c in report["checks"]}
    assert "cargo" in names
    cargo = [c for c in report["checks"] if c["name"] == "cargo"][0]
    assert cargo["status"] == "FAIL"
    assert "rustup" in cargo["fix"].lower() or "cargo" in cargo["fix"].lower()


def test_research_doctor_passes_with_fake_toolchain(tmp_path, monkeypatch):
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] == 0, json.dumps(report, ensure_ascii=False)
    assert all(c["status"] == "PASS" for c in report["checks"])


def test_research_doctor_reports_missing_binary_with_fix(tmp_path, monkeypatch):
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    (upstream / "target" / "release" / "vocabulary.exe").unlink()
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] != 0
    failed = [c for c in report["checks"] if c["status"] == "FAIL"]
    assert any("vocabulary" in c["name"] for c in failed)
    assert all("build" in c["fix"] or "cargo" in c["fix"] for c in failed)


def test_doctor_flags_stale_cmake_cache_generator_conflict(tmp_path, monkeypatch):
    """本轮实测故障的回归：残留 Ninja CMakeCache 会让 cargo 在 btls-sys 上 panic。

    doctor 必须在 build 之前说清楚，并给出待删目录（不许让用户去读 cargo 栈）。
    """
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    out_build = _stale_cache(upstream, "Ninja")
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    if sys.platform != "win32":
        pytest.skip("generator 冲突只在 Windows（VS generator 强制）上成立")
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] != 0
    entry = [c for c in report["checks"] if c["name"] == "cmake_cache"]
    assert entry, json.dumps(report, ensure_ascii=False)
    assert entry[0]["status"] == "FAIL"
    assert "Ninja" in entry[0]["detail"]
    assert str(out_build) in entry[0]["fix"]


def test_doctor_reports_clean_when_cache_generator_matches(tmp_path, monkeypatch):
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    _stale_cache(upstream, "Visual Studio 18 2026")
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    entry = [c for c in report["checks"] if c["name"] == "cmake_cache"][0]
    assert entry["status"] == "PASS", entry


def test_doctor_flags_missing_cmake(tmp_path, monkeypatch):
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    monkeypatch.setattr(
        research.shutil, "which",
        lambda name: None if name == "cmake" else str(tmp_path / f"{name}.exe"),
    )
    monkeypatch.setenv("CFSOLVE_UPSTREAM", str(upstream))
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] != 0
    entry = [c for c in report["checks"] if c["name"] == "cmake"][0]
    assert entry["status"] == "FAIL"
    assert "cmake" in entry["fix"].lower()


def test_doctor_finds_example_binaries_in_examples_subdir(tmp_path, monkeypatch):
    """cargo build --example 的产物在 target/release/examples/ 下（本轮实测）。

    只在 release/ 找会让"构建成功"仍然报缺失，G0 永远过不了。
    """
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    target = upstream / "target" / "release"
    ex_dir = target / "examples"
    ex_dir.mkdir()
    for exe in TARGET_EXES:
        (target / f"{exe}.exe").unlink()
        (ex_dir / f"{exe}.exe").write_bytes(b"MZ fake")
    report = research.cmd_doctor(cfsolve_upstream=str(upstream))
    assert report["exit_code"] == 0, json.dumps(report, ensure_ascii=False)
    assert all(c["status"] == "PASS" for c in report["checks"])
    assert research._binary(upstream, TARGET_EXES[0]).parent.name == "examples"


def test_build_injects_libclang_and_nasm_env(tmp_path, monkeypatch):
    """btls-sys 的构建前置（libclang / nasm）必须由 build 自动注入。

    本轮实测链条：generator 冲突 → libclang 缺失 → nasm 不在 PATH。三次都是
    环境问题却表现为 cargo panic；注入后用户只需一条 build 命令。
    """
    upstream = _fake_toolchain(tmp_path, monkeypatch)
    dll_dir = tmp_path / "llvm" / "bin"
    dll_dir.mkdir(parents=True)
    (dll_dir / "libclang.dll").write_bytes(b"MZ fake")
    nasm_dir = tmp_path / "nasm"
    nasm_dir.mkdir()
    (nasm_dir / "nasm.exe").write_bytes(b"MZ fake")
    monkeypatch.delenv("LIBCLANG_PATH", raising=False)
    monkeypatch.setattr(research, "_find_libclang", lambda: str(dll_dir / "libclang.dll"))
    monkeypatch.setattr(research, "_find_nasm", lambda: str(nasm_dir / "nasm.exe"))

    seen = {}

    def _spy(cmd, *a, **k):
        seen["env"] = k.get("env") or {}

        class _R:
            returncode = 0

        return _R()

    monkeypatch.setattr(research.subprocess, "run", _spy)
    report = research.cmd_build(cfsolve_upstream=str(upstream))
    assert report["returncode"] == 0
    assert seen["env"]["LIBCLANG_PATH"] == str(dll_dir)
    assert "LIBCLANG_PATH" in report["env_injected"]
    if sys.platform == "win32":
        assert str(nasm_dir) in seen["env"]["PATH"]


def test_research_build_refuses_without_upstream(tmp_path, monkeypatch):
    monkeypatch.delenv("CFSOLVE_UPSTREAM", raising=False)
    # 隔离本机真实 D:\cfsolve-upstream：把默认路径指到不存在的 tmp 目录
    monkeypatch.setattr(research, "DEFAULT_UPSTREAM", str(tmp_path / "no-such-upstream"))
    monkeypatch.setattr(research, "find_cargo", lambda: "cargo")
    calls = []
    real_run = research.subprocess.run

    def _spy(cmd, *a, **k):
        calls.append(cmd)
        if "cargo" in str(cmd[0]):
            raise AssertionError(f"cargo 被调用: {cmd}")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(research.subprocess, "run", _spy)
    with pytest.raises(research.ResearchError) as exc:
        research.cmd_build(cfsolve_upstream=None)
    assert "cfsolve" in str(exc.value).lower()
    assert not any("cargo" in str(c[0]) for c in calls)


# ---------- T-73 阶段推断 ----------

STAGES_TABLE = [
    ([], "pending"),
    (["iframe.js"], "captured"),
    (["iframe.js", "iframe.deobfuscated.js"], "deobfuscated"),
    (["iframe.js", "iframe.deobfuscated.js", "response.decoded.txt"], "payload_sent"),
    (["iframe.js", "iframe.deobfuscated.js", "response.decoded.txt", "second-response.decoded.txt"], "stage2_received"),
]


@pytest.mark.parametrize("files,expected", STAGES_TABLE, ids=[e for _, e in STAGES_TABLE])
def test_stage_inference_from_directory_files(tmp_path, files, expected):
    for name in files:
        (tmp_path / name).write_text("x", encoding="utf-8")
    assert research.infer_stage(tmp_path) == expected


def _run_limited_case(tmp_path, max_mb=None, max_sec=None):
    """返回 (result, script_path)。script 是一个可控的吃资源进程。"""
    if os.name != "nt":
        pytest.skip("run_limited.py Job Object 分支 Windows 专属（G3 WSL 复跑再验）")
    script = tmp_path / "worker.py"
    return script


def test_run_wrapper_enforces_memory_limit(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows Job Object")
    script = tmp_path / "hog.py"
    # 持续增长分配：绕过 Python 前台 MemoryError，让 Job Object 的
    # PROCESS_MEMORY 上限（64MB）触发杀进程（returncode 为负）
    script.write_text(
        "import time\nchunks = []\nwhile True:\n"
        "    chunks.append(bytearray(4 * 1024 * 1024))\n    time.sleep(0.01)\n",
        encoding="utf-8",
    )
    result = research.run_wrapped(
        [sys.executable, str(script)], max_mb=64, max_sec=60, cwd=str(tmp_path)
    )
    assert result["terminated_by_limit"] is True
    assert result["limit_kind"] == "memory"


def test_run_wrapper_enforces_time_limit(tmp_path):
    if os.name != "nt":
        pytest.skip("run_limited.py 当前为 Windows 实现")
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    result = research.run_wrapped(
        [sys.executable, str(script)], max_mb=512, max_sec=3, cwd=str(tmp_path)
    )
    assert result["terminated_by_limit"] is True
    assert result["limit_kind"] == "time"


def test_peak_rss_is_null_not_fabricated(tmp_path):
    # 无 Job Object（非 Windows / pywin32 缺失）时没有可靠来源 → 必须是 None 不是编造数字
    script = tmp_path / "ok.py"
    script.write_text("print('done')\n", encoding="utf-8")
    result = research.run_wrapped(
        [sys.executable, str(script)], max_mb=512, max_sec=30, cwd=str(tmp_path),
        _no_job=True,
    )
    assert result["peak_rss_mb"] is None


def test_run_wrapper_clean_exit_not_terminated(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows 分支")
    script = tmp_path / "ok.py"
    script.write_text("print('done')\n", encoding="utf-8")
    result = research.run_wrapped(
        [sys.executable, str(script)], max_mb=512, max_sec=30, cwd=str(tmp_path)
    )
    assert result["terminated_by_limit"] is False
    assert result["returncode"] == 0


# ---------- T-74 补丁清单 ----------

def test_patch_manifest_every_patch_has_assertions():
    patches = research.load_patches()
    assert len(patches) >= 2
    for p in patches:
        assert p.get("assertion_ids"), f"patch {p.get('id')} 缺 assertion_ids"
        assert p.get("id") and p.get("files") and p.get("upstream_commit")
        assert p["status"] in ("applied", "superseded", "upstreamed")


def test_patch_manifest_files_exist():
    root = Path(__file__).resolve().parent.parent / "tools" / "cfsolve-patches"
    for p in research.load_patches():
        assert (root / p["file"]).exists(), p["id"]


def _tmp_upstream(tmp_path: Path) -> Path:
    """造一个真 git 仓库当假 upstream。"""
    repo = tmp_path / "upstream-repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    (repo / "src").mkdir()
    (repo / "src" / "a.rs").write_text("fn alpha() {}\n", encoding="utf-8")
    (repo / "src" / "b.rs").write_text("fn beta() {}\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return repo


def test_patch_check_detects_conflict(tmp_path):
    repo = _tmp_upstream(tmp_path)
    (repo / "src" / "a.rs").write_text("fn alpha_changed_totally() {}\n", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-aqm", "change"],
        cwd=repo, check=True, capture_output=True,
    )
    patch = tmp_path / "conflict.patch"
    patch.write_text(
        textwrap.dedent("""\
        --- a/src/a.rs
        +++ b/src/a.rs
        @@ -1,1 +1,2 @@
         fn alpha() {}
        +fn gamma() {}
        """),
        encoding="utf-8",
    )
    report = research.check_patch(str(patch), upstream=repo)
    assert report["status"] == "conflict"
    assert report["conflict_files"], "冲突必须给出文件（含行号信息）"


def test_patch_check_detects_upstreamed(tmp_path):
    repo = _tmp_upstream(tmp_path)
    report = research.check_patch(
        "whatever.patch",
        upstream=repo,
        signature_symbols=["fn alpha()"],
    )
    assert report["status"] == "upstreamed"


def test_patch_check_detects_applicable(tmp_path):
    repo = _tmp_upstream(tmp_path)
    patch = tmp_path / "good.patch"
    patch.write_text(
        textwrap.dedent("""\
        --- a/src/a.rs
        +++ b/src/a.rs
        @@ -1,1 +1,2 @@
         fn alpha() {}
        +fn gamma() {}
        """),
        encoding="utf-8",
    )
    report = research.check_patch(str(patch), upstream=repo, signature_symbols=["fn zzz_not_present()"])
    assert report["status"] == "applicable"
