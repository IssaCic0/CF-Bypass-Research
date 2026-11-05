# captchas/ —— 万盾求解器副本（自有，不追上游）

11 厂商验证码/挑战求解器，源自 `captcha-solver`，**已合并进本仓库作为自有代码**。

## 为什么是副本而不是依赖

主项目是「CF 专项」，上游是「多厂商通解」。两者的演进节奏不同：上游追厂商改版，
本项目追 CF 挑战与回放工程。把上游当依赖意味着每一次上游改动都会传进主链路；
做成副本则相反——**我们决定何时 cherry-pick**，主链路永远不被上游节奏绑架。

## 与原版（captcha-solver）的差异

- 二进制 ONNX 模型（`aliyun/best.onnx`、`recaptcha/models/recaptcha_cls_s.onnx`）
  **不入库**（见 `.gitignore`）；缺失时 aliyun 自动回落 cv2 gap 检测，
  reCAPTCHA 的 `classifier=auto` 回落 mistral。
- import 统一为 `captchas.common.*`（原版是 `common.*`，依赖 cwd）。
- 原 `server.py` 的 FastAPI 外壳不搬：入口统一走主服务的 `POST /solve`
  （`want=<扩展类型>`），分发在 `solver/multicaptcha.py`。
- 仍然**只解挑战、不建号**：账号创建与表单侦察是调用方的事。

## 目录

| 目录 | 厂商 | 产物 |
|---|---|---|
| `turnstile/` | Cloudflare Turnstile | 一次性 token |
| `recaptcha/` | Google reCAPTCHA v2/v3/invisible | 一次性 token（可带 score） |
| `hcaptcha/` | hCaptcha | 一次性 token |
| `cloudflare/` | Cloudflare | `cf_clearance` cookie |
| `awswaf/` | AWS WAF | `aws-waf-token` |
| `botguard/` | Google OAuth BotGuard | `bgRequest` token |
| `datadome/` | DataDome | `datadome` cookie |
| `perimeterx/` | PerimeterX / HUMAN | `_px3` cookie（CDP press-hold） |
| `akamai/` | Akamai Bot Manager | `_abck` cookie（`bmak` 遥测） |
| `aliyun/` | 阿里云 Captcha 2.0 | `{certifyId, deviceToken, data}`（滑块，**强制子进程**） |
| `arkose/` | Arkose FunCaptcha | `fc_token`（ONNX 视觉拼图） |
| `common/` | — | 选择器解析 / pre_actions / post_fetch / Mistral key pool |

## 依赖

`pip install cloakbrowser`（必需）；`aliyun`/`arkose` 另需 `onnxruntime` +
`opencv-python-headless`。缺失时请求报 `not-configured`，不影响主链路。

## 上游同步策略

不做自动同步。需要某个上游修复时，手动 diff 对应子目录后 cherry-pick，
并在 `CHANGELOG.md` 记下来源与日期。改动本目录视为改本仓库代码（同样过测试与门禁）。
