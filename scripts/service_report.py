"""确定性服务报告（deterministic service report）+ golden 断言。

借鉴 pinchtab 的 `docs/audit.md`：把一次真实运行的可观测面固化成**确定性 JSON**，
与 `docs/golden/service-report.golden.json` 逐字节比对 —— 任何契约性变化都必须显式更新 golden。

它解决什么（现有门禁的三个盲区）：
  · `validate-openapi.mjs` 只校验**路由集合**，不校验**响应字段**。
    实测：v6.1 给 `/health` 加了 `captcha_types`、v6.2 给 `/status` 加了 `wedge`，
    两个门禁全绿，但 `docs/API.md` 里一个字段都没写 —— 对接方永远不知道它们存在。
  · `validate-docs.mjs` 只校验**数字**（测试数/覆盖率/版本），不校验结构。
  · 单测用 `FakeBrowser` 在**进程内**跑，证明得了逻辑，证明不了"按文档起进程后
    实际吐出来的字段形状"。

做法（每个字段都只记**名字与类型**，绝不记值）：
  1. 起一个真进程（`python solver/service.py`，假浏览器不需要 —— 这里不起浏览器，
     只把它绑在 127.0.0.1 + `--no-auth`，验的是 HTTP 面）；
  2. 依次打全部端点（鉴权两态），把每个响应的 `status` + `Content-Type` +
     **字段名到类型的树**记下来；
  3. 与 golden 比对：不一致 → 打印人类可读 diff 并 exit 1。

为什么要"确定性"：字段顺序、集合顺序、时间戳都会让报告飘。所以：
  · JSON 键按字典序输出；
  · 数组只记**元素类型的并集**（不记长度、不记顺序）；
  · 时间戳 / uptime / pid / 缓存路径这类运行相关值一律替换成 `<volatile>`。

更新 golden：`python scripts/service_report.py --write`
（改完必须在同一 commit 里说明"为什么这个形状变了"。）

用法：
    python scripts/service_report.py            # 校验（CI / run.py verify 用）
    python scripts/service_report.py --write    # 重新生成 golden
    python scripts/service_report.py --base=http://127.0.0.1:8878   # 打已有服务（调试）
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

try:  # Windows 控制台默认 cp936，emoji / 特殊符号会直接抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:  # pragma: no cover
    pass

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "docs" / "golden" / "service-report.golden.json"

#: 端点的选取纪律：**只收"不依赖外网"的路径**。
#: `POST /detect` 的真实探测会发一次出站 GET，`/solve` 的真实求解会起浏览器 ——
#: 这两类都会让报告随网络与目标站变化，不再是"确定性"的。所以这里只用
#: 参数校验 / SSRF 拦截 / 兼容层错误这些**纯本地**路径来固化形状。
#: 真实求解/探测的形状由 `scripts/e2e_local.py` 与单测覆盖，不混进 golden。

#: 关于"环境差异"的处理见 `_normalize_health`：报告只记**类型**，不记值，
#: 所以 `state=ready/starting` 这类差异天然不进报告；需要归一化的只有 `/health` 的状态码。

#: 运行相关的字段：值会变，形状不变。记成 `<volatile>` 只保留类型信息。
#:
#: 收录标准："这台机器上跑出来是什么"不构成契约。特别地：
#:   · `last_snapshot_at` / `last_diff_at` —— 有无研究快照取决于**本机是否跑过
#:     `scripts/research.py`**。CI 上恒为 `null`、开发机上是 ISO 字符串。
#:     它们曾在 CI 上造成 `'string' → 'null'` 的假红（本机全绿），
#:     因为 golden 是拿"有快照的机器"生成的 —— 那不是门禁，那是环境指纹。
#:   · `cache_backend` —— 取决于有没有配 `--cache-dir`。
#:   · `browser` / `browser_os` 同理（认值会绑死引擎选择）。
VOLATILE_KEYS = {
    "pid", "uptime_sec", "last_success_at", "last_error_at", "ts", "request_id",
    "req_id", "cache_backend", "ua_fingerprint", "avg_elapsed_sec", "started",
    "cs_clearance", "cf_clearance", "token", "value", "port", "host_ts",
    # 只收**跨环境会变类型**的（`null` ↔ `string`），不收"类型恒定、只是数值不同"的：
    # 计数器加进来会把 `assert "number"` 这条检查也丢掉，等于自己削弱门禁。
    "last_snapshot_at", "last_diff_at",
}

#: 端点清单：(标签, method, path, 是否需要 token, 请求体)
#: 标签必须唯一 —— 同一 path 的不同用例（public / auth、不同请求体）会落到不同响应形状，
#: 共用一个键就会**互相覆盖**，而报告看起来仍然是"全部测过"。
ENDPOINTS: list[tuple[str, str, str, bool, dict | None]] = [
    ("statuspage",      "GET",  "/",             False, None),
    ("health-public",   "GET",  "/health",       False, None),
    ("health-auth",     "GET",  "/health",       True,  None),
    ("status",          "GET",  "/status",       True,  None),
    ("logs",            "GET",  "/logs?lines=5", True,  None),
    ("stats",           "GET",  "/stats",        True,  None),
    ("metrics",         "GET",  "/metrics",      True,  None),
    ("console",         "GET",  "/console",      True,  None),
    ("detect-ssrf",     "POST", "/detect",       True,  {"url": "http://127.0.0.1:1/"}),
    ("solve-ssrf",      "POST", "/solve",        True,  {"url": "http://127.0.0.1:1/", "want": "clearance"}),
    ("solve-bad-scheme", "POST", "/solve",       True,  {"url": "ftp://x/", "want": "clearance"}),
    ("compat-create",   "POST", "/createTask",   False, {"clientKey": "", "task": {"type": "AntiCloudflareTask"}}),
    ("compat-result",   "POST", "/getTaskResult", False, {"clientKey": "", "taskId": "nope"}),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shape(value, key: str = "") -> object:
    """把任意 JSON 值收敛成"只描述形状"的确定性结构。

    规则（就这四条，多一条都会让报告不稳定）：
      · dict → {"type":"object","fields":{k: shape(v)}}（键按字典序）
      · list → {"type":"array","items": <元素形状的并集>}（不记长度/顺序）
      · 标量 → 类型名；命中 VOLATILE_KEYS 的记 "<volatile>" 等价于只保留类型
    """
    if isinstance(value, dict):
        return {
            "type": "object",
            "fields": {k: shape(v, k) for k, v in sorted(value.items())},
        }
    if isinstance(value, list):
        merged: dict[str, object] = {}
        for item in value:
            s = shape(item, key)
            t = s if isinstance(s, str) else json.dumps(s, sort_keys=True, ensure_ascii=False)
            merged[t] = s
        return {
            "type": "array",
            "items": list(merged.values()),
        }
    # ⚠️ volatile 判断必须**排在类型判断之前**：`None` 曾经先返回 "null"，
    # 于是"本机有研究快照（string）vs CI 没有（null）"仍然被判成类型漂移 ——
    # 这正是 CI 抓到的那条假红。volatile 的语义是"这个字段的**值和有无**都不进契约"。
    if key in VOLATILE_KEYS:
        return "<volatile>"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "unknown"


VOLATILE_KEYS.add("generatedAt")


def _normalize_health(payload: object, status: int) -> tuple[object, object]:
    """只把**状态码**从报告里摘出去；字段形状照旧严格比。

    为什么只需要动状态码：`shape()` 本来就只记**类型**不记值 —— `state` 是 `ready`
    还是 `starting`、`browser_alive` 是 true 还是 false，都不进报告；进报告的是
    "这个键存在且是字符串/布尔"。所以删掉 `worker_alive` 照样红，而"本机没 fetch
    浏览器 → 503 starting"不会误伤。

    唯一会随环境变的是 `/health` 的 200 vs 503（CI 不下载 camoufox 二进制，本机有）。
    把它归一成 `<ready-or-starting>`，门禁才不会绑在某台机器上。
    """
    if status in (200, 503):
        status = "<ready-or-starting>"
    return payload, status


def request(base: str, method: str, path: str, token: str, body: dict | None):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "").split(";")[0]
            status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        ctype = e.headers.get("Content-Type", "").split(";")[0]
        status = e.code
    except Exception as e:  # noqa: BLE001
        return {"status": -1, "content_type": type(e).__name__, "body": "exception"}
    payload: object
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        # HTML（状态页/控制台）只记大小级别，不记内容 —— 内容是散文，天天在改
        text = raw.decode("utf-8", "replace")
        payload = {"_html": True, "doctype": text.lower().startswith("<!doctype")}
    if path == "/health":
        payload, status = _normalize_health(payload, status)
    return {"status": status, "content_type": ctype, "body": shape(payload)}


def build_report(base: str, token: str) -> dict:
    report: dict[str, dict] = {}
    for label, method, path, need_token, body in ENDPOINTS:
        report[label] = {
            "request": f"{method} {path}",
            "auth": need_token,
            **request(base, method, path, token if need_token else "", body),
        }
    return {"schema": 2, "endpoints": report}


def start_service(port: int, token_file: str) -> subprocess.Popen:
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    return subprocess.Popen(
        [sys.executable, "solver/service.py", "--port", str(port),
         "--token-file", token_file],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


def wait_ready(base: str, timeout: float = 60.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status in (200, 503):
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def diff(got: dict, want: dict, prefix: str = "") -> list[str]:
    """人类可读的形状差异（只报**增/删/类型变化**，不报值）。"""
    out: list[str] = []
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got)):
            path = f"{prefix}.{k}" if prefix else k
            if k not in got:
                out.append(f"- 缺少：{path}")
            elif k not in want:
                out.append(f"+ 新增：{path}")
            else:
                out.extend(diff(got[k], want[k], path))
    elif isinstance(want, list) and isinstance(got, list):
        gw = json.dumps(got, sort_keys=True, ensure_ascii=False)
        ww = json.dumps(want, sort_keys=True, ensure_ascii=False)
        if gw != ww:
            out.append(f"~ 数组形状变化：{prefix}")
    else:
        if got != want:
            out.append(f"~ 类型变化：{prefix}  {want!r} → {got!r}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="确定性服务报告 + golden 断言")
    ap.add_argument("--write", action="store_true", help="重新生成 golden（改契约时必须说明理由）")
    ap.add_argument("--base", default="", help="打已有服务（跳过自起进程）")
    args = ap.parse_args()

    if args.base:
        report = build_report(args.base, "")
        return _compare_or_write(report, args.write)

    port = free_port()
    workdir = Path(tempfile.mkdtemp(prefix="svc-report-"))
    token_file = workdir / "token.env"
    proc = start_service(port, str(token_file))
    base = f"http://127.0.0.1:{port}"
    try:
        if not wait_ready(base):
            log = proc.stdout.read() if proc.stdout else ""
            print("服务未能就绪：" + chr(10) + log[-2000:], file=sys.stderr)
            return 1
        token = _read_token(token_file)
        if not token:
            print(f"读不到服务端 token（{token_file} 为空）", file=sys.stderr)
            return 1
        report = build_report(base, token)
        return _compare_or_write(report, args.write)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:  # noqa: BLE001
            proc.kill()
        for leftover in workdir.glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


def _read_token(path: Path, timeout: float = 15.0) -> str:
    """从服务端写的 token 文件里读 Bearer token（首启是服务端生成的，不是我们给的）。"""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("CF_SOLVE_TOKEN="):
                    value = line.split("=", 1)[1].strip()
                    if value:
                        return value
        except OSError:
            pass
        time.sleep(0.2)
    return ""


def _compare_or_write(report: dict, write: bool) -> int:
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if write:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text, encoding="utf-8", newline="\n")
        print(f"golden 已更新：{GOLDEN}")
        return 0
    if not GOLDEN.exists():
        print(f"缺少 golden：{GOLDEN}\n先跑 python scripts/service_report.py --write", file=sys.stderr)
        return 1
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    problems = diff(report, want)
    if not problems:
        print(f"✅ 服务报告与 golden 一致（{len(report['endpoints'])} 个端点）")
        return 0
    print("FAIL 服务报告与 golden 不一致：", file=sys.stderr)
    for p in problems[:40]:
        print("   " + p, file=sys.stderr)
    print("\n如果这个变化是有意的：改同一处的文档（docs/API.md）后跑 "
          "`python scripts/service_report.py --write`，并在 commit 里说明理由。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())