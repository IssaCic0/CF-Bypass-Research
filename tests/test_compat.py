"""YesCaptcha 兼容层测试（v5.0.1 E1）。

三层：
  1. **纯函数**：`task.type` → `want` 的映射、`solution` 结构。脱离 HTTP，毫秒级；
  2. **注册表**：TTL 过期、待处理上限、错误映射、任何异常都必须落成结果（不能永远 processing）；
  3. **HTTP 契约**：鉴权（Bearer / clientKey）、业务错误一律 200+errorId、
     传输层畸形仍 400/413、未知 taskId、拒绝任务级代理。

关于"求解本身"：这里用替身 Solver，所以只证明**兼容层的协议正确**，不证明真浏览器能过盾——
后者由 `scripts/e2e_local.py` 与真机实验负责（policy.md 的分层验证口径）。
"""

from __future__ import annotations

import json
import time

import pytest

from solver.compat import (
    COMPAT_ERROR_BY_CODE,
    ERR_KEY,
    ERR_NO_SLOT,
    ERR_PARAM,
    ERR_PROXY,
    ERR_TASK_ID,
    ERR_TYPE,
    ERR_UNSOLVABLE,
    TASK_TYPE_MAP,
    CompatError,
    CompatTask,
    TaskRegistry,
    solution_from_result,
    spec_from_task,
)
from solver.challenges import Want
from solver.errors import ErrorCode, SolveError
from test_service import _post, live_server, wait_until  # noqa: F401 —— fixture 需被收集


# ─────────────────────────────────────────── 1. 纯函数：type → want


def test_supported_task_types_are_exactly_three():
    """刻意只做 3 种。这条用例是"契约边界"的守门人：加类型必须同步改文档与用例。"""
    assert set(TASK_TYPE_MAP) == {
        "AntiCloudflareTask",
        "TurnstileTaskProxyless",
        "RecaptchaV3TaskProxyless",
    }
    assert TASK_TYPE_MAP["AntiCloudflareTask"] is Want.CLEARANCE
    assert TASK_TYPE_MAP["TurnstileTaskProxyless"] is Want.TURNSTILE
    assert TASK_TYPE_MAP["RecaptchaV3TaskProxyless"] is Want.RECAPTCHA_V3


def test_clearance_task_needs_only_url():
    spec = spec_from_task({"type": "AntiCloudflareTask", "websiteURL": "https://e.com/"})
    assert spec.want is Want.CLEARANCE
    assert spec.url == "https://e.com/"
    assert spec.sitekey == ""


def test_turnstile_task_maps_action_and_cdata():
    spec = spec_from_task({
        "type": "TurnstileTaskProxyless", "websiteURL": "https://e.com/",
        "websiteKey": "0xKEY", "action": "login", "cdata": "abc",
    })
    assert (spec.want, spec.sitekey, spec.action, spec.cdata) == (Want.TURNSTILE, "0xKEY", "login", "abc")


def test_recaptcha_task_maps_pageAction_to_action():
    spec = spec_from_task({
        "type": "RecaptchaV3TaskProxyless", "websiteURL": "https://e.com/",
        "websiteKey": "6LcKEY", "pageAction": "homepage",
    })
    assert spec.want is Want.RECAPTCHA_V3
    assert spec.action == "homepage"


def test_unknown_type_lists_supported_ones():
    with pytest.raises(CompatError) as ei:
        spec_from_task({"type": "ImageToTextTask", "websiteURL": "https://e.com/"})
    assert ei.value.error_code == ERR_TYPE
    assert "AntiCloudflareTask" in ei.value.description


@pytest.mark.parametrize("task", [None, [], "str", 42])
def test_task_must_be_object(task):
    with pytest.raises(CompatError) as ei:
        spec_from_task(task)
    assert ei.value.error_code == ERR_PARAM


@pytest.mark.parametrize("payload", [
    {"type": "AntiCloudflareTask"},                                  # 缺 websiteURL
    {"type": "AntiCloudflareTask", "websiteURL": ""},                 # 空 url
    {"type": "AntiCloudflareTask", "websiteURL": "e.com"},            # 无 scheme
    {"type": "TurnstileTaskProxyless", "websiteURL": "https://e/"},    # 缺 websiteKey
    {"type": "", "websiteURL": "https://e.com/"},                     # 空 type
])
def test_param_errors(payload):
    with pytest.raises(CompatError) as ei:
        spec_from_task(payload)
    assert ei.value.error_code == ERR_PARAM


@pytest.mark.parametrize("field_name", ["proxyType", "proxyAddress", "proxyPort", "proxyLogin", "proxyPassword"])
def test_task_level_proxy_is_rejected(field_name):
    """拒绝任务级代理不是"没实现"：它会导致求解/回放出口不一致 → 稳定静默 403。"""
    with pytest.raises(CompatError) as ei:
        spec_from_task({
            "type": "AntiCloudflareTask", "websiteURL": "https://e.com/", field_name: "x",
        })
    assert ei.value.error_code == ERR_PROXY
    assert "--egress-id" in ei.value.description


def test_compat_error_payload_shape():
    p = CompatError(ERR_PARAM, "x").payload()
    assert p == {"errorId": 1, "errorCode": ERR_PARAM, "errorDescription": "x"}


# ─────────────────────────────────────────── 1b. 纯函数：solution 结构


def test_solution_for_clearance_uses_cookies():
    sol = solution_from_result(Want.CLEARANCE, {
        "cf_clearance": "CLEAR", "cookies": {"cf_clearance": "CLEAR"}, "user_agent": "UA",
    })
    assert sol == {"cookies": {"cf_clearance": "CLEAR"}, "userAgent": "UA"}


def test_solution_for_clearance_falls_back_to_token_field():
    sol = solution_from_result(Want.CLEARANCE, {"cf_clearance": "CLEAR", "user_agent": "UA"})
    assert sol["cookies"] == {"cf_clearance": "CLEAR"}


def test_solution_for_turnstile_uses_token():
    sol = solution_from_result(Want.TURNSTILE, {"token": "TOK", "user_agent": "UA"})
    assert sol == {"token": "TOK", "userAgent": "UA"}


def test_solution_for_recaptcha_uses_grecaptcharesponse():
    sol = solution_from_result(Want.RECAPTCHA_V3, {"token": "TOK", "user_agent": "UA"})
    assert sol == {"gRecaptchaResponse": "TOK", "userAgent": "UA"}


def test_error_mapping_covers_every_error_code():
    """每个我方错误码都必须有对应的 YesCaptcha 错误码，否则客户端会收到空 errorCode。"""
    missing = [c for c in ErrorCode if c not in COMPAT_ERROR_BY_CODE]
    assert not missing, f"这些错误码没有映射：{missing}"
    assert COMPAT_ERROR_BY_CODE[ErrorCode.UNSOLVED] == ERR_UNSOLVABLE


# ─────────────────────────────────────────── 2. 注册表


class FakeSolver:
    """替身：只实现兼容层会用到的 `solve()`。"""

    def __init__(self, outcome="ok", delay=0.0):
        self.outcome = outcome
        self.delay = delay
        self.calls = []

    def solve(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.delay:
            time.sleep(self.delay)
        if self.outcome == "raise":
            raise SolveError(ErrorCode.BUSY, "队列满", stage="admit")
        if self.outcome == "unsolved":
            return {"ok": True, "solved": False, "error": "没拿到凭证"}
        return {"ok": True, "solved": True, "token": "TOK", "user_agent": "UA"}


def wait_task(reg, task_id, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rec = reg.result(task_id)
        if rec.status == "ready":
            return rec
        time.sleep(0.01)
    raise AssertionError("任务在超时内没有完成")


def make_task():
    return {"type": "TurnstileTaskProxyless", "websiteURL": "https://e.com/", "websiteKey": "k"}


def test_registry_happy_path():
    # 给一点延迟：否则任务线程可能在第一次断言前就跑完，"processing" 无从观察
    reg = TaskRegistry(FakeSolver(delay=0.05))
    rec = reg.create(make_task())
    assert rec.status == "processing"
    done = wait_task(reg, rec.task_id)
    assert done.solution == {"token": "TOK", "userAgent": "UA"}


def test_registry_passes_normalized_intent_to_solver():
    fs = FakeSolver()
    reg = TaskRegistry(fs)
    rec = reg.create(make_task())
    wait_task(reg, rec.task_id)
    url, kwargs = fs.calls[0]
    assert url == "https://e.com/"
    assert kwargs["want"] == "turnstile"
    assert kwargs["sitekey"] == "k"
    assert kwargs["force"] is False


def test_registry_unsolved_becomes_unsolvable_error():
    reg = TaskRegistry(FakeSolver(outcome="unsolved"))
    rec = reg.create(make_task())
    done = wait_task(reg, rec.task_id)
    assert done.error_code == ERR_UNSOLVABLE
    assert "没拿到凭证" in done.error_description


def test_registry_solve_error_is_mapped_not_swallowed():
    reg = TaskRegistry(FakeSolver(outcome="raise"))
    rec = reg.create(make_task())
    done = wait_task(reg, rec.task_id)
    assert done.error_code == "ERROR_SERVICE_OVERLOAD"   # BUSY → 过载


def test_registry_unknown_task_id():
    reg = TaskRegistry(FakeSolver())
    with pytest.raises(CompatError) as ei:
        reg.result("nope")
    assert ei.value.error_code == ERR_TASK_ID


def test_registry_ttl_purges_results():
    reg = TaskRegistry(FakeSolver(), ttl=0.05)
    rec = reg.create(make_task())
    wait_task(reg, rec.task_id)
    time.sleep(0.12)
    with pytest.raises(CompatError) as ei:
        reg.result(rec.task_id)
    assert ei.value.error_code == ERR_TASK_ID


def test_registry_rejects_when_pending_cap_reached():
    reg = TaskRegistry(FakeSolver(delay=0.5), max_pending=1)
    reg.create(make_task())
    with pytest.raises(CompatError) as ei:
        reg.create(make_task())
    assert ei.value.error_code == ERR_NO_SLOT


def test_registry_invalid_task_does_not_consume_slot():
    """校验在前、占位在后：非法任务不该把待处理额度吃掉。"""
    reg = TaskRegistry(FakeSolver(delay=0.3), max_pending=1)
    with pytest.raises(CompatError):
        reg.create({"type": "ImageToTextTask", "websiteURL": "https://e.com/"})
    reg.create(make_task())   # 仍能建


def test_registry_stats_and_close():
    reg = TaskRegistry(FakeSolver(delay=0.2), max_pending=4)
    reg.create(make_task())
    st = reg.stats()
    assert st["max_pending"] == 4
    assert st["pending"] >= 1
    reg.close()
    with pytest.raises(CompatError):
        reg.create(make_task())


def test_task_payload_shapes():
    spec = spec_from_task(make_task())
    processing = CompatTask(task_id="t", spec=spec)
    assert processing.result_payload() == {"status": "processing"}

    ready = CompatTask(task_id="t", spec=spec, status="ready", solution={"token": "x"})
    assert ready.result_payload() == {"status": "ready", "solution": {"token": "x"}}

    failed = CompatTask(task_id="t", spec=spec, status="ready", error_code=ERR_UNSOLVABLE,
                        error_description="nope")
    payload = failed.result_payload()
    assert payload["errorId"] == 1 and payload["errorCode"] == ERR_UNSOLVABLE


# ─────────────────────────────────────────── 3. HTTP 契约


def _create(base, body, token=None):
    return _post(base, "/createTask", body, token=token)


def _result(base, task_id, token=None, client_key=None):
    body = {"taskId": task_id}
    if client_key is not None:
        body["clientKey"] = client_key
    return _post(base, "/getTaskResult", body, token=token)


def test_create_task_without_any_credential_is_error_id_1(live_server):
    """生态客户端只看 errorId：缺凭据也必须 HTTP 200 + errorId=1，否则会被当网络错误狂重试。"""
    status, body = _create(live_server, {"task": make_task()})
    assert status == 200
    assert body["errorId"] == 1
    assert body["errorCode"] == ERR_KEY


def test_create_task_accepts_client_key_in_body(live_server):
    status, body = _create(live_server, {"clientKey": "test-token", "task": make_task()})
    assert status == 200
    assert body["errorId"] == 0
    assert len(body["taskId"]) == 16


def test_create_task_accepts_bearer(live_server):
    status, body = _create(live_server, {"task": make_task()}, token="test-token")
    assert status == 200 and body["errorId"] == 0


def test_create_task_rejects_wrong_client_key(live_server):
    status, body = _create(live_server, {"clientKey": "wrong", "task": make_task()})
    assert status == 200 and body["errorId"] == 1 and body["errorCode"] == ERR_KEY


def test_create_task_transport_error_stays_400(live_server):
    """非 JSON 是客户端 bug，不是业务失败 —— 不能也塞进 errorId=1。"""
    status, body = _post(live_server, "/createTask", b"not-json", token="test-token")
    assert status == 400
    assert body["error_code"] == "invalid-request"


def test_get_task_result_unknown_id(live_server):
    status, body = _result(live_server, "deadbeefdeadbeef", token="test-token")
    assert status == 200
    assert body["errorId"] == 1 and body["errorCode"] == ERR_TASK_ID


def test_full_async_flow_reaches_ready(live_server):
    """真实 HTTP 异步闭环：createTask → 轮询 → ready（这里用假浏览器，故是"可预期的失败"）。

    这条验的是**协议闭环**：任务能被创建、能被查到、且**一定**会走到结论
    （不会永远 processing —— 那是最坏的用户体验）。
    """
    status, body = _create(live_server, {"clientKey": "test-token", "task": {
        "type": "AntiCloudflareTask", "websiteURL": "https://example.com/",
    }})
    assert status == 200 and body["errorId"] == 0
    task_id = body["taskId"]

    def finished():
        code, payload = _result(live_server, task_id, token="test-token")
        assert code == 200
        if payload.get("status") == "ready":
            # 协议不变量（不预设假浏览器一定失败/成功 —— 那是替身行为，不是契约）：
            #   errorId=0 必须有 solution；errorId=1 必须有已知 errorCode。
            if payload.get("errorId") == 0:
                assert payload.get("solution"), "errorId=0 却没有 solution"
            else:
                assert payload["errorCode"].startswith("ERROR_"), payload
            return True
        return False

    assert wait_until(finished, timeout=20.0), "任务在 20 秒内没有得出结论"


def test_status_endpoint_exposes_compat_counters(live_server):
    """`/status` 带上兼容层计数，运维能一眼看出"兼容层有没有在积压"。"""
    from test_service import _get

    status, body = _get(live_server, "/status", token="test-token")
    assert status == 200
    compat = json.loads(body)["compat"]
    assert compat["max_pending"] >= 1
    assert "pending" in compat and "created_total" in compat


# ─────────────────────────────────────────── 复审补充：兜底与上限

def test_thread_start_failure_rolls_back_registration(monkeypatch):
    """线程起不来时必须回滚登记，否则字典里留下一条永远 processing 的记录占着配额（复审 M2）。"""
    import threading as _threading

    class Boom(_threading.Thread):
        def start(self):  # noqa: D401
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(_threading, "Thread", Boom)
    reg = TaskRegistry(FakeSolver(), max_pending=2)
    with pytest.raises(CompatError) as ei:
        reg.create(make_task())
    assert "无法启动任务线程" in ei.value.description
    # 配额没有被僵尸记录占掉：恢复线程后再建能成功
    monkeypatch.undo()
    reg.create(make_task())
    assert reg.stats()["created_total"] == 1


def test_solver_returning_non_dict_becomes_internal_error_not_stuck():
    """solve() 返回非 dict 也必须落成结论，不能永远 processing（复审 M1 的核心场景）。"""

    class WeirdSolver:
        def solve(self, url, **kw):
            return None          # 未来改坏了：返回 None

    reg = TaskRegistry(WeirdSolver())
    rec = reg.create(make_task())
    done = wait_task(reg, rec.task_id)
    assert done.status == "ready"
    assert done.error_code == "ERROR_INTERNAL"
    assert "NoneType" in done.error_description


def test_finish_is_idempotent():
    """兜底路径不许覆盖已得出的结论。"""
    reg = TaskRegistry(FakeSolver())
    rec = reg.create(make_task())
    done = wait_task(reg, rec.task_id)
    before = dict(done.solution or {})
    reg._finish(done, error_code="ERROR_INTERNAL", error_description="兜底不该生效")
    assert done.status == "ready"
    assert done.solution == before
    assert done.error_code == ""


def test_retained_ready_tasks_are_capped(monkeypatch):
    """ready 记录要有保留上限，否则 600s 内高频 create 能把 _tasks 涨到几千条。"""
    reg = TaskRegistry(FakeSolver(), max_pending=1)
    for _ in range(10):
        rec = reg.create(make_task())
        wait_task(reg, rec.task_id)
        reg.stats()      # 每次 stats 会触发清理
    assert reg.stats()["retained"] <= 1 * TaskRegistry.RETAIN_FACTOR


def test_purge_does_not_remove_processing_task():
    """还在跑的任务不能被过期清理删掉（否则客户端从 processing 直接跳到 TASK_ID_INVALID）。"""
    reg = TaskRegistry(FakeSolver(delay=0.3), ttl=0.01, max_pending=4)
    rec = reg.create(make_task())
    time.sleep(0.05)                 # 超过 ttl，但任务仍在跑
    assert reg.result(rec.task_id).status == "processing"
    wait_task(reg, rec.task_id)


def test_sitekey_length_is_capped_like_solve():
    """兼容层的 websiteKey 必须与 /solve 共用同一个长度上限（复审 MINOR-13）。"""
    from solver.request import MAX_TOKEN_FIELD_LEN

    long_key = "k" * (MAX_TOKEN_FIELD_LEN + 1)
    with pytest.raises(CompatError) as ei:
        spec_from_task({"type": "TurnstileTaskProxyless",
                        "websiteURL": "https://e.com/", "websiteKey": long_key})
    assert ei.value.error_code == ERR_PARAM
    assert "websiteKey" in ei.value.description
