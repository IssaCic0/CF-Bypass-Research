"""真实调用验收：真进程 + 真 HTTP + 真 token，全端点 × 鉴权两态矩阵。

为什么必须脚本化（v6.2）：这张 27 项的矩阵在 v5.0.1 是**人工跑一次**记录进
`workflow_status.md` 的 —— 它证明过当时那一刻，但**不能复跑**，所以之后每次改动
（v6.1 嫁接层、v6.2 卡死状态/兼容层错误码）都只能"凭记忆认为还成立"。
不可复跑的验收不是验收，是一次性快照。

设计要点：
  · **真进程**：`python solver/service.py` 子进程，不是进程内 ThreadingHTTPServer；
  · **真 token**：从服务端首启写的 `.env` 里读（不是 `--no-auth`）；
  · 每个断言写清"期望"与"为什么这样期望"，失败时能直接定位；
  · 不联网：所有目标都是本机/非法 scheme，不会真的打出去（policy §2）。

用法：
    python scripts/verify_real_calls.py            # 全部
    python scripts/verify_real_calls.py --keep     # 失败时保留进程与日志（调试）
退出码：0=全绿；1=有失败项。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not ok else ""))
    return ok


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def call(base: str, method: str, path: str, *, token: str = "", body: dict | None = None,
         raw: bytes | None = None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            payload = r.read()
            return r.status, payload, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def as_json(payload: bytes) -> dict:
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def read_token(path: Path, timeout: float = 20.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("CF_SOLVE_TOKEN="):
                    v = line.split("=", 1)[1].strip()
                    if v:
                        return v
        except OSError:
            pass
        time.sleep(0.2)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="真实调用验收（真进程 + 真 token）")
    ap.add_argument("--keep", action="store_true", help="失败时保留进程与日志")
    args = ap.parse_args()

    port = free_port()
    workdir = Path(tempfile.mkdtemp(prefix="cf-realcall-"))
    token_file = workdir / "token.env"
    audit_file = workdir / "audit.jsonl"
    log_path = workdir / "service.log"
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "solver/service.py", "--port", str(port),
         "--token-file", str(token_file), "--audit-log", str(audit_file)],
        cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    failed = False
    try:
        # ── 1. 起得来 + token 落地 ────────────────────
        ready = False
        end = time.monotonic() + 90
        while time.monotonic() < end:
            try:
                st, _, _ = call(base, "GET", "/health")
                if st in (200, 503):
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.3)
        check("01 service ready", ready)
        token = read_token(token_file)
        check("02 token 文件已生成", bool(token), f"长度={len(token)}")
        if not ready or not token:
            failed = True
            return _finish(proc, log, log_path, workdir, args.keep, failed)

        # ── 2. 公开端点 ──────────────────────────────
        st, body, _ = call(base, "GET", "/")
        check("03 GET / 公开可读", st == 200 and b"<!doctype html>" in body.lower(), f"status={st}")
        st, body, _ = call(base, "GET", "/health")
        minimal = as_json(body)
        # 状态码取决于**这台机器有没有 fetch 浏览器二进制**（CI 按规定不下载 → 503 starting）。
        # 契约要看的是"给了什么字段"，不是"此刻是不是 ready"——后者是环境，不是代码。
        check("04 GET /health 免鉴权可读（200/503）", st in (200, 503), f"status={st}")
        # `request_id` 是 T-79 的贯穿 ID，每个 JSON 出口都有，属于最小集的一部分。
        check("05 最小集 = 4 个状态字段 + request_id",
              set(minimal) == {"ok", "state", "browser_alive", "worker_alive", "request_id"},
              str(sorted(minimal)))
        check("06 最小集不含内部字段",
              not ({"last_error", "proxies", "ua_fingerprint", "cache_backend"} & set(minimal)))

        # ── 3. 鉴权矩阵：无 token → 401 ──────────────
        for path, method, payload in (
            ("/status", "GET", None), ("/logs", "GET", None),
            ("/stats", "GET", None), ("/metrics", "GET", None),
            ("/console", "GET", None),
            ("/detect", "POST", {"url": "http://127.0.0.1:1/"}),
            ("/solve", "POST", {"url": "http://127.0.0.1:1/", "want": "clearance"}),
        ):
            st, body, _ = call(base, method, path, body=payload)
            j = as_json(body)
            check(f"07 {method} {path} 无 token → 401",
                  st == 401 and j.get("error_code") == "unauthorized", f"status={st}")

        # ── 4. 带 token 的正常读取 ──────────────────
        st, body, _ = call(base, "GET", "/health", token=token)
        full = as_json(body)
        # 同样：不看状态码，看"带 token 时多了哪些运维字段"。
        check("08 GET /health 带 token 给完整视图",
              st in (200, 503) and "backends" in full and "cache_backend" in full, f"status={st}")
        check("09 完整视图含 captcha_types 清单",
              isinstance(full.get("captcha_types"), list) and len(full["captcha_types"]) == 11,
              str(full.get("captcha_types"))[:80])
        st, body, _ = call(base, "GET", "/status", token=token)
        s = as_json(body)
        check("10 GET /status 含在途 + 卡死快照",
              st == 200 and "inflight" in s and "wedge" in s, f"keys={sorted(s)[:8]}")
        check("11 /status 不把自身算进 inflight", s.get("inflight_count") == 0, str(s.get("inflight_count")))
        st, body, _ = call(base, "GET", "/logs?lines=5", token=token)
        check("12 GET /logs 返回环形缓冲", st == 200 and "lines" in as_json(body))
        st, body, _ = call(base, "GET", "/stats", token=token)
        check("13 GET /stats 200", st == 200)
        st, body, _ = call(base, "GET", "/metrics", token=token)
        text = body.decode("utf-8", "replace")
        check("14 GET /metrics 是 Prometheus 文本",
              st == 200 and "cfsolve_solves_total" in text, f"status={st}")
        check("15 /metrics 含进程自检序列",
              "cfsolve_process_rss_bytes" in text)
        st, body, _ = call(base, "GET", "/console", token=token)
        check("16 GET /console 带 token 返回 HTML", st == 200 and b"<!doctype html>" in body.lower())

        # ── 5. 守卫与参数校验 ───────────────────────
        st, body, _ = call(base, "POST", "/detect", token=token,
                           body={"url": "http://127.0.0.1:1/"})
        check("17 POST /detect 内网目标 → 400（SSRF 守卫）",
              st == 400 and as_json(body).get("error_code") == "invalid-request", f"status={st}")
        st, body, _ = call(base, "POST", "/solve", token=token,
                           body={"url": "ftp://x/", "want": "clearance"})
        check("18 POST /solve 非法 scheme → 400", st == 400, f"status={st}")
        st, body, _ = call(base, "POST", "/solve", token=token, raw=b"{" + b"x" * (70 * 1024))
        check("19 超大 body → 413（可重试语义）", st == 413, f"status={st}")

        # ── 6. 兼容层（YesCaptcha）──────────────────
        st, body, _ = call(base, "POST", "/createTask",
                           body={"clientKey": "wrong", "task": {"type": "AntiCloudflareTask",
                                                                "websiteURL": "https://e.com/"}})
        j = as_json(body)
        check("20 createTask 错误 clientKey → 200+errorId=1",
              st == 200 and j.get("errorId") == 1
              and j.get("errorCode") == "ERROR_KEY_DOES_NOT_EXIST", f"status={st} {j.get('errorCode')}")
        st, body, _ = call(base, "POST", "/createTask",
                           body={"clientKey": token,
                                 "task": {"type": "AntiCloudflareTask",
                                          "proxyType": "http", "proxyAddress": "1.2.3.4"}})
        j = as_json(body)
        check("21 createTask 任务级代理 → ERROR_PROXY_NOT_SUPPORTED",
              j.get("errorCode") == "ERROR_PROXY_NOT_SUPPORTED", str(j.get("errorCode")))
        st, body, _ = call(base, "POST", "/createTask",
                           body={"clientKey": token,
                                 "task": {"type": "ImageToTextTask", "websiteURL": "https://e.com/"}})
        j = as_json(body)
        check("22 createTask 不支持的类型 → ERROR_TASK_NOT_SUPPORTED（带原因）",
              j.get("errorCode") == "ERROR_TASK_NOT_SUPPORTED"
              and "视觉模型" in (j.get("errorDescription") or ""), str(j.get("errorCode")))
        st, body, _ = call(base, "POST", "/createTask",
                           body={"clientKey": token,
                                 "task": {"type": "AntiCloudflareTask"}})
        j = as_json(body)
        check("23 createTask 缺必填 → ERROR_TASK_PROPERTY_EMPTY",
              j.get("errorCode") == "ERROR_TASK_PROPERTY_EMPTY", str(j.get("errorCode")))
        st, body, _ = call(base, "POST", "/getTaskResult", body={"clientKey": token, "taskId": "nope"})
        j = as_json(body)
        check("24 getTaskResult 未知 taskId → ERROR_NO_SUCH_CAPCHA_ID",
              j.get("errorCode") == "ERROR_NO_SUCH_CAPCHA_ID", str(j.get("errorCode")))

        # ── 7. 未知路径：两态语义 ───────────────────
        st, _, _ = call(base, "GET", "/nope")
        check("25 未知路径无 token → 401（不泄露端点存在性）", st == 401, f"status={st}")
        st, _, _ = call(base, "GET", "/nope", token=token)
        check("26 未知路径带 token → 404", st == 404, f"status={st}")

        # ── 8. 审计日志不落凭证 ─────────────────────
        try:
            audit_text = audit_file.read_text(encoding="utf-8")
            check("27 审计日志已落盘且有 solve/deny 事件",
                  '"event"' in audit_text and len(audit_text.splitlines()) > 0)
            check("28 审计日志不含 token 原文", token not in audit_text)
        except OSError:
            check("27 审计日志已落盘且有 solve/deny 事件", False, "文件不存在")

        # ── 9. 优雅退出 ─────────────────────────────
        st, body, _ = call(base, "POST", "/shutdown", token=token)
        check("29 POST /shutdown → 202", st == 202, f"status={st}")
        code = proc.wait(timeout=40)
        check("30 退出码 0", code == 0, f"exit={code}")
        log.flush()
        check("31 日志无 Traceback", "Traceback" not in log_path.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        check("unexpected", False, f"{type(e).__name__}: {e}")
        failed = True
    finally:
        # 收尾只做清理，**不在这里 return** —— finally 里的 return 会吞掉异常与 KeyboardInterrupt
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        log.close()
    return _finish(proc, log, log_path, workdir, args.keep, failed)


def _finish(proc, log, log_path: Path, workdir: Path, keep: bool, failed: bool) -> int:
    bad = [r for r in results if not r[1]]
    print(f"\nRESULT: {len(results) - len(bad)}/{len(results)} 通过")
    if bad or keep:
        print(f"日志：{log_path}")
        for name, _, detail in bad[:10]:
            print(f"  FAIL {name} :: {detail}")
    else:
        for p in workdir.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass
    return 0 if not bad and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())