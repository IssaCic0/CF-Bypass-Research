"""求解服务的自检工具（doctor）—— 回答"这台机器到底能不能跑"。

为什么需要它（替代 README 里的七步手工自检）：
  · 这套栈的启动失败模式几乎都不是"代码报错"，而是**环境**：camoufox 二进制没 fetch、
    端口被占（Windows 上 SO_REUSEADDR 还会静默串台）、cache 目录不可写、token 缺失、
    纯协议二进制没配导致"以为走了快路线其实一直在跑浏览器"。
  · 这些都属于"能在 5 秒内查出来，却平均要花 40 分钟排查"的东西。

用法：
    python scripts/doctor.py                # 全量自检（会起一次真实的临时服务）
    python scripts/doctor.py --offline      # 只查环境，不启动服务
    python scripts/doctor.py --port 8878    # 指定端口（默认 8878）
    python scripts/doctor.py --json         # 机器可读输出（给 CI / 部署脚本用）
    python scripts/doctor.py --offline --proxy http://u:p@h:8080 --verify-egress
                                            # 实测出口 IP（**唯一会出站的组合**，见下）

`--offline` 只表示"不启动服务"。默认全量自检会起一次临时服务，但那也是本机回环。
**整个工具默认不出站**；唯有显式加 `--verify-egress` 才会经每个代理请求一次 IP 回显。

退出码：0=全绿 / 1=有可降级项（能用但非最优）/ 2=有阻断项（起不来）
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "solver"))

OK = "ok"
WARN = "warn"
FAIL = "fail"


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, name: str, level: str, detail: str, hint: str = "") -> None:
        self.checks.append({"name": name, "level": level, "detail": detail, "hint": hint})

    def worst(self) -> str:
        if any(c["level"] == FAIL for c in self.checks):
            return FAIL
        if any(c["level"] == WARN for c in self.checks):
            return WARN
        return OK

    def exit_code(self) -> int:
        return {OK: 0, WARN: 1, FAIL: 2}[self.worst()]

    def render(self) -> str:
        icon = _icons()
        lines = []
        for c in self.checks:
            lines.append(f"{icon[c['level']]} {c['name']}: {c['detail']}")
            if c["hint"] and c["level"] != OK:
                lines.append(f"    → {c['hint']}")
        lines.append("")
        lines.append(f"结论：{self.worst().upper()}（exit={self.exit_code()}）")
        return "\n".join(lines)


def _icons() -> dict[str, str]:
    """按控制台实际编码挑图标。

    为什么不能直接写 ✓/⚠/✗：Windows 的默认控制台是 GBK（cp936），
    打印这几个字符会抛 UnicodeEncodeError —— 一个自检工具自己崩掉是最尴尬的失败模式。
    """
    enc = (getattr(sys.stdout, "encoding", "") or "ascii").lower()
    try:
        "✓⚠✗→".encode(enc)
        return {OK: "✓", WARN: "⚠", FAIL: "✗"}
    except Exception:  # noqa: BLE001
        return {OK: "[OK]", WARN: "[WARN]", FAIL: "[FAIL]"}


def _force_utf8_stdout() -> None:
    """尽量把标准输出切到 UTF-8，避免中文 detail 在 GBK 控制台上变乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def check_python(rep: Report) -> None:
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) >= (3, 10):
        rep.add("python", OK, ver)
    else:
        rep.add("python", FAIL, ver, "需要 Python ≥ 3.10（用了 X | Y 类型语法）")


def check_module(rep: Report, module: str, level_if_missing: str, hint: str) -> None:
    try:
        mod = __import__(module)
        rep.add(module, OK, _module_version(mod) or "已安装")
    except Exception as e:  # noqa: BLE001
        rep.add(module, level_if_missing, f"缺失（{type(e).__name__}）", hint)


def _module_version(mod) -> str:
    """稳妥地取版本号。

    踩过的坑：camoufox 把 `__version__` 做成了一个**模块**（`camoufox/__version__.py`），
    直接 `str(getattr(mod, "__version__"))` 会让后续 `.startswith()` 抛 AttributeError，
    于是一个装得好好的依赖被报告成"缺失"。取版本必须容忍非字符串。
    """
    raw = getattr(mod, "__version__", "")
    if isinstance(raw, str) and raw and not raw.startswith("<"):
        return raw
    inner = getattr(raw, "__version__", "")
    if isinstance(inner, str) and inner:
        return inner
    return ""


def check_camoufox_binary(rep: Report) -> None:
    """camoufox 的 Firefox 二进制必须单独 fetch，缺了会在第一次求解才炸。"""
    try:
        from camoufox.pkgman import installed_verstr  # type: ignore

        ver = installed_verstr()
        rep.add("camoufox 二进制", OK, str(ver))
    except Exception as e:  # noqa: BLE001
        rep.add(
            "camoufox 二进制",
            WARN,
            f"未确认（{type(e).__name__}: {str(e)[:60]}）",
            "python -m camoufox fetch",
        )


def check_port(rep: Report, port: int) -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
        rep.add(f"端口 {port}", OK, "可用")
    except OSError as e:
        rep.add(f"端口 {port}", FAIL, f"被占用（{e}）", f"netstat -ano | findstr :{port} 找到进程后停掉，或换 --port")
    finally:
        s.close()


def check_cache_dir(rep: Report) -> None:
    d = tempfile.mkdtemp(prefix="cfsolve-doctor-")
    try:
        from cache import SqliteStore  # type: ignore

        store = SqliteStore(d, "doctor-token", max_files=8)
        store.set("doctor|default", {"cf_clearance": "X", "expires_at": time.time() + 60})
        got = store.get("doctor|default")
        rep.add("落盘缓存(SQLite/WAL)", OK if got else FAIL, "写读往返正常" if got else "写入后读不到")
        rep.add("缓存实现", OK, f"sqlite（{os.path.join(d, 'cache.sqlite3')}）")
    except Exception as e:  # noqa: BLE001
        rep.add("落盘缓存(SQLite/WAL)", WARN, f"不可用（{type(e).__name__}: {str(e)[:80]}）",
                "将降级为 FileStore；检查目录写权限与 sqlite3 是否可用")
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def check_token(rep: Report, token_file: str) -> None:
    env = (os.environ.get("CF_SOLVE_TOKEN") or "").strip()
    if env:
        rep.add("鉴权 token", OK, f"来自环境变量（长度 {len(env)}）")
        return
    path = ROOT / token_file
    if path.exists():
        rep.add("鉴权 token", OK, f"来自 {token_file}（文件存在）")
    else:
        rep.add("鉴权 token", WARN, f"{token_file} 不存在", "首次启动会自动生成；或显式设置 CF_SOLVE_TOKEN")


def check_optional_backends(rep: Report) -> None:
    """纯协议/pydoll 是"可选但最优"的路线，缺了不阻断，但要说清楚。"""
    bin_path = (os.environ.get("CFSOLVE_BIN") or "").strip()
    prof = (os.environ.get("CFSOLVE_PROFILE") or "").strip()
    if bin_path and prof and Path(bin_path).is_file() and Path(prof).is_file():
        rep.add("纯协议后端 cfsolve", OK, "已配置")
    else:
        rep.add("纯协议后端 cfsolve", WARN, "未配置",
                "docs/纯协议-cfsolve.md；缺它就只能走浏览器（每次 16-20s + 1.2GB）")

    try:
        from pydoll_backend import find_chrome  # type: ignore

        chrome = find_chrome(os.environ.get("CHROME_PATH", ""))
        if chrome:
            rep.add("pydoll/Chrome", OK, str(chrome))
        else:
            rep.add("pydoll/Chrome", WARN, "未找到 Chrome", "装 Chrome 或设置 CHROME_PATH；--pydoll 会被 skip")
    except Exception as e:  # noqa: BLE001
        rep.add("pydoll/Chrome", WARN, f"不可用（{type(e).__name__}）", "pip install pydoll-python")


def check_proxy_pool(rep: Report, proxies: list[str], registered: list[str],
                     *, verify: bool, echo_url: str, timeout: float) -> None:
    """代理池自检（v6.3，参考 proxypool 的 tester 思路）。

    两件事分开报：
      1. **配置**（离线可查）：地址能不能解析、有没有登记出口 IP。缺登记只是 WARN ——
         没登记也能跑，只是退回 `px:<地址哈希>`，代价是"同一 IP 的不同入口不能共享缓存"。
      2. **实测**（`--verify-egress` 才做）：真的经每个代理请求一次 IP 回显，
         与登记值比对。**不一致是 FAIL** —— 那是缓存键错位、凭证发给错出口的源头。

    为什么默认不实测：它会出站。虽然 IP 回显不计入真机预算（用户 2026-11-05 拍板），
    但 doctor 是"5 秒查完环境"的工具，默认不该有网络等待。
    """
    if not proxies:
        return
    from proxypool import ProxyPool, parse_proxy_url  # type: ignore

    try:
        pool = ProxyPool(proxies)
    except ValueError as e:
        rep.add("代理池", FAIL, f"配置无法解析：{e}",
                "地址形如 scheme://user:pass@host:port；缺 scheme 会按 http 处理")
        return

    parsed_registered = {}
    for item in registered:
        # `rpartition` 而不是 `partition`：代理口令里带 `=` 很常见（base64 填充），
        # 而 IP 里不可能有 `=` —— 按第一个等号切会把 `scheme://u:dGVzdA==@h:8080`
        # 切成 server=`scheme://u:dGVzdA`，于是"出口明明在列表里"却报"不在列表里"。
        server, _, ip = item.rpartition("=")
        parsed_registered[server.strip()] = ip.strip()
    unknown = [s for s in parsed_registered
               if not pool.set_egress_ip_by_server(s, parsed_registered[s])]
    if unknown:
        # ⚠️ 这里**不能 return**：一旦有一条登记项写错，`--verify-egress` 就整段被跳过 ——
        # 用户唯一想要的东西（登记值和真实出口对不对得上）静默不执行，报告里连
        # "未实测"都不会出现，读起来像"没问题"。继续往下走，未匹配项自然落到 missing 里。
        rep.add("代理池 · --proxy-egress-ip", WARN,
                f"这些出口不在 --proxy 列表里：{unknown}",
                "检查拼写：格式是 scheme://host:port=1.2.3.4；其余出口照常检查")

    snapshot = pool.snapshot()
    missing = [e["proxy"] for e in snapshot["entries"] if not e["egress_ip_known"]]
    if missing:
        rep.add(f"代理池 · {snapshot['size']} 个出口", WARN,
                f"{len(missing)} 个未登记出口 IP：{missing}",
                "用 --proxy-egress-ip scheme://host:port=IP 登记；"
                "不登记会退回地址哈希（同 IP 的不同入口无法共享缓存）")
    else:
        rep.add(f"代理池 · {snapshot['size']} 个出口", OK, "全部已登记出口 IP")

    if not verify:
        rep.add("代理池 · 出口 IP 实测", WARN, "未实测（加 --verify-egress 才真发请求）",
                "实测会经每个出口请求一次 IP 回显；能确认登记值是否真的对")
        return
    from proxypool import verify_egress  # type: ignore

    checks = verify_egress(pool, echo_url=echo_url, timeout=timeout)
    bad = [c for c in checks if not c.ok]
    if bad:
        for c in bad:
            rep.add(f"出口实测 · {c.proxy}", FAIL, c.detail,
                    "改登记值：--proxy-egress-ip scheme://host:port=实测IP；"
                    "或换出口 —— 不一致会让凭证发给错 IP，表现为静默 403")
    else:
        detail = "；".join(f"{c.proxy}={c.observed or '未测得'}" for c in checks)
        rep.add("代理池 · 出口 IP 实测", OK, detail)


def check_service_boot(rep: Report, port: int, timeout: float = 90.0) -> None:
    """真起一次服务并打 /health —— 这是"端到端能不能跑"的唯一可信答案。"""
    import subprocess
    import urllib.request

    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "solver" / "service.py"),
         "--port", str(port), "--no-auth", "--cache-dir", tempfile.mkdtemp(prefix="cfsolve-boot-")],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    started = time.time()
    last = ""
    try:
        while time.time() - started < timeout:
            if proc.poll() is not None:
                out = proc.stdout.read() if proc.stdout else ""
                rep.add("服务启动", FAIL, f"进程提前退出（exit={proc.returncode}）：{out.strip()[:200]}",
                        "看上面的输出；常见原因：camoufox 未 fetch、端口被占、依赖缺失")
                return
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if data.get("state") == "ready":
                    rep.add("服务启动", OK, f"{time.time() - started:.1f}s 后 ready（cache={data.get('cache_backend')}）")
                    return
                last = json.dumps(data, ensure_ascii=False)[:200]
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {str(e)[:80]}"
            time.sleep(0.5)
        rep.add("服务启动", FAIL, f"{timeout:.0f}s 内未就绪（最后一次：{last}）",
                "看 solver/service.py 的启动日志；/health 三态会说明卡在哪一步")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CF-Bypass-Research 环境自检")
    ap.add_argument("--port", type=int, default=8878)
    ap.add_argument("--offline", action="store_true", help="只查环境，不启动服务")
    ap.add_argument("--token-file", default=".env")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    ap.add_argument("--proxy", action="append", default=[],
                    help="要自检的代理出口（可重复）；与服务的 --proxy 同格式")
    ap.add_argument("--proxy-egress-ip", action="append", default=[],
                    help="已登记的出口 IP，格式 scheme://host:port=1.2.3.4（可重复）")
    ap.add_argument("--verify-egress", action="store_true",
                    help="经每个出口实测 IP 回显并与登记值比对（会出站；默认不做）")
    ap.add_argument("--egress-echo-url", default="",
                    help="IP 回显地址，默认用 Cloudflare /cdn-cgi/trace")
    args = ap.parse_args(argv)
    _force_utf8_stdout()

    rep = Report()
    check_python(rep)
    check_module(rep, "camoufox", WARN, "pip install -r requirements.txt")
    check_module(rep, "playwright", WARN, "pip install -r requirements.txt")
    check_module(rep, "cryptography", WARN, "落盘加密需要它；缺了会降级成内存缓存")
    check_camoufox_binary(rep)
    check_cache_dir(rep)
    check_token(rep, args.token_file)
    check_optional_backends(rep)
    _echo = args.egress_echo_url.strip()
    if not _echo:
        from proxypool import DEFAULT_EGRESS_ECHO_URL  # type: ignore

        _echo = DEFAULT_EGRESS_ECHO_URL
    check_proxy_pool(rep, args.proxy, args.proxy_egress_ip,
                     verify=args.verify_egress, echo_url=_echo, timeout=10.0)
    if not args.offline:
        check_port(rep, args.port)
        check_service_boot(rep, args.port)

    if args.json:
        print(json.dumps({"worst": rep.worst(), "exit_code": rep.exit_code(), "checks": rep.checks},
                         ensure_ascii=False, indent=2))
    else:
        print(rep.render())
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
