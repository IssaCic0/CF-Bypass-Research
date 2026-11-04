"""`scripts/capture_sample.py` 的测试（v5.0.1 E2）。

核心思路：**用本地 HTTP 服务当"假 CF 站"**，这样能真跑采集链路（真 HTTP、真落盘、真 sha256）
却一个字节都不出网 —— 既不烧真机预算，也不受 CF 改版影响。

覆盖：
  1. 纯函数：`window._cf_chl_opt` 抠取、挑战脚本筛选、响应头白名单（Set-Cookie 只记有无）；
  2. dry-run **必须零网络**（把 fetch 换成"一调用就炸"的替身来证明）；
  3. 真抓：产物落盘、sha256 与内容一致、manifest 结构完整、detect 结论正确；
  4. 预算：`max_requests` 真的会限制请求数；
  5. 硬上限：超过 20 必须显式 `--allow-over-budget`，否则拒跑。
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import capture_sample as cs  # noqa: E402

CHL_OPT = "window._cf_chl_opt={cType:'managed', cvId:'3'}"
#: 抠出来的是**整条语句**（含结尾分号），这样才能直接当 chl_opt.js 存下来
CHL_OPT_STMT = CHL_OPT + ";"

PAGE_HTML = f"""<!doctype html><html><head>
<script>{CHL_OPT};</script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1.js"></script>
<script src="https://cdn.example.net/analytics.js"></script>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1.js"></script>
</head><body>checking…</body></html>"""

PLAIN_HTML = "<html><body>hello</body></html>"

JS_BODY = "/* challenge js */ console.log('v1');"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # noqa: N802 —— 静默，避免污染测试输出
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/page":
            body = PAGE_HTML.encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("cf-mitigated", "challenge")
            self.send_header("Set-Cookie", "cf_clearance=SECRET-VALUE; path=/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/plain":
            body = PLAIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/cdn-cgi/challenge-platform/"):
            body = JS_BODY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


@pytest.fixture
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        yield base
    finally:
        srv.shutdown()
        srv.server_close()


# ─────────────────────────────────────────── 纯函数


def test_extract_chl_opt():
    assert cs.extract_chl_opt(PAGE_HTML) == CHL_OPT_STMT
    assert cs.extract_chl_opt(PLAIN_HTML) == ""
    assert cs.extract_chl_opt("") == ""


def test_extract_chl_opt_handles_nested_braces():
    html = "window._cf_chl_opt={a:1, b:{c:2}, d:'x'};"
    assert cs.extract_chl_opt(html) == "window._cf_chl_opt={a:1, b:{c:2}, d:'x'};"


def test_find_challenge_scripts_filters_absolutizes_dedups():
    urls = cs.find_challenge_scripts(PAGE_HTML, "https://target.test/x")
    assert urls == ["https://target.test/cdn-cgi/challenge-platform/h/b/orchestrate/jsch/v1.js"], urls
    # 第三方脚本不抓（省预算）
    assert all("analytics" not in u for u in urls)
    # 去重
    assert len(urls) == len(set(urls))


def test_find_challenge_scripts_matches_jsd():
    html = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert cs.find_challenge_scripts(html, "https://t.test/") == [
        "https://t.test/cdn-cgi/challenge-platform/scripts/jsd/main.js"
    ]


def test_safe_headers_drops_cookie_value():
    picked = cs._safe_headers({
        "Set-Cookie": "cf_clearance=SECRET",
        "Content-Type": "text/html",
        "X-Tracking-Id": "abc",
    })
    assert "set-cookie" not in picked
    assert picked["set-cookie-present"] == "yes"
    assert "SECRET" not in json.dumps(picked)
    assert picked["content-type"] == "text/html"
    assert "X-Tracking-Id" not in picked and "x-tracking-id" not in picked


# ─────────────────────────────────────────── dry-run 零网络


def test_dry_run_makes_no_network_call(monkeypatch, tmp_path, capsys):
    def explode(*_a, **_k):
        raise AssertionError("dry-run 不该发任何请求")

    monkeypatch.setattr(cs, "fetch", explode)
    code = cs.main(["--url", "https://target.test/", "--out", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert code == 0
    assert "[dry-run]" in out
    assert not (tmp_path / "o").exists()


# ─────────────────────────────────────────── 真抓（本地假站）


def test_capture_writes_manifest_and_artifacts(site, tmp_path):
    out = tmp_path / "samples"
    manifest = cs.capture(f"{site}/page", out, ua="test-ua", timeout=5.0, max_requests=5)

    assert manifest["requests_made"] == 2, manifest        # 挑战页 + 1 个挑战脚本
    assert manifest["failures"] == 0
    assert manifest["page"]["status"] == 403
    assert manifest["page"]["headers"]["cf-mitigated"] == "challenge"
    assert manifest["page"]["headers"]["set-cookie-present"] == "yes"
    assert "SECRET-VALUE" not in json.dumps(manifest, ensure_ascii=False)

    # 产物齐备且 sha256 与内容一致
    for name in ("challenge_page.html", "chl_opt.js", "main_0.js", "manifest.json"):
        assert out.joinpath(name).is_file(), name
    page_bytes = out.joinpath("challenge_page.html").read_bytes()
    assert manifest["page"]["sha256"] == hashlib.sha256(page_bytes).hexdigest()
    assert out.joinpath("chl_opt.js").read_text(encoding="utf-8") == CHL_OPT_STMT
    assert out.joinpath("main_0.js").read_text(encoding="utf-8") == JS_BODY

    # 每个 artifact 都有 sha256，且与真实文件对得上
    for item in manifest["artifacts"]:
        assert item.get("file"), item
        data = out.joinpath(item["file"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(data).hexdigest()


def test_capture_records_detect_conclusion(site, tmp_path):
    manifest = cs.capture(f"{site}/page", tmp_path / "s", ua="test-ua", timeout=5.0, max_requests=5)
    det = manifest["detect"]
    assert det["cloudflare_challenge_type"] == "managed"
    assert any(p["id"] == "detect-cloudflare" for p in det["providers"])
    assert det["recommended_route"] == "browser"


def test_capture_on_plain_site_has_no_chl_opt(site, tmp_path):
    out = tmp_path / "s2"
    manifest = cs.capture(f"{site}/plain", out, ua="test-ua", timeout=5.0, max_requests=5)
    assert manifest["requests_made"] == 1
    assert not out.joinpath("chl_opt.js").exists()
    assert manifest["detect"]["recommended_route"] == "direct"


def test_capture_respects_request_budget(site, tmp_path):
    """预算 1 = 只抓挑战页，绝不顺带抓脚本。"""
    manifest = cs.capture(f"{site}/page", tmp_path / "s3", ua="test-ua", timeout=5.0, max_requests=1)
    assert manifest["requests_made"] == 1
    files = [a["file"] for a in manifest["artifacts"]]
    # chl_opt.js 是从**已抓到的页面**里本地抠的，不消耗请求；不能有 main_*.js
    assert files == ["challenge_page.html", "chl_opt.js"], files
    assert not any(f.startswith("main_") for f in files)


def test_capture_records_failure_without_crashing(tmp_path):
    """连不上目标：manifest 仍要落盘（含 error），进程不许抛栈 —— 采集失败也是证据。"""
    manifest = cs.capture("http://127.0.0.1:1/none", tmp_path / "s4", ua="test-ua",
                          timeout=2.0, max_requests=3)
    assert manifest["failures"] == 1
    assert manifest["error"]
    assert tmp_path.joinpath("s4", "manifest.json").is_file()


# ─────────────────────────────────────────── 预算硬闸


def test_hard_budget_is_refused_without_flag(tmp_path, capsys):
    code = cs.main(["--url", "https://t.test/", "--out", str(tmp_path), "--max-requests", "50"])
    assert code == 2
    assert "硬上限" in capsys.readouterr().err


def test_max_requests_must_be_positive(tmp_path):
    assert cs.main(["--url", "https://t.test/", "--out", str(tmp_path), "--max-requests", "0"]) == 2


def test_real_flag_runs_capture(site, tmp_path, capsys):
    code = cs.main(["--url", f"{site}/page", "--out", str(tmp_path / "real"), "--real",
                    "--max-requests", "3", "--timeout", "5"])
    out = capsys.readouterr().out
    assert code == 0
    assert "完成：" in out
    assert "detect：route=browser" in out
