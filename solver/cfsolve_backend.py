"""纯协议（无浏览器）后端：调用 chris124567/cfsolve 的 `challenge` 可执行文件。

原理（与"纯 JS 引擎"完全不同，别混为一谈）：
  · cfsolve 自带精简 JS 虚拟机 + 虚拟页面环境，能在**没有浏览器进程**的前提下跑完
    Cloudflare 的挑战脚本；
  · 挑战脚本会向它索要"环境指纹"（WebGL/Audio/时序/字体…），它并不现场编造，而是
    回放一份**从真实浏览器录下来的自洽答案** —— 这就是 profile 文件
    （`profiles/*.json`：26 条 answers + 41 条 challenge_page_answers）；
  · TLS 层用打了补丁的 BoringSSL 冒充同一款浏览器的 ClientHello。

所以它不是"算出来的"，而是**重放一份真实浏览器快照**。这也解释了它的硬约束：
cf_clearance 必须**用与 profile 相同的出口 IP 和 UA** 回放，否则照样被拒。

⚠️ 本仓库**无法构建/实测它**（需要 libclang + CMake + 打补丁的 BoringSSL，本机缺 libclang），
所以这个后端是"接线已就绪、二进制由用户自备"的状态。
构建与录制 profile 的步骤见 docs/纯协议-cfsolve.md。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

try:  # 包导入
    from .errors import ErrorCode, SolveError
except ImportError:  # 直接以脚本运行
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]


class CfsolveBackend:
    """把 cfsolve 的 CLI 包成我们后端协议里的一个实现。"""

    def __init__(
        self,
        binary: str,
        profile: str,
        *,
        work_root: str = "",
        timeout: float = 120.0,
        extra_args: list[str] | None = None,
    ) -> None:
        self.binary = binary
        self.profile = profile
        self.work_root = work_root
        self.timeout = timeout
        self.extra_args = list(extra_args or [])
        self._ua_cache: str | None = None

    def name(self) -> str:
        return "cfsolve"

    def is_configured(self) -> bool:
        return bool(self.binary) and os.path.isfile(self.binary) and os.path.isfile(self.profile)

    def user_agent(self) -> str:
        """profile 里录的 UA —— 回放必须用它，否则 cf_clearance 失效。"""
        if self._ua_cache is None:
            self._ua_cache = ""
            try:
                with open(self.profile, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key, value in ((data.get("transport") or {}).get("headers") or {}).items():
                    if key.lower() == "user-agent" and isinstance(value, str):
                        self._ua_cache = value
                        break
            except Exception:  # noqa: BLE001 —— 读不到 UA 不致命，由调用方保证
                self._ua_cache = ""
        return self._ua_cache

    def solve(self, url: str, timeout: float, force: bool = False, **kwargs) -> dict:
        want = str(kwargs.get("want") or "clearance").lower()
        sitekey = str(kwargs.get("sitekey") or "")
        action = str(kwargs.get("action") or "")
        cdata = str(kwargs.get("cdata") or "")

        if not self.is_configured():
            raise SolveError(
                ErrorCode.NOT_CONFIGURED,
                "cfsolve 可执行文件或 profile 不存在",
                stage="admit",
                hint="按 docs/纯协议-cfsolve.md 构建 challenge 二进制并准备 profile",
            )

        workdir = tempfile.mkdtemp(prefix="cfsolve-", dir=self.work_root or None)
        # cfsolve 要求输出目录不存在或为空
        target_dir = os.path.join(workdir, "run")
        cmd = [self.binary, "--page", url, target_dir, "--solve", "--fingerprint", self.profile]
        if want in ("turnstile", "recaptcha_v3"):
            if not sitekey:
                raise SolveError(ErrorCode.INVALID_REQUEST, f"want={want} 需要 sitekey", stage="validate")
            cmd += ["--sitekey", sitekey, "--blank-page"]
            if action:
                cmd += ["--action", action]
            if cdata:
                cmd += ["--cdata", cdata]
        cmd += self.extra_args

        t0 = time.monotonic()
        elapsed = 0.0
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=min(self.timeout, max(5.0, timeout + 30.0)),
            )
        except subprocess.TimeoutExpired as e:
            raise SolveError(
                ErrorCode.TIMEOUT,
                f"cfsolve 超时（>{self.timeout:.0f}s）",
                stage="wait",
                hint="提高 --cfsolve-timeout，或换用浏览器引擎",
            ) from e
        except OSError as e:
            raise SolveError(
                ErrorCode.NOT_CONFIGURED,
                f"无法执行 cfsolve：{e}",
                stage="admit",
                hint="确认 --cfsolve-bin 指向可执行文件且有执行权限",
            ) from e
        finally:
            elapsed = round(time.monotonic() - t0, 2)
            shutil.rmtree(workdir, ignore_errors=True)

        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise classify(stderr or stdout, proc.returncode)

        first = stdout.splitlines()[0].strip() if stdout else ""
        if not first:
            raise SolveError(
                ErrorCode.UNSOLVED, "cfsolve 没有输出任何结果", stage="wait",
                hint="看它的 stderr 进度日志（本后端会把它记进 last_error）",
            )

        if first.startswith("cf_clearance="):
            clearance = first.split("=", 1)[1].strip()
            return {
                "ok": True, "solved": True, "want": "clearance",
                "cf_clearance": clearance,
                "user_agent": self.user_agent(),
                "cookies": {"cf_clearance": clearance},
                "cookie_list": [{
                    "name": "cf_clearance", "value": clearance, "domain": "", "path": "/",
                    "expires": -1, "http_only": True, "secure": True, "same_site": "",
                }],
                "expires_in": 0, "expires_at": 0.0,
                "elapsed": elapsed, "cached": False, "cacheable": True,
                "egress_id": "cfsolve", "backend": self.name(),
            }

        return {
            "ok": True, "solved": True, "want": want,
            "token": first,
            "token_field": "cf-turnstile-response" if want == "turnstile" else "g-recaptcha-response",
            "user_agent": self.user_agent(),
            "cookies": {}, "cookie_list": [],
            "expires_in": 300 if want == "turnstile" else 120, "expires_at": 0.0,
            "elapsed": elapsed, "cached": False, "cacheable": False,
            "egress_id": "cfsolve", "backend": self.name(),
        }


def classify(text: str, code: int) -> SolveError:
    """把 cfsolve 的错误文本映射到我们的错误码（对应它 README 的错误表）。"""
    low = (text or "").lower()
    if "not challenged" in low:
        return SolveError(ErrorCode.UNSOLVED, "该页没有 Cloudflare 挑战（NotChallenged）", stage="wait",
                          hint="确认目标确实有盾；纯协议后端只在有挑战时才有意义")
    if "blocked" in low:
        return SolveError(ErrorCode.UNSOLVED, "站点直接返回错误页（Blocked），不是挑战", stage="wait",
                          hint="这是 IP/账号级封禁，换出口比换方案有效")
    if "rejected" in low:
        return SolveError(ErrorCode.UNSOLVED, "Cloudflare 拒收这次解答（Rejected）", stage="wait",
                          hint="profile 可能过期：用 capture-proxy 重新录一份")
    if "network" in low or "connect" in low:
        return SolveError(ErrorCode.TRANSPORT, f"网络错误：{text[-200:]}", stage="navigate")
    return SolveError(ErrorCode.UNSOLVED, f"cfsolve 失败（exit={code}）：{text[-200:]}", stage="wait")
