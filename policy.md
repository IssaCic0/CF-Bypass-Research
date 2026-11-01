# policy.md — 本仓库的执行门禁（写死数值，不许出现"适当/尽量"）

> 生效日期：2026-11-01 · 适用：CF-Bypass-Research 全部改动 · 与 `plans/下一步改进指南.md`(v4.0) 配套

## 1. 质量门禁（数值）

| 项 | 门禁 | 检查命令 |
|---|---|---|
| Python 全量测试 | **0 failed，0 error** | `python -m pytest tests/ -q` |
| Python 覆盖率 | **≥ 80%**（`solver/` 整体）；`solver/service.py` **≥ 70%** | `python -m pytest tests/ -q --cov=solver --cov-fail-under=80` |
| Go 测试 | **0 failed**，且 `-race` 通过 | `cd gocfsolve && go test -race ./...` |
| Go 静态检查 | `go vet` **无输出** | `cd gocfsolve && go vet ./...` |
| Python 语法 | `compileall` 退出码 **0** | `python -m compileall -q solver scripts tests` |
| 结论库一致性 | 退出码 **0** | `node scripts/validate-findings.mjs` |
| 文档一致性 | `docs/00-索引.md` 的计数与 `findings.json` 实际条数**相等** | 见 `scripts/validate-findings.mjs` 输出 |

## 2. 付费 / 外部资源红线

- **付费 API 真实调用预算 = 0**：本仓库不涉及付费 API；若将来引入，默认 0 次，须用户书面批准并给额度。
- **真实目标站过盾请求**：受 `plans/下一步改进指南.md` §15 预算约束（本版 60 次），**累计失败 ≥5 次立即全停**。
- **CI 永远离线**：禁止 `camoufox fetch`、禁止任何真实联网请求、禁止 §15 实验。
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
- 提交粒度：一个任务卡 = 一个可独立回滚的 commit。
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
