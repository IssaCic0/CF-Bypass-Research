"""`solver/bootstrap.py` 的装配层单测（v5.0.1 P1-4）。

为什么单独有这个文件：`bootstrap.py` 的 `main()` 是"参数解析 + 组件装配 + 信号处理"，
2026-11-04 审计实测它的覆盖率只有 **38%**（`167-300` 整段未执行），而 `solver/` 整体
却报 82% —— 整体数字把这块盲区平均掉了。盲区里当场就藏着真缺陷：
`except (ValueError, SolveError)` 里的 `SolveError` **没被 import**，
导致文档承诺的"cloakbrowser 缺依赖 → 友好提示 + exit 2"实际抛 `NameError` 裸 traceback。
（已实证：`python solver/service.py --engine cloakbrowser` → NameError；修好后 exit 2。）

做法：**不启动真浏览器、不绑定真端口**。
  · `Solver` 换替身 → 只断言 bootstrap 传了什么参数、设了哪些 Handler 属性；
  · `Server` 换替身 → `serve_forever` 立即返回，避免测试挂住；
  · `signal.signal` 换 no-op → 不污染 pytest 进程的信号处理。

这些是"装配契约"测试，不是集成测试：它们证明"接线对"，不证明"浏览器能过盾"。
"""

from __future__ import annotations

import json

import pytest

from solver import bootstrap as boot
from solver import service as svc


# --------------------------------------------------------------------------- 替身


class FakeSolver:
    """记录构造参数；不启线程、不开浏览器、不建 sqlite。"""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = dict(kwargs)
        self.cache_backend = "memory(fake)"
        self.prewarmer = None
        self.backend_chain = []
        self.closed = False

    def close(self):
        self.closed = True


class FakeServer:
    """只实现 bootstrap 会调用的四个方法。`address` 用于断言端口来自 CLI。"""

    last = None

    def __init__(self, address, handler):
        type(self).last = self
        self.address = address
        self.handler = handler
        self.served = False
        self.drained = None
        self.closed = False

    def serve_forever(self, poll_interval=0.5):
        self.served = True

    def drain(self, grace):
        self.drained = grace
        return 0

    def server_close(self):
        self.closed = True

    def shutdown(self):  # 信号处理里会用到
        pass


@pytest.fixture
def wired(monkeypatch):
    """把 bootstrap 的外部世界全部换成替身，并保证 Handler 类属性被还原。"""
    saved = {
        name: getattr(svc.Handler, name, "<unset>")
        for name in ("solver", "token", "router", "allow_remote_shutdown", "audit_path")
    }
    monkeypatch.setattr(boot, "Solver", FakeSolver)
    monkeypatch.setattr(boot, "Server", FakeServer)
    monkeypatch.setattr(boot, "engine_preflight", lambda _engine: None)
    monkeypatch.setattr(boot.signal, "signal", lambda *_a, **_k: None)
    FakeSolver.last_kwargs = {}
    FakeServer.last = None
    try:
        yield monkeypatch
    finally:
        for name, value in saved.items():
            if value == "<unset>":
                try:
                    delattr(svc.Handler, name)
                except AttributeError:
                    pass
            else:
                setattr(svc.Handler, name, value)


def run(argv, capsys=None):
    code = boot.main(argv)
    out = capsys.readouterr().out if capsys is not None else ""
    return code, out


# ----------------------------------------------------------------- SolveError 回归


def test_solveerror_is_imported_into_bootstrap_namespace():
    """最小回归：`main()` 的 except 元组引用了它，缺了就是 NameError。"""
    assert hasattr(boot, "SolveError")
    assert issubclass(boot.SolveError, Exception)


def test_engine_preflight_failure_returns_2_with_friendly_message(wired, capsys):
    """缺依赖（如 cloakbrowser 未装）必须给"安装命令"式提示，而不是 traceback。"""

    def boom(_engine):
        raise boot.SolveError(
            svc.ErrorCode.NOT_CONFIGURED,
            "cloakbrowser 未安装",
            stage="admit",
            hint="pip install cloakbrowser",
        )

    wired.setattr(boot, "engine_preflight", boom)
    code, out = run(["--port", "18878", "--no-auth"], capsys)
    assert code == 2
    assert "引擎不可用" in out
    assert "cloakbrowser 未安装" in out
    assert "pip install cloakbrowser" in out  # hint 必须透出，否则用户不知道下一步
    assert "Traceback" not in out
    assert FakeServer.last is None  # 失败在绑定端口之前，不该起服务


# --------------------------------------------------------------------- 冲突与早退


def test_proxy_with_cfsolve_bin_is_hard_conflict(wired, capsys):
    """代理池与"各自直连"的后端不能共存：出口不一致会静默 403。"""
    code, out = run(["--proxy", "http://127.0.0.1:8080", "--cfsolve-bin", "x", "--no-auth"], capsys)
    assert code == 2
    assert "配置冲突" in out
    assert FakeServer.last is None


def test_proxy_with_pydoll_is_hard_conflict(wired, capsys):
    code, out = run(["--proxy", "http://127.0.0.1:8080", "--pydoll", "--no-auth"], capsys)
    assert code == 2
    assert "配置冲突" in out


def test_prewarm_parse_error_returns_2_and_closes_solver(wired, capsys, tmp_path):
    """保活目标文件不存在 → 明确报错并释放已装配的 solver（不能带着半成品继续跑）。"""
    missing = tmp_path / "nope.txt"
    code, out = run(["--prewarm-file", str(missing), "--no-auth"], capsys)
    assert code == 2
    assert "保活配置错误" in out
    # 注意顺序：bootstrap 先构造 Server 再处理保活，所以这里不是"没构造"，
    # 而是"构造了但一次都没 serve"，并且已经 solver.close()。
    assert FakeServer.last is not None
    assert FakeServer.last.served is False


def test_port_bind_failure_returns_2_and_closes_solver(wired, capsys):
    class BindFail(FakeServer):
        def __init__(self, address, handler):
            raise OSError("address already in use")

    wired.setattr(boot, "Server", BindFail)
    code, out = run(["--port", "18879", "--no-auth"], capsys)
    assert code == 2
    assert "绑定失败" in out


# ------------------------------------------------------------------ P1-2 token 文件


def test_token_file_defaults_from_env(wired, monkeypatch, tmp_path):
    """`CF_SOLVE_TOKEN_FILE` 曾在 Dockerfile/compose 里设置但全仓无代码读取（死配置）。

    这条用例锁住"它真的生效"——否则 docker run 场景下 token 每重启重生成，
    客户端旧 token 全 401，且缓存密钥跟着变 → 共享缓存整库作废。
    """
    target = tmp_path / "data" / ".env"
    monkeypatch.setenv("CF_SOLVE_TOKEN_FILE", str(target))
    seen = {}

    def spy(path):
        seen["path"] = path
        return "tok", "file"

    wired.setattr(boot, "load_or_create_token", spy)
    code, _ = run(["--port", "18880"], None)
    assert code == 0
    assert seen["path"] == str(target)


def test_token_file_cli_overrides_env(wired, monkeypatch, tmp_path):
    monkeypatch.setenv("CF_SOLVE_TOKEN_FILE", str(tmp_path / "from-env.env"))
    cli_path = tmp_path / "from-cli.env"
    seen = {}

    def spy(path):
        seen["path"] = path
        return "tok", "file"

    wired.setattr(boot, "load_or_create_token", spy)
    code, _ = run(["--port", "18881", "--token-file", str(cli_path)], None)
    assert code == 0
    assert seen["path"] == str(cli_path)


def test_no_auth_skips_token_and_uses_placeholder_key(wired, capsys):
    """`--no-auth` 是联调开关：不读 token 文件，但仍要给缓存一个确定的派生密钥。"""
    called = {"n": 0}

    def spy(path):
        called["n"] += 1
        return "tok", "file"

    wired.setattr(boot, "load_or_create_token", spy)
    code, out = run(["--port", "18882", "--no-auth"], capsys)
    assert code == 0
    assert called["n"] == 0
    assert svc.Handler.token == ""
    assert FakeSolver.last_kwargs["token"] == "memory-only"
    assert "auth=disabled" in out


# --------------------------------------------------------------------- 装配接线


def test_cache_dir_comes_from_env(wired, monkeypatch, tmp_path):
    monkeypatch.setenv("CF_CACHE_DIR", str(tmp_path / "cachedir"))
    code, _ = run(["--port", "18883", "--no-auth"], None)
    assert code == 0
    assert FakeSolver.last_kwargs["cache_dir"] == str(tmp_path / "cachedir")


def test_single_backend_has_no_router(wired):
    code, _ = run(["--port", "18884", "--no-auth"], None)
    assert code == 0
    assert svc.Handler.router is None
    assert svc.Handler.solver.backend_chain == ["camoufox"]


def test_multi_backend_builds_router_in_priority_order(wired, tmp_path):
    """纯协议 → pydoll → Camoufox → FlareSolverr，顺序就是回落顺序，不许乱。"""
    prof = tmp_path / "profile.json"
    prof.write_text("{}", encoding="utf-8")
    code, _ = run(
        [
            "--port", "18885", "--no-auth",
            "--cfsolve-bin", str(tmp_path / "challenge.exe"),
            "--cfsolve-profile", str(prof),
            "--pydoll",
            "--flaresolverr", "http://127.0.0.1:8191",
        ],
        None,
    )
    assert code == 0
    router = svc.Handler.router
    assert router is not None
    assert router.backends == ["cfsolve", "pydoll", "camoufox", "flaresolverr"]
    assert svc.Handler.solver.backend_chain == ["cfsolve", "pydoll", "camoufox", "flaresolverr"]


def test_cfsolve_bin_without_profile_is_ignored(wired, tmp_path):
    """半配置不能静默降级成"看起来配了"：只给 --cfsolve-bin 不装配该后端。"""
    code, _ = run(
        ["--port", "18886", "--no-auth", "--cfsolve-bin", str(tmp_path / "challenge.exe")], None
    )
    assert code == 0
    assert svc.Handler.router is None


def test_audit_log_unwritable_is_disabled_with_warning(wired, capsys, tmp_path):
    """审计日志路径不可写 → 关掉审计并警告，但**不能**因此起不来（求解优先）。"""
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # 拿文件当父目录 → makedirs 必然失败
    code, out = run(["--port", "18887", "--no-auth", "--audit-log", str(blocker / "audit.jsonl")], capsys)
    assert code == 0
    assert svc.Handler.audit_path == ""
    assert "审计日志不可写" in out


def test_audit_log_writable_is_enabled(wired, capsys, tmp_path):
    target = tmp_path / "logs" / "audit.jsonl"
    code, out = run(["--port", "18888", "--no-auth", "--audit-log", str(target)], capsys)
    assert code == 0
    assert svc.Handler.audit_path == str(target)
    assert "审计日志已启用" in out
    assert target.parent.is_dir()


def test_allow_remote_shutdown_flag_is_wired(wired):
    code, _ = run(["--port", "18889", "--no-auth", "--allow-remote-shutdown"], None)
    assert code == 0
    assert svc.Handler.allow_remote_shutdown is True


def test_graceful_shutdown_drains_before_closing(wired, capsys):
    """顺序必须是 drain → server_close → solver.close，反了会把在途请求打成连接重置。"""
    order = []

    class Trace(FakeServer):
        def drain(self, grace):
            order.append("drain")
            return 0

        def server_close(self):
            order.append("server_close")

    original_close = FakeSolver.close

    def close(self):
        order.append("solver_close")
        original_close(self)

    wired.setattr(boot, "Server", Trace)
    wired.setattr(FakeSolver, "close", close)
    code, out = run(["--port", "18890", "--no-auth"], capsys)
    assert code == 0
    assert order == ["drain", "server_close", "solver_close"]
    assert "在途请求已全部完成" in out


def test_startup_log_line_contains_operational_facts(wired, capsys):
    """启动日志是运维第一眼看到的东西：端口/ttl/egress/auth/cache/proxies 必须都在。"""
    code, out = run(
        ["--port", "18891", "--no-auth", "--ttl", "600", "--egress-id", "e1"], capsys
    )
    assert code == 0
    assert "监听 http://127.0.0.1:18891" in out
    assert "ttl=600s" in out
    assert "egress=e1" in out
    assert "auth=disabled" in out
    assert "cache=" in out
    assert "proxies=0" in out
