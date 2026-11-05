"""YesCaptcha / 2Captcha 风格的异步任务兼容层（v5.0.1 E1）。

**它只是适配层，不是第二条求解路径**：内部仍然调用 `Solver.solve()` ——
同一份缓存、同一套准入、同一份指标。不调用这两个端点时，`/solve` 的行为一个字节都没变。

为什么需要（真实痛点）：存量爬虫栈大多按 YesCaptcha 的 `createTask`/`getTaskResult`
异步任务协议写死了代码，而本服务原生是**同步** `POST /solve`。没有这一层，接入方要自己
写一个轮询适配器。契约见 `docs/API.md` 的「YesCaptcha 兼容层」一节。

两条刻意的拒绝，都不是"没实现"：
  1. **不接受任务级代理**。本项目的凭证是"绑定求解时出口 IP"的，回放侧按 `--egress-id`
     选出口。允许任务级代理就会出现"求解走 A、回放走 B"→ 稳定的静默 403 ——
     与 `service.py` 里 `--proxy` 和 `--cfsolve-bin/--pydoll` 被设为**启动硬冲突**是同一条原因。
  2. **不提供 getBalance**。没有计费模型，返回假余额只会误导客户端。

线程与资源：每个任务一个守护线程；待处理上限默认 64；结果保留 10 分钟。
这三条是为了让"兼容层"不变成资源泄漏入口。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field

try:  # 作为包导入
    from .challenges import RESULT_FIELD, Want
    from .errors import ErrorCode, SolveError
    from .request import MAX_TOKEN_FIELD_LEN
except ImportError:  # 直接以脚本运行：python solver/service.py
    from challenges import RESULT_FIELD, Want  # type: ignore[no-redef]
    from errors import ErrorCode, SolveError  # type: ignore[no-redef]
    from request import MAX_TOKEN_FIELD_LEN  # type: ignore[no-redef]

def _host_of(url: str) -> str:
    """取主机名（不依赖 service.py，避免循环导入）。"""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


#: 结果保留时长（秒）。超过就当"没这个任务"——比返回一个永远 processing 的僵尸任务诚实。
COMPAT_TASK_TTL = 600.0
#: 待处理任务上限。满了立刻拒绝（对应 `ERROR_NO_SLOT_AVAILABLE`），不做无界排队。
DEFAULT_MAX_PENDING = 64
#: 单个兼容任务的求解预算（秒）。与 `/solve` 的默认值一致。
COMPAT_SOLVE_TIMEOUT = 45.0

#: `task.type` → 主链路 `want`。**只放主链路真解得了、且语义一对一的**。
TASK_TYPE_MAP: dict[str, Want] = {
    "AntiCloudflareTask": Want.CLEARANCE,
    "TurnstileTaskProxyless": Want.TURNSTILE,
    "TurnstileTaskProxylessM1": Want.TURNSTILE,          # M1 = 上游的模型变体，对我们等价
    "RecaptchaV3TaskProxyless": Want.RECAPTCHA_V3,
    "RecaptchaV3TaskProxylessM1": Want.RECAPTCHA_V3,
    "RecaptchaV3TaskProxylessM1S7": Want.RECAPTCHA_V3,
    "RecaptchaV3TaskProxylessM1S9": Want.RECAPTCHA_V3,
}

#: `task.type` → 万盾嫁接层类型（captchas/ 子包，v6.1 起）。值是 `CAPTCHA_TYPES` 的键。
#: 与 `TASK_TYPE_MAP` 分立的原因：这两条路的产物形状、缓存语义与依赖完全不同
#: （主链路 = Camoufox/pydoll/cfsolve；嫁接层 = CloakBrowser），混在一张表里
#: 迟早有人把 `want` 传进嫁接层。
MC_TASK_TYPE_MAP: dict[str, str] = {
    # —— reCAPTCHA v2（主链路没有 v2，只有 v3）——
    "NoCaptchaTaskProxyless": "recaptcha_x",
    "RecaptchaV2TaskProxyless": "recaptcha_x",
    "RecaptchaV2EnterpriseTaskProxyless": "recaptcha_x",
    # —— Enterprise v3：主链路 Want 没有 enterprise 开关，走嫁接层 ——
    "RecaptchaV3EnterpriseTask": "recaptcha_x",
    "RecaptchaV3EnterpriseTaskM1": "recaptcha_x",
    # —— 其它厂商 ——
    "HCaptchaTaskProxyless": "hcaptcha",
    "FunCaptchaTaskProxyless": "arkose",
    # 这些是本仓库的**扩展名**（YesCaptcha 官方没有对应 task type，但我们真解得了）：
    # 命名沿用生态的 `xxxTaskProxyless` 风格，文档里明确标注为扩展。
    "DataDomeTaskProxyless": "datadome",
    "PerimeterXTaskProxyless": "perimeterx",
    "AkamaiTaskProxyless": "akamai",
    "AwsWafTaskProxyless": "awswaf_x",
    "CloudflareTurnstileTaskProxyless": "turnstile_x",
}

#: 生态里存在、但我们**确实解不了**的类型 → 明确拒绝 + 原因。
#: 不做成"静默 unsupported"：客户端要能区分"我们不做"与"这次没解出来"。
UNSUPPORTED_TASK_TYPES: dict[str, str] = {
    "ImageToTextTask": "图片文字识别需要视觉模型后端（本服务不内置）",
    "ImageToTextTaskMuggle": "图片文字识别需要视觉模型后端（本服务不内置）",
    "ImageToTextTaskM1": "图片文字识别需要视觉模型后端（本服务不内置）",
    "FunCaptchaClassification": "纯图片分类需要视觉模型后端（本服务不内置）",
    "HCaptchaClassification": "纯图片分类需要视觉模型后端（本服务不内置）",
    "ReCaptchaV2Classification": "纯图片分类需要视觉模型后端（本服务不内置）",
    "AwsClassification": "纯图片分类需要视觉模型后端（本服务不内置）",
    "AntiTurnstileTaskProxyLess": "Turnstile 请用 TurnstileTaskProxyless",
}

#: 任务级代理字段：一律拒绝（见模块 docstring 的拒绝 1）。
PROXY_FIELDS = ("proxyType", "proxyAddress", "proxyPort", "proxyLogin", "proxyPassword")

#: 我方错误码 → YesCaptcha 错误码。
COMPAT_ERROR_BY_CODE: dict[ErrorCode, str] = {
    ErrorCode.UNAUTHORIZED: "ERROR_KEY_DOES_NOT_EXIST",
    ErrorCode.INVALID_REQUEST: "ERROR_TASK_PARAM_INVALID",
    ErrorCode.PAYLOAD_TOO_LARGE: "ERROR_TASK_PARAM_INVALID",
    ErrorCode.BUSY: "ERROR_SERVICE_OVERLOAD",
    ErrorCode.RATE_LIMITED: "ERROR_SERVICE_OVERLOAD",
    ErrorCode.NOT_CONFIGURED: "ERROR_INTERNAL",
    ErrorCode.BROWSER_UNAVAILABLE: "ERROR_INTERNAL",
    ErrorCode.TIMEOUT: "ERROR_TIMEOUT",
    ErrorCode.TRANSPORT: "ERROR_TRANSPORT",
    ErrorCode.INTERNAL: "ERROR_INTERNAL",
    ErrorCode.UNSOLVED: "ERROR_CAPTCHA_UNSOLVABLE",
}

# ── 错误码（v6.2：对齐 YesCaptcha 生态的真实词表）──
#
# 为什么要改：v5.0.1 用的是我们自造的词（ERROR_TASK_TYPE_NOT_SUPPORTED /
# ERROR_TASK_PARAM_INVALID / ERROR_TASK_ID_INVALID）。按 YesCaptcha 协议写死的客户端
# 是**按 errorCode 分支**的，改 base_url 过来后这三类错误全部落进 default 分支 ——
# 兼容层"能连上但错误处理全错"，比连不上更坏。
# 现在的词表取自生态实际用法（对照 ohmycaptcha 的实现与 YesCaptcha 文档）：
#   ERROR_TASK_NOT_SUPPORTED / ERROR_TASK_PROPERTY_EMPTY /
#   ERROR_NO_SUCH_CAPCHA_ID（生态里就是这个拼写，不要"修正"成 CAPTCHA）
ERR_KEY = "ERROR_KEY_DOES_NOT_EXIST"
#: 未知 task.type
ERR_TYPE = "ERROR_TASK_NOT_SUPPORTED"
#: 必填字段缺失/为空（生态名）
ERR_PROPERTY_EMPTY = "ERROR_TASK_PROPERTY_EMPTY"
#: 字段存在但值非法（超长/类型错/不是 http）——生态里没有专用码，沿用语义最接近的
#: PROPERTY_EMPTY 会误导（客户端以为"再传一次就好"），所以保留一个通明确的码，
#: 并在 docs/API.md 注明是扩展。
ERR_PARAM = "ERROR_TASK_PARAM_INVALID"
ERR_PROXY = "ERROR_PROXY_NOT_SUPPORTED"
#: taskId 不存在/已过期（生态的拼写确实是 CAPCHA）
ERR_TASK_ID = "ERROR_NO_SUCH_CAPCHA_ID"
ERR_NO_SLOT = "ERROR_NO_SLOT_AVAILABLE"
ERR_UNSOLVABLE = "ERROR_CAPTCHA_UNSOLVABLE"


class CompatError(Exception):
    """兼容层的业务错误（对应 HTTP 200 + `errorId=1`）。"""

    def __init__(self, error_code: str, description: str) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description

    def payload(self) -> dict:
        return {"errorId": 1, "errorCode": self.error_code, "errorDescription": self.description}


@dataclass(frozen=True)
class CompatSpec:
    """一个兼容任务被规范化后的求解意图。

    两条执行路径（v6.2 起）：
      · `mc_type == ""` → 主链路，`want` 有值，调 `Solver.solve()`
      · `mc_type != ""` → 万盾嫁接层，`want` 为 None，调 `multicaptcha.solve_async()`
    为什么要分开：两层的产物形状、依赖（Camoufox vs CloakBrowser）与缓存语义都不同；
    用一个 `want` 字段硬塞两种语义，会在 `_run_inner` 里写出"看起来能跑"的分支。
    """

    want: Want | None
    url: str
    sitekey: str = ""
    action: str = ""
    cdata: str = ""
    #: 万盾嫁接层类型（`multicaptcha.CAPTCHA_TYPES` 的键）；空 = 走主链路
    mc_type: str = ""
    #: 嫁接层的额外参数（version/enterprise/public_key/referer/render_flow/...）
    mc_extra: dict = field(default_factory=dict)

    @property
    def want_value(self) -> str:
        return self.want.value if self.want is not None else self.mc_type


def spec_from_task(task: object) -> CompatSpec:
    """把 YesCaptcha 的 `task` 对象翻译成本服务的求解意图（纯函数）。

    任何不合法都抛 `CompatError`；错误码与描述按 `docs/API.md` 的契约表。
    """
    if not isinstance(task, dict):
        raise CompatError(ERR_PROPERTY_EMPTY, "task 必须是对象")

    task_type = task.get("type")
    if not isinstance(task_type, str) or not task_type:
        raise CompatError(ERR_PROPERTY_EMPTY, "task.type 必须是非空字符串")

    for field_name in PROXY_FIELDS:
        if field_name in task:
            raise CompatError(
                ERR_PROXY,
                "不接受任务级代理（" + field_name + "）：本服务的凭证绑定求解时的出口 IP，"
                "任务级代理会导致求解与回放出口不一致 → 稳定 403。请用启动参数 --proxy / --egress-id",
            )

    want = TASK_TYPE_MAP.get(task_type)
    mc_type = MC_TASK_TYPE_MAP.get(task_type)
    if want is None and mc_type is None:
        reason = UNSUPPORTED_TASK_TYPES.get(task_type)
        supported = sorted(set(TASK_TYPE_MAP) | set(MC_TASK_TYPE_MAP))
        raise CompatError(
            ERR_TYPE,
            (f"task.type={task_type!r} 本服务不做：{reason}。"
             if reason else f"未知 task.type={task_type!r}。")
            + f"支持的 {len(supported)} 种见 docs/API.md；此处列出：{supported}",
        )

    url = task.get("websiteURL")
    if not isinstance(url, str) or not url:
        raise CompatError(ERR_PROPERTY_EMPTY, "task.websiteURL 必填")
    if not url.startswith(("http://", "https://")):
        raise CompatError(ERR_PARAM, "task.websiteURL 必须是 http(s) 绝对地址")

    sitekey = ""
    action = ""
    cdata = ""
    if want in (Want.TURNSTILE, Want.RECAPTCHA_V3):
        sitekey = task.get("websiteKey") or ""
        if not isinstance(sitekey, str) or not sitekey:
            raise CompatError(ERR_PROPERTY_EMPTY,
                              "task.websiteKey 必填（Turnstile / reCAPTCHA 都需要 sitekey）")
        if want is Want.TURNSTILE:
            action = task.get("action") or ""
            cdata = task.get("cdata") or ""
        else:
            # YesCaptcha 的 reCAPTCHA v3 用 pageAction；我们透传给 --action
            action = task.get("pageAction") or ""
        if not isinstance(sitekey, str) or len(sitekey) > MAX_TOKEN_FIELD_LEN:
            raise CompatError(
                ERR_PARAM, f"websiteKey 必须是不超过 {MAX_TOKEN_FIELD_LEN} 字符的字符串"
            )
        for name, value in (("action", action), ("cdata", cdata)):
            if not isinstance(value, str) or len(value) > MAX_TOKEN_FIELD_LEN:
                raise CompatError(
                    ERR_PARAM, f"{name} 必须是不超过 {MAX_TOKEN_FIELD_LEN} 字符的字符串"
                )

    if mc_type:
        # 嫁接层分支：字段校验按该类型的最小集合（与 /solve 旁路同一套规则）
        extra: dict = {}
        _sitekey = task.get("websiteKey") or task.get("public_key") or ""
        if mc_type in ("recaptcha_x", "hcaptcha", "turnstile_x") and not _sitekey:
            raise CompatError(ERR_PROPERTY_EMPTY, "task.websiteKey 必填（该类型需要 sitekey）")
        if mc_type == "recaptcha_x":
            # 由 task.type 决定变体：Enterprise / v2 / 默认 v3
            if "Enterprise" in task_type:
                extra["enterprise"] = True
            if "V2" in task_type or task_type == "NoCaptchaTaskProxyless":
                extra["version"] = "v2"
            else:
                extra["version"] = "v3"
            action = task.get("pageAction") or task.get("action") or ""
        if mc_type == "arkose":
            extra["public_key"] = _sitekey
        if mc_type == "datadome":
            extra["referer"] = task.get("referer") or ""
        if mc_type == "perimeterx":
            extra["render_flow"] = task.get("render_flow") or ""
        return CompatSpec(want=None, url=url, sitekey=_sitekey, action=action,
                          mc_type=mc_type, mc_extra=extra)

    return CompatSpec(want=want, url=url, sitekey=sitekey, action=action, cdata=cdata)


def solution_from_result(want: Want, result: dict, mc_type: str = "") -> dict:
    """把结果翻译成 YesCaptcha 的 `solution` 结构（纯函数）。

    产物字段名各不相同，这不是我们发明的，是生态既定契约：
      · clearance      → `solution.cookies.cf_clearance` + `userAgent`
      · turnstile      → `solution.token` + `userAgent`
      · recaptcha_v3   → `solution.gRecaptchaResponse` + `userAgent`
    v6.2 起还支持嫁接层（`mc_type != ""`）：hCaptcha / reCAPTCHA v2 在生态里都叫
    `gRecaptchaResponse`；cookie 类（DataDome/PerimeterX/Akamai/AWS/Clearance）放
    `cookies`；Arkose FunCaptcha 用 `token`。
    """
    if mc_type:
        return _solution_from_mc_result(mc_type, result)
    ua = str(result.get("user_agent") or "")
    token = str(result.get(RESULT_FIELD.get(want, "token")) or "")
    if want is Want.CLEARANCE:
        cookies = result.get("cookies")
        if not isinstance(cookies, dict) or not cookies.get("cf_clearance"):
            cookies = {"cf_clearance": token} if token else {}
        return {"cookies": dict(cookies), "userAgent": ua}
    if want is Want.RECAPTCHA_V3:
        return {"gRecaptchaResponse": token, "userAgent": ua}
    return {"token": token, "userAgent": ua}


#: 嫁接层各类型的产物字段 → YesCaptcha `solution` 的字段名。
#: 依据：hCaptcha / reCAPTCHA 在生态里统一叫 `gRecaptchaResponse`（实测 ohmycaptcha
#: 也是这么返回的），Arkose 用 `token`，cookie 类统一进 `cookies`。
_MC_TOKEN_AS_GRECAPTCHA = frozenset({"recaptcha_x", "hcaptcha"})
_MC_COOKIE_TYPES = frozenset({"cloudflare_x", "awswaf_x", "datadome", "perimeterx", "akamai"})


def _solution_from_mc_result(mc_type: str, result: dict) -> dict:
    ua = str(result.get("user_agent") or "")
    token = result.get("token")
    if isinstance(token, dict):        # aliyun 的 token 是 {certifyId, deviceToken, data}
        return {"token": token, "userAgent": ua}
    if mc_type in _MC_COOKIE_TYPES:
        cookies = result.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            # 有的求解器只在顶层给单个 cookie（cf_clearance / _px3 / datadome …）
            cookies = {}
            for key, value in result.items():
                if isinstance(value, str) and value and key in (
                        "cf_clearance", "_px3", "_abck", "datadome", "aws-waf-token"):
                    cookies[key] = value
        return {"cookies": dict(cookies), "userAgent": ua}
    if mc_type in _MC_TOKEN_AS_GRECAPTCHA:
        return {"gRecaptchaResponse": str(token or ""), "userAgent": ua}
    return {"token": str(token or ""), "userAgent": ua}


@dataclass
class CompatTask:
    task_id: str
    spec: CompatSpec
    status: str = "processing"          # processing | ready
    created_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    solution: dict | None = None
    error_code: str = ""
    error_description: str = ""

    def result_payload(self) -> dict:
        """`getTaskResult` 的成功响应体（不含 errorId/errorCode，由调用方拼）。"""
        if self.status != "ready":
            return {"status": "processing"}
        if self.error_code:
            return {
                "status": "ready", "errorId": 1, "errorCode": self.error_code,
                "errorDescription": self.error_description,
            }
        return {"status": "ready", "solution": self.solution or {}}


class TaskRegistry:
    """兼容任务的注册表：创建 / 查询 / 过期清理 / 上限保护。

    线程安全：`create` 由 HTTP handler 线程调用，`_run` 在任务线程里写结果，
    `result` 又由 HTTP handler 线程读 —— 全部走同一把锁。
    """

    def __init__(self, solver, *, ttl: float = COMPAT_TASK_TTL, max_pending: int = DEFAULT_MAX_PENDING) -> None:
        self._solver = solver
        # 下限给到 0.01s 而不是 1s：让"过期清理"这条逻辑能被单测验证（生产配置不会低于 1s）
        self._ttl = max(0.01, float(ttl))
        self._max_pending = max(1, int(max_pending))
        self._tasks: dict[str, CompatTask] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._created = 0

    # -- 对外 ----------------------------------------
    def create(self, task: object) -> CompatTask:
        """校验并启动一个后台求解任务。不合法直接抛 `CompatError`（不占资源）。"""
        spec = spec_from_task(task)          # 先校验：不合法连线程都不开
        with self._lock:
            if self._closed:
                raise CompatError("ERROR_INTERNAL", "服务正在关闭，不再接受新任务")
            self._purge_locked()
            pending = sum(1 for t in self._tasks.values() if t.status != "ready")
            if pending >= self._max_pending:
                raise CompatError(
                    ERR_NO_SLOT, f"待处理任务已达上限 {self._max_pending}；请降低并发或稍后重试"
                )
            task_id = secrets.token_hex(8)
            record = CompatTask(task_id=task_id, spec=spec)
            self._tasks[task_id] = record
            self._created += 1
        try:
            threading.Thread(
                target=self._run, args=(record,), name=f"compat-{task_id}", daemon=True
            ).start()
        except BaseException as e:  # noqa: BLE001 —— 线程起不来必须回滚登记
            # 不回滚的后果：字典里留下一条永远不会推进的 processing，它照样占 max_pending 配额
            with self._lock:
                self._tasks.pop(task_id, None)
                # 计数器只记**真正跑起来**的任务，否则 stats 会报出一个不存在的任务
                self._created -= 1
            raise CompatError(
                "ERROR_INTERNAL", f"无法启动任务线程（{type(e).__name__}: {str(e)[:80]}）"
            ) from e
        return record

    def result(self, task_id: str) -> CompatTask:
        """查任务。不存在/已过期 → `ERROR_TASK_ID_INVALID`。"""
        if not isinstance(task_id, str) or not task_id:
            raise CompatError(ERR_PROPERTY_EMPTY, "taskId 必须是非空字符串")
        with self._lock:
            self._purge_locked()
            record = self._tasks.get(task_id)
        if record is None:
            raise CompatError(
                ERR_TASK_ID, f"任务不存在或已过期（结果保留 {self._ttl:.0f} 秒）"
            )
        return record

    def stats(self) -> dict:
        with self._lock:
            self._purge_locked()
            pending = sum(1 for t in self._tasks.values() if t.status != "ready")
            return {
                "created_total": self._created,
                "pending": pending,
                "retained": len(self._tasks),
                "max_pending": self._max_pending,
                "ttl_sec": self._ttl,
            }

    def close(self) -> None:
        """标记关闭：不再接受新任务；已在跑的守护线程随进程退出（不 join，避免阻塞 shutdown）。"""
        with self._lock:
            self._closed = True

    # -- 内部 ----------------------------------------
    #: ready 记录的保留上限（pending 上限的倍数）。防止 600s 窗口内高频 create
    #: 把 _tasks 涨到几千条（每条都含 solution 里的凭证副本）。
    RETAIN_FACTOR = 4

    def _purge_locked(self) -> None:
        now = time.time()
        # 只清**已完成**且超时的：把还在跑的记录清掉，客户端会从 processing
        # 直接跳到 ERROR_TASK_ID_INVALID。
        # 保留期从**完成时刻**算（max(created_at, finished_at)），不是从创建时刻算：
        # 否则一个跑了很久的求解（真机 16–20s、超时上限 45s）刚出结果就可能被立刻清掉，
        # 调用方第一个 getTaskResult 就拿到 TASK_ID_INVALID —— 与文档承诺的
        # 「结果保留 N 秒」直接矛盾。
        for key in [k for k, t in self._tasks.items()
                    if t.status == "ready"
                    and now - max(t.created_at, t.finished_at) > self._ttl]:
            self._tasks.pop(key, None)
        # 保留量上限：超额时按创建时间淘汰最旧的已完成记录
        ready = sorted((t for t in self._tasks.values() if t.status == "ready"),
                       key=lambda t: t.created_at)
        excess = len(ready) - self._max_pending * self.RETAIN_FACTOR
        for record in ready[:max(0, excess)]:
            self._tasks.pop(record.task_id, None)

    def _run(self, record: CompatTask) -> None:
        """在任务线程里跑一次求解。**任何异常都必须落成结果**，否则任务永远 processing。

        兜的是 BaseException 且包住**整个函数体**：原来 try 只圈住 `solve()`，
        之后 `result.get(...)` / `solution_from_result(...)` 的任何意外都会让线程直接死掉、
        记录停在 processing 直到 TTL 过期 —— 客户端 600s 内只会一直拿到 processing（M1）。
        """
        try:
            self._run_inner(record)
        except BaseException as e:  # noqa: BLE001 —— 绝不允许「永远 processing」
            self._finish(
                record,
                error_code="ERROR_INTERNAL",
                error_description=f"{type(e).__name__}: {str(e)[:120]}",
            )

    def _run_inner(self, record: CompatTask) -> None:
        spec = record.spec
        if spec.mc_type:
            result = self._run_mc(spec, record)
            if result is None:      # 已在本方法内写成失败，直接返回
                return
        else:
            try:
                result = self._solver.solve(
                    spec.url,
                    timeout=COMPAT_SOLVE_TIMEOUT,
                    force=False,
                    want=spec.want_value,
                    sitekey=spec.sitekey,
                    action=spec.action,
                    cdata=spec.cdata,
                )
            except SolveError as e:
                code = COMPAT_ERROR_BY_CODE.get(e.code, "ERROR_INTERNAL")
                self._finish(record, error_code=code, error_description=e.message)
                return

        if not isinstance(result, dict):
            raise TypeError(f"solver.solve 返回了 {type(result).__name__}，不是 dict")
        if not result.get("solved"):
            self._finish(
                record,
                error_code=ERR_UNSOLVABLE,
                error_description=str(result.get("error") or result.get("error_code") or "求解失败"),
            )
            return
        self._finish(record, solution=solution_from_result(spec.want, result, spec.mc_type))

    def _run_mc(self, spec: CompatSpec, record: CompatTask) -> dict | None:
        """嫁接层求解（captchas/）。失败已写成结果时返回 None。

        为什么要 `asyncio.run`：`multicaptcha.solve_async` 是 async 的，而兼容任务是
        **每个任务一个线程**（见 `create`），在自己的线程里开新事件循环最省事，
        也不会与主 worker 线程的任何循环冲突。`loop_lock` 已覆盖跨循环安全。
        """
        import asyncio as _asyncio

        try:
            from . import multicaptcha as mc
        except ImportError:      # 直接以脚本运行
            import multicaptcha as mc  # type: ignore[no-redef]
        body = {
            "type": spec.mc_type,
            "url": spec.url,
            "sitekey": spec.sitekey,
            "action": spec.action,
            **spec.mc_extra,
        }
        # 出口粘性：与 /solve 旁路同一规则（cookie 类产物绑 IP）
        try:
            entry = self._solver._entry_for(_host_of(spec.url)) if spec.url else None
        except Exception:        # noqa: BLE001 —— 取不到出口不是失败，只是没有粘性
            entry = None
        if entry is not None and not body.get("proxy"):
            body["proxy"] = entry.raw_url
        try:
            result = _asyncio.run(mc.solve_async(body, timeout_s=COMPAT_SOLVE_TIMEOUT))
        except SolveError as e:
            code = COMPAT_ERROR_BY_CODE.get(e.code, "ERROR_INTERNAL")
            self._finish(record, error_code=code, error_description=e.message)
            return None
        if entry is not None and isinstance(result, dict):
            result.setdefault("egress_id", entry.egress_id)
        return result

    def _finish(self, record: CompatTask, *, solution: dict | None = None,
                error_code: str = "", error_description: str = "") -> None:
        with self._lock:
            if record.status == "ready":
                return          # 幂等：兜底路径不许覆盖已经得出的结论
            record.solution = solution
            record.error_code = error_code
            record.error_description = error_description
            record.finished_at = time.time()
            record.status = "ready"
