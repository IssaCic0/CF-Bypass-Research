# policy.md — 本仓库的执行门禁（写死数值，不许出现"适当/尽量"）

> 生效日期：2026-11-01 · 适用：CF-Bypass-Research 全部改动 · 与 `plans/下一步改进指南.md`(v4.0) 配套

## 1. 质量门禁（数值）

| 项 | 门禁 | 检查命令 |
|---|---|---|
| Python 全量测试 | **0 failed，0 error** | `python -m pytest tests/ -q` |
| Python 覆盖率（整体） | **≥ 80%**（`solver/` 整体） | `python -m pytest tests/ -q --cov=solver --cov-fail-under=80` |
| Python 覆盖率（**逐模块**） | 每个 `solver/` 模块 ≥ `docs/coverage-policy.json` 里登记的值；**未登记 = 直接红** | `node scripts/check-coverage.mjs --coverage-json=coverage.json` |
| Python 覆盖率（**拆分产物专项**） | `bootstrap.py` **≥ 85%** · `auth.py` **≥ 85%** · `probe.py` **≥ 85%**（三者都是 v4.4 从 `service.py` 拆出来的，拆分当年留下过空洞：bootstrap 38% / auth 73% / probe 72%） | 同上（三项已登记进 `docs/coverage-policy.json`，实测 2026-11-04：bootstrap 87.3% / auth 95.8% / probe 93.4%） |
| Go 测试 | **0 failed**，且 `-race` 通过 | `cd gocfsolve && go test -race ./...` |
| Go 静态检查 | `go vet` **无输出** | `cd gocfsolve && go vet ./...` |
| Python 语法 | `compileall` 退出码 **0** | `python -m compileall -q solver scripts tests` |
| 结论库一致性 | 退出码 **0** | `node scripts/validate-findings.mjs` |
| **detect 结论差分** | 退出码 **0**（签名表改动导致分类结论变化必须显式更新 golden） | `python -m pytest tests/test_detect_golden.py -q` |
| **接口规格一致性** | 退出码 **0**（`docs/openapi.json` 的 `path+method` 必须与 `solver/service.py` 实际路由**完全一致**；除 `/health` 外必须声明 Bearer） | `node scripts/validate-openapi.mjs` |
| 文档数字一致性 | 退出码 **0**（测试数/覆盖率/结论条数/版本号四处必须等于 `docs/facts.json`，且 facts.json 必须等于本次实测） | `node scripts/validate-docs.mjs --coverage-json=coverage.json` |
| 文档一致性 | `docs/00-索引.md` 的计数与 `findings.json` 实际条数**相等** | 见 `scripts/validate-findings.mjs` 输出 |

> **数字口径（2026-11-04 起）**：测试数/覆盖率/版本号只允许出现在 `docs/facts.json` 里，
> 文档正文写具体数字时必须与它一致，由 `validate-docs.mjs` 把关。历史轮次的数字
> （如 `workflow_status.md` 里标了 `v4.7 轮次` 的行）保持原值不追改 —— 那是当时的实测记录。

> **为什么加"逐模块覆盖率"（2026-11-04）**：整体 82% 曾与 `bootstrap.py` 的 38% 并存 ——
> 一个整体数字把盲区平均掉了。而那块盲区里当场就藏着真缺陷（`bootstrap.py` 引用了未导入的
> `SolveError`，让文档承诺的「缺依赖 → 友好提示 + exit 2」变成 `NameError` 裸 traceback）。
> 修完之后 `bootstrap.py` 从 38% → 87%，整体从 82% → 85%。
>
> **门槛口径**：`min` = 该模块本轮实测值的整数部分（floor）。加代码不加测试就会掉下 min，
> 这正是想要的信号。要降门槛必须在同一 commit 里写理由。
>
> **曾经的豁免已被撤销（v6.2）**：`solver/engines.py` 长期以"cloakbrowser 是专有二进制、
> 本机与 CI 都装不了"为由挂在 56%。这个理由**不成立** —— 工厂函数的分支逻辑与二进制无关，
> 注入假模块就能全路径覆盖（`tests/test_engines_factories.py`，实测 100%）。门禁已提到 **95**。
> 教训：**用一个"环境限制"解释覆盖率缺口之前，先确认缺口真的来自环境**，
> 而不是来自"没写测试"。真实二进制的行为属于**集成验证**，不在模块覆盖率口径内
> （见 `docs/coverage-policy.json` 的 why 字段）。

## 2. 付费 / 外部资源红线

- **付费 API 真实调用预算 = 0**：本仓库不涉及付费 API；若将来引入，默认 0 次，须用户书面批准并给额度。
- **真实目标站过盾请求**：受《下一步改进指南 v4.7→v5.0》§15 预算约束。
  **额度台账**：原 60 次 / 已用 ≈46（据 `workflow_status.md` R5）→ 2026-11-04 用户**追加 20 次**（累计上限 **80**）。
  任何一批实验前必须报"本批用几次、打哪些站"；**累计失败 ≥5 次立即全停**，不许"再试一次就好"。
  **2026-11-04 R-E2E-1 台账**：用 **4 次**（udemy 求解 1 + Python 回放 1 + Go 回放 1 + glassdoor 尝试 1），失败 **1 次**（glassdoor 求解超时，按纪律未重试）。累计 ≈**50 / 80**。
  **2026-11-04 R-E2E-2 台账**：判据修正对照用 **6 次**（fresh solve 1 + 带/不带头 2 + 前缀探测 1 + Go 回放 1 + 时序抖动 1）。累计 ≈**56 / 80**。
  **2026-11-04 R-E2E-3 台账**：glassdoor.co.uk 端到端 **2 次，全部成功**（求解 16.5s/12 cookie；回放 200/735,957B/真实标题/前缀 0 marker）。累计 ≈**58 / 80**，累计失败仍 **1/5**。
  **2026-11-04 用户明确授权**：真机 E2E 验证可以做（含求解 + 回放），发版也可以做。
  授权范围仍受上面台账约束（累计上限 80）；**"授权可以花"不等于"可以乱花"**——
  每批照样先报计划、失败照样即停、同站照样串行 ≥30s。
- **本机出口纪律**：同一出口连续打同一站有被提分风险（`workflow_status.md` R9b 有实证），
  换出口必须换 `--egress-id`，否则会复用别处 IP 的凭证。
- **CI 永远离线**：禁止在 runner 上 `camoufox fetch`、禁止任何真实联网请求、禁止 §15 实验。
  > 2026-11-04 曾为"容器内跑 e2e"开过一个联网例外，实测后**已撤回**：镜像的 UBO 插件预热
  > 依赖 `api.github.com` 列 release（实测 403/404 抖动）且下载极慢（容器内首启 40s 才 7%），
  > 于是 e2e 的 180s 就绪预算必然超时。结论：这个 job 的成败取决于上游插件源，而不是我们的代码质量。
  > 现在 CI 恢复为**全程离线**；e2e 留在本地（`run.ps1 e2e`，实测 30/30、20.3s）。
  > 详见 `docs/findings.json` F-043 与 `.github/workflows/ci.yml` 文件末尾的说明。
- 任何实验脚本必须支持 `--dry-run`。

## 3. 任务分级与审查

| 级别 | 触发 | 要求 |
|---|---|---|
| L1 低风险 | 单文件、可逆、有测试 | 直接实施 + 定向验证 |
| L2 中风险 | 多文件 / 有下游影响 | 拆成可回滚变更单元 + 扩回归验证 |
| L3 高风险 | 公共契约 / 持久化 / 安全边界 | 先影响分析 + 回滚方案，未授权不实施 |

**审查门禁**：任何节点标记 `Done` 前，必须有 **Approve** 记录（见 `workflow_status.md` 的"审查记录"）。
Approve 的含义是"严格审查后未发现阻塞性问题"，不是"代码完美"。

## 4. 证据门禁

- 没有**真实执行过的命令 + 退出码 + 输出摘要**，不得声明"通过/完成/闭环"。
- 证据落 `evidence/<task_id>.json`，字段见 `evidence/README.md`。
- 禁止伪造退出码、猜测测试输出、把静态阅读写成运行验证、把 mock 结果写成真实外部服务结果。
- 覆盖率无法计算时填 `null` 并在 `limitations` 说明，**不许填估计值**。
- 证据内禁止出现 token / cookie 值 / 密码；`cf_clearance` 只写长度。

## 5. 版本控制

- 允许：本地 commit、推送 `main`（项目默认分支为 `master`，本版发版合并到 `master`）。
- **禁止**：`git push --force` / `-f` / 任何等效强制推送；禁止删除他人分支。
- **tag 移动（唯一一次，已披露）**：2026-11-04 `v5.0.1` 创建后约 30 分钟从 `d9d0b96` 移到 `c628f1b`。
  原因：`d9d0b96` 上 CI 的「文档数字一致性」一步为红 —— 新加的门禁脚本没考虑**覆盖率是平台相关测量值**
  （同一提交 Windows 86.x% / Linux 87.x%），产品代码本身在该提交上全绿。
  修复在 `9dbb3ab`（覆盖率 ±2 容差），`c628f1b` 另删了一个误提交的临时文件。
  **只移动了 tag 与 Release 标记，未改动任何分支历史、未 force push 任何分支。**
  今后同类情况**默认按"发补丁版（vX.Y.Z+1）"处理**；只有在"发布物内容本身没变、纯属发布标记错误"
  且已明确披露时，才允许移动 tag。
- 提交粒度：一个任务卡 = 一个可独立回滚的 commit。
- **发版（2026-11-04 用户授权）**：发版 = ①五处版本号同步（`CHANGELOG.md` / `Dockerfile` /
  `docker-compose.yml` / `docs/facts.json` / `docs/openapi.json`）②全部门禁复跑并落证据
  ③commit → `tag vX.Y.Z` → push → GitHub Release。**五处有任一处不同步即视为发版失败**（`validate-docs.mjs` 会红）。
- 提交前：`git status` 与 `git diff` 必须只含本任务范围改动，无密钥、无临时文件、无调试残留。
- 提交信息遵循 Conventional Commits（`feat|fix|refactor|docs|test|chore|perf|ci`）。

## 6. 部署与回滚

- 本项目**无生产部署目标**（无服务器清单、无 K8s、无云凭证）。部署动作 = 本地多进程启动 + 健康检查 + 冒烟。
- 任何"热更新"动作必须：先记当前版本/SHA → 再起新进程 → 健康检查通过 → 才停旧进程 → 失败立即回滚（旧进程不停）。
- 数据类不可逆操作（删缓存目录、删研究产物）必须先 `git` 有历史或先备份。

## 7. 停止条件

1. 同一问题连续 **2 次**修复无实质进展 → 停，报告阻塞。
2. 需要用户凭证/生产权限/不可逆破坏 → 停。
3. 真实实验累计失败 ≥5 → 停。
4. 审查 **3 轮**仍未 Approve → 停，记录未解决问题。
