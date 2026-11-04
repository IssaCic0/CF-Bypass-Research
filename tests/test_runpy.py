# T-80: run.py 与 run.ps1 双入口一致性 + verify fail-fast 语义
"""锁定两套入口不漂移：run.ps1 的命令集合必须与 run.py 的 COMMANDS 集合相等；
verify 的步骤执行器必须在第一个非零退出码处停止且返回该退出码。"""
import argparse
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_runpy():
    spec = importlib.util.spec_from_file_location("run_entry", ROOT / "run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_runpy_stop_matches_port_exactly(monkeypatch):
    """`stop 8878` 不许命中 `:88780`（子串匹配会误杀另一个服务）。"""
    run = _load_runpy()
    netstat_out = (
        "  TCP    0.0.0.0:88780    0.0.0.0:0    LISTENING    1111\n"
        "  TCP    127.0.0.1:8878   0.0.0.0:0    LISTENING    2222\n"
    )

    class _R:
        stdout = netstat_out

    monkeypatch.setattr(run.subprocess, "run", lambda *a, **k: _R())
    killed = []
    monkeypatch.setattr(run.subprocess, "call", lambda cmd: killed.append(cmd) or 0)
    monkeypatch.setattr(run, "read_env_token", lambda: "")
    class _FakeReq:
        def add_header(self, *a, **k):
            pass

    class _FakeUrlreq:
        @staticmethod
        def Request(*a, **k):
            return _FakeReq()

        @staticmethod
        def urlopen(*a, **k):
            raise OSError("no server")

    monkeypatch.setattr(run, "urlreq", _FakeUrlreq)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    rc = run.cmd_stop(argparse.Namespace(port=8878))
    assert rc == 0
    assert killed, "应当终止监听 8878 的进程"
    assert all("2222" in " ".join(map(str, c)) for c in killed), f"误杀了别的 PID: {killed}"


def test_runpy_targets_match_runps1():
    """解析 run.ps1 的 ValidateSet（转发契约）与 run.py 的 COMMANDS 键，集合相等。"""
    ps1_text = (ROOT / "run.ps1").read_text(encoding="utf-8-sig")
    m = re.search(r"\[ValidateSet\((.*?)\)\]", ps1_text)
    assert m, "run.ps1 中找不到 [ValidateSet('...')] 命令清单"
    ps1_cmds = set(re.findall(r"'(\w+)'", m.group(1)))

    src = (ROOT / "run.py").read_text(encoding="utf-8")
    m2 = re.search(r"^COMMANDS\s*=\s*\{(.*?)^\}", src, re.M | re.S)
    assert m2, "run.py 中找不到 COMMANDS 字典"
    runpy_cmds = set(re.findall(r"'(\w+)'\s*:", m2.group(1)))

    assert ps1_cmds == runpy_cmds, (
        f"两套入口漂移！仅 run.ps1 有: {sorted(ps1_cmds - runpy_cmds)}；"
        f"仅 run.py 有: {sorted(runpy_cmds - ps1_cmds)}"
    )


def test_runpy_verify_fails_fast_on_first_failure():
    """假目标返回非 0 → 后续步骤不执行，退出码等于第一个失败步骤的退出码。"""
    mod = _load_runpy()
    executed = []

    def make(name, rc):
        def fn():
            executed.append(name)
            return rc
        return fn

    steps = [
        ("step-a-ok", make("step-a-ok", 0)),
        ("step-b-fail", make("step-b-fail", 3)),
        ("step-c-never", make("step-c-never", 0)),
    ]
    rc = mod.run_steps(steps)
    assert executed == ["step-a-ok", "step-b-fail"], f"fail-fast 失效，实际执行: {executed}"
    assert rc == 3, f"退出码应为第一个失败步骤的 3，实际 {rc}"
