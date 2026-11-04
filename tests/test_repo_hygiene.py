"""scripts/check_repo_hygiene.py 的验收测试（v6.0 T-69）。

用例（改前必红精神）：
  · 在 tmp git 仓库里跟踪一个 x.sqlite3 → exit 1 且提示的修复命令正确；
  · 干净 tmp git 仓库 → exit 0；
  · 真仓库自检（当前修后状态）→ exit 0。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_repo_hygiene.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    return repo


def test_hygiene_script_flags_tracked_cache_db(tmp_path):
    """tmp git 仓库跟踪 x.sqlite3 → exit 1，修复命令提示正确。"""
    repo = _init_repo(tmp_path)
    (repo / "x.sqlite3").write_bytes(b"fake db")
    _git(repo, "add", "x.sqlite3")
    _git(repo, "commit", "-qm", "add db")

    r = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo,
                       capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 1, f"应 exit 1，实际 {r.returncode}：{out}"
    assert "x.sqlite3" in out
    assert "git rm --cached" in out, "必须给出可执行的修复命令"
    # 命令提示里应包含被命中的文件本身
    assert "x.sqlite3" in out.split("git rm --cached")[1]


def test_hygiene_script_clean_repo_exits_zero(tmp_path):
    """干净仓库（只有源码文件）→ exit 0。"""
    repo = _init_repo(tmp_path)
    (repo / "ok.py").write_text("print('hi')\n")
    _git(repo, "add", "ok.py")
    _git(repo, "commit", "-qm", "init")

    r = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo,
                       capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 0, out + r.stderr.decode(errors="replace")


def test_hygiene_script_flags_env_and_pycache(tmp_path):
    """.env（任何层级）与 __pycache__/ 都必须命中。"""
    repo = _init_repo(tmp_path)
    sub = repo / "pkg" / "__pycache__"
    sub.mkdir(parents=True)
    (repo / ".env").write_text("CF_SOLVE_TOKEN=x\n")
    (sub / "m.pyc").write_bytes(b"\x00")
    _git(repo, "add", "-f", ".env", "pkg/__pycache__/m.pyc")
    _git(repo, "commit", "-qm", "oops")

    r = subprocess.run([sys.executable, str(SCRIPT)], cwd=repo,
                       capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 1
    assert ".env" in out and "__pycache__" in out


def test_hygiene_script_real_repo_is_clean():
    """真仓库（T-69 修后）→ exit 0。这条失败 = 运行产物又被 git add 了。"""
    r = subprocess.run([sys.executable, str(SCRIPT)],
                       cwd=Path(__file__).resolve().parents[1],
                       capture_output=True)
    out = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 0, out + r.stderr.decode(errors="replace")
