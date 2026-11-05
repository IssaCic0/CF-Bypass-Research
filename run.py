#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CF-Bypass-Research 跨平台统一入口（v5.1 T-80）。

与 run.ps1 的子命令一一对应；run.ps1 内部转发到这里，两套逻辑不再漂移。
仅用 stdlib；verify 按 run.ps1 的既有顺序 fail-fast：pytest → compileall →
go → findings → e2e → repo hygiene → research doctor → service-report(v6.2 golden)。

四类验收（v6.2 起全部可复跑，不再是人工一次性快照）：
    python run.py ui          # UI 截图验收：真 Chrome 跑 5 种状态 + 按钮交互，截图落 plans/证据/
    python run.py realcalls   # 真实调用验收：真进程 + 真 token，全端点 × 鉴权两态
    python run.py e2e         # 真实 E2E 验收：真进程 + 真浏览器（--skip-browser 可跳过浏览器段）
    python run.py report      # 服务报告 golden（响应字段形状）；--write 重新生成

用法：
    python run.py doctor [--port N]
    python run.py test | quick | go | findings | verify | e2e | bench | start | stop
    python run.py smoke [--port N] [--no-auth]
    python run.py ui [--only ready|wedged|degraded|no-token|audit|unreachable]
    python run.py realcalls | report [--write]
    python run.py egress --proxy URL [--proxy URL ...] [--proxy-egress-ip URL=IP ...]
                    [--egress-echo-url URL]          # 唯一会出站的子命令
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib import request as urlreq
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent

# Windows 控制台默认 GBK，打印 '↔'/'✅' 等字符会 UnicodeEncodeError。
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        _s.reconfigure(encoding="utf-8", errors="replace")

# 子命令 → 实现函数。键集合必须与 run.ps1 的 ValidateSet 完全相等
# （tests/test_runpy.py::test_runpy_targets_match_runps1 锁死）。
COMMANDS = {
    'doctor': None,
    'test': None,
    'quick': None,
    'go': None,
    'findings': None,
    'smoke': None,
    'e2e': None,
    'start': None,
    'stop': None,
    'bench': None,
    'verify': None,
    'ui': None,
    'realcalls': None,
    'report': None,
    'egress': None,
    'help': None,
}


def step(msg):
    print(f"\n=== {msg} ===")


def _redact(cmd) -> str:
    """回显命令时把 `scheme://user:pass@host` 里的口令抹掉。

    这一行是打在**终端**上的，而终端经常被重定向进文件 / CI 日志 / 贴进 issue
    （`python run.py egress --proxy http://u:p@h:8080` 是最常见的用法）。
    实际执行的 argv 不受影响 —— 只改显示。
    """
    return " ".join(re.sub(r"(://[^:/@\s]+):[^@\s]*@", r"\1:***@", str(c))
                    for c in cmd)


def run(cmd, cwd=None, **kw):
    """跑外部命令，透传退出码；command not found 视为失败 127。"""
    print(f"+ {_redact(cmd)}")
    try:
        return subprocess.call([str(c) for c in cmd],
                               cwd=str(cwd or ROOT), **kw)
    except FileNotFoundError:
        print(f"FAIL: 找不到可执行文件 {cmd[0]}", file=sys.stderr)
        return 127


def read_env_token():
    """读 CF_SOLVE_TOKEN：环境变量优先，否则 .env（与 run.ps1 stop 逻辑一致）。"""
    tok = os.environ.get("CF_SOLVE_TOKEN")
    if tok:
        return tok
    envf = ROOT / ".env"
    if envf.exists():
        for line in envf.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("CF_SOLVE_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def cmd_doctor(a):
    return run([sys.executable, "scripts/doctor.py", "--port", a.port])


def cmd_test(a):
    step("Python 全量测试 + 覆盖率门槛（policy.md: ≥80%）")
    return run([sys.executable, "-m", "pytest", "tests/", "-q",
                "--cov=solver", "--cov-report=term-missing", "--cov-fail-under=80"])


def cmd_quick(a):
    step("定向快测（改一处代码时用这个，别每次跑全量）")
    rc1 = run([sys.executable, "-m", "pytest",
               "tests/test_cache.py", "tests/test_cache_sqlite.py", "-q"])
    rc2 = run([sys.executable, "-m", "pytest", "tests/", "-q",
               "-k", "route or guard or ttl or budget"])
    return 1 if (rc1 or rc2) else 0


def cmd_go(a):
    step("Go: vet + test -race")
    rc = run(["go", "vet", "./..."], cwd=ROOT / "gocfsolve")
    if rc:
        return rc
    return run(["go", "test", "-race", "./..."], cwd=ROOT / "gocfsolve")  # 注：run() cwd 由 kw 覆盖为 gocfsolve


def cmd_findings(a):
    step("结论库一致性校验（findings.json ↔ 仓库文件）")
    if not shutil.which("node"):
        print("WARN: 未安装 node，跳过 findings 校验")
        return 0
    return run(["node", "scripts/validate-findings.mjs"])


def cmd_e2e(a):
    step("真进程 E2E 验收（离线）")
    args = [sys.executable, "scripts/e2e_local.py", "--json-out", "evidence/e2e-local.json"]
    if a.skip_browser:
        args.append("--skip-browser")
    return run(args)


def cmd_bench(a):
    step("离线压测（不打真实站点）")
    rcs = [
        run([sys.executable, "scripts/loadtest.py", "--mode", "hot",
             "--workers", "8", "--duration", "5"]),
        run([sys.executable, "scripts/loadtest.py", "--mode", "cold",
             "--workers", "16", "--solve-delay", "0.05"]),
        run([sys.executable, "scripts/loadtest.py", "--mode", "share",
             "--processes", "4", "--iterations", "2"]),
    ]
    return next((rc for rc in rcs if rc), 0)


def cmd_smoke(a):
    step(f"起服务并做冒烟（port={a.port}）")
    cache_dir = Path(tempfile.mkdtemp(prefix="cfsolve-smoke-"))
    args = [sys.executable, "solver/service.py", "--port", str(a.port),
            "--cache-dir", str(cache_dir)]
    if a.no_auth:
        args.append("--no-auth")
    proc = subprocess.Popen(args, cwd=ROOT)
    try:
        base = f"http://127.0.0.1:{a.port}"
        deadline = time.monotonic() + 120
        ready = None
        while time.monotonic() < deadline:
            try:
                with urlreq.urlopen(f"{base}/health", timeout=3) as r:
                    import json
                    ready = json.loads(r.read())
                if ready.get("state") == "ready":
                    break
            except (URLError, OSError, ValueError):
                time.sleep(0.5)
        if not ready or ready.get("state") != "ready":
            print("FAIL: 服务 120s 内未就绪（看 solver/service.py 的启动输出）",
                  file=sys.stderr)
            return 1
        print(f"OK  /health ready（browser={ready.get('browser')}, "
              f"cache={ready.get('cache_backend')}）")

        with urlreq.urlopen(f"{base}/detect?url=https://example.com/", timeout=20) as r:
            import json
            d = json.loads(r.read())
        print(f"OK  /detect example.com → protected={d.get('protected')} "
              f"route={d.get('recommendation', {}).get('route')}")

        try:
            urlreq.urlopen(f"{base}/detect?url=http://127.0.0.1:{a.port}/health",
                           timeout=10)
            print("FAIL: SSRF 守卫失效：内网 URL 没有被拒绝", file=sys.stderr)
            return 1
        except (URLError, OSError):
            pass
        print("OK  SSRF 守卫拦住了内网 URL")
        return 0
    finally:
        proc.kill()
        shutil.rmtree(cache_dir, ignore_errors=True)


def cmd_start(a):
    step(f"前台启动（port={a.port}，Ctrl+C 退出）")
    args = [sys.executable, "solver/service.py", "--port", str(a.port),
            "--cache-dir", "./cache"]
    if a.no_auth:
        args.append("--no-auth")
    try:
        return subprocess.call(args, cwd=ROOT)
    except KeyboardInterrupt:
        return 0


def cmd_stop(a):
    """优雅停止监听 a.port 的服务：先 /shutdown（带 token），再强制终止。"""
    step(f"优雅停止监听 {a.port} 的服务")
    port = a.port

    def listening_pids():
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        pids = []
        # 必须精确匹配 `:<port>` 后紧跟空白：子串匹配会把 `:8878` 命中 `:88780`，
        # 于是"停 8878"顺手杀掉了另一个服务（破坏性误杀，复审点名）。
        pattern = re.compile(rf":{re.escape(str(port))}\s")
        for line in out.splitlines():
            if "LISTENING" in line and pattern.search(line):
                pid = line.split()[-1]
                if pid.isdigit():
                    pids.append(pid)
        return pids

    pids = listening_pids()
    if not pids:
        print(f"没有进程监听 {port}")
        return 0

    token = read_env_token()
    graceful = False
    try:
        req = urlreq.Request(f"http://127.0.0.1:{port}/shutdown", method="POST",
                             data=b"{}", headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urlreq.urlopen(req, timeout=5) as r:
            r.read()
        graceful = True
        print("已请求优雅退出（等 15s 让在途请求收尾）")
        for _ in range(30):
            time.sleep(0.5)
            if not listening_pids():
                break
    except (URLError, OSError) as e:
        print(f"优雅退出不可用（{e}），回退为强制终止")

    for pid in listening_pids():
        # 不要写成跨行 f-string：PEP 701（f-string 内换行）是 Python 3.12+，
        # CI 跑 3.11 会直接 SyntaxError（本轮 CI 抓到）。
        reason = "优雅退出未在 15s 内完成" if graceful else "无 token 或端点不可用"
        print(f"强制终止 PID {pid}（{reason}）")
        if os.name == "nt":
            subprocess.call(["taskkill", "/F", "/PID", pid])
        else:
            subprocess.call(["kill", "-9", pid])
    return 0


def run_steps(steps):
    """fail-fast：第一个非零返回即停，退出码 = 该步骤退出码。"""
    for name, fn in steps:
        rc = fn()
        if rc != 0:
            print(f"\nFAIL: {name} 未通过（exit={rc}），后续步骤不执行",
                  file=sys.stderr)
            return rc
    return 0


def cmd_verify(a):
    step("verify: Python 测试 + 覆盖率")
    if a.fast:
        pytest_files = ["tests/test_runpy.py"]
        cov = []
    else:
        pytest_files = ["tests/"]
        cov = ["--cov=solver", "--cov-report=term", "--cov-fail-under=80"]

    def py():
        return run([sys.executable, "-m", "pytest", *pytest_files, "-q", *cov])

    def compileall():
        return run([sys.executable, "-m", "compileall", "-q", "solver", "scripts", "tests"])

    def go():
        return cmd_go(a)

    def findings():
        return cmd_findings(a)

    def e2e():
        rc = cmd_e2e(a)
        if rc:
            print("E2E 验收失败（看 evidence/e2e-local.json 的 failed 项）", file=sys.stderr)
        return rc

    def hygiene():
        p = ROOT / "scripts" / "check_repo_hygiene.py"
        if not p.exists():
            print("FAIL: scripts/check_repo_hygiene.py 不存在（T-69 未落地）。"
                  "落地后 verify 自动纳入；或先跑其余步骤定位问题。", file=sys.stderr)
            return 1
        return run([sys.executable, "scripts/check_repo_hygiene.py"])

    def service_report():
        """确定性服务报告 vs golden（v6.2，借鉴 pinchtab 的 docs/audit.md）。

        为什么放进 verify：`validate-openapi.mjs` 只校验**路由集合**，
        新加/删掉一个响应字段它完全看不见 —— v6.1 的 `captcha_types` 与
        v6.2 的 `wedge` 就是在"门禁全绿"的情况下漏进文档的。
        """
        p = ROOT / "scripts" / "service_report.py"
        if not p.exists():
            print("FAIL: scripts/service_report.py 不存在（v6.2 服务报告门禁未落地）。", file=sys.stderr)
            return 1
        return run([sys.executable, str(p)])

    def research_doctor():
        p = ROOT / "scripts" / "research.py"
        if not p.exists():
            print("FAIL: scripts/research.py 不存在（T-67 研究台未落地）。"
                  "其 doctor 子命令缺失，无法校验研究台环境。", file=sys.stderr)
            return 1
        return run([sys.executable, "scripts/research.py", "doctor"])

    rc = run_steps([
        ("pytest", py),
        ("compileall", compileall),
        ("go vet/test", go),
        ("findings", findings),
        ("e2e", e2e),
        ("repo-hygiene(T-69)", hygiene),
        ("research-doctor(T-67)", research_doctor),
        ("service-report(golden)", service_report),
    ])
    if rc == 0:
        print("\n全部通过 ✅")
    return rc


def cmd_ui(a):
    """UI 验收：真 Chrome 跑 6 种状态 + 按钮交互 + 截图（v6.3）。

    为什么单独一个命令而不是塞进 verify：它需要 Playwright 的 Chromium，
    而 CI 按规定不下载浏览器（policy §2：全程离线）。本地跑，把截图落 plans/证据/。
    """
    return run([sys.executable, str(ROOT / "scripts" / "verify_ui_e2e.py")]
               + (["--only", a.value] if getattr(a, "value", "") else []))


def cmd_realcalls(a):
    """真实调用验收：真进程 + 真 token，全端点 × 鉴权两态（v6.2 脚本化）。"""
    return run([sys.executable, str(ROOT / "scripts" / "verify_real_calls.py")])


def cmd_report(a):
    """确定性服务报告 vs golden（响应字段形状门禁）。"""
    p = ROOT / "scripts" / "service_report.py"
    return run([sys.executable, str(p)] + (["--write"] if getattr(a, "write", False) else []))


def cmd_egress(a):
    """出口 IP 实测校验（v6.3，参考 proxypool 的 tester 思路）。

    为什么单独一个命令：它会**出站**（经每个代理请求一次 IP 回显）。
    doctor 默认不带网络等待；要实测就显式跑这个。
    """
    cmd = [sys.executable, str(ROOT / "scripts" / "doctor.py"),
           "--offline", "--verify-egress"]
    for p in getattr(a, "proxy", []) or []:
        cmd += ["--proxy", p]
    for p in getattr(a, "proxy_egress_ip", []) or []:
        cmd += ["--proxy-egress-ip", p]
    if getattr(a, "egress_echo_url", ""):
        cmd += ["--egress-echo-url", a.egress_echo_url]
    return run(cmd)


def cmd_help(a):
    print(__doc__)
    return 0


for _name in COMMANDS:
    fn = globals().get(f"cmd_{_name}")
    COMMANDS[_name] = fn or cmd_help


def build_parser():
    p = argparse.ArgumentParser(prog="run.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=8878)
    common.add_argument("--no-auth", action="store_true", help="鉴权关闭（smoke/start）")
    common.add_argument("--skip-browser", action="store_true", help="e2e 跳过真浏览器")
    common.add_argument("--fast", action="store_true",
                        help="verify 只跑定向测试不做覆盖率（T-80 验证用）")
    for name in COMMANDS:
        sp = sub.add_parser(name, parents=[common])
        if name == "ui":
            sp.add_argument("--only", dest="value", default="",
                            help="只跑某个状态：ready/wedged/degraded/no-token/unreachable")
        if name == "report":
            sp.add_argument("--write", action="store_true", help="重新生成 golden")
        if name == "egress":
            sp.add_argument("--proxy", action="append", default=[], help="要实测的出口（可重复）")
            sp.add_argument("--proxy-egress-ip", action="append", default=[],
                            help="已登记的出口 IP，格式 scheme://host:port=1.2.3.4")
            sp.add_argument("--egress-echo-url", default="",
                            help="IP 回显地址（默认 Cloudflare /cdn-cgi/trace）")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    return COMMANDS[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
