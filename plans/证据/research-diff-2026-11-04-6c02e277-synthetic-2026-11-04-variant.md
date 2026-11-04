# 研究快照差分报告：2026-11-04-6c02e277 → synthetic-2026-11-04-variant

生成时间(UTC): 2026-11-04T07:37:15Z

## 1. 元信息

| 项目 | A | B |
| --- | --- | --- |
| 抓取时间 | 2026-11-04T07:24:51Z | 2026-11-04T07:24:51Z |
| 目标 eTLD+1 | unknown | unknown |
| 脚本字节数 | 528691 | 539410 |
| sha256 前 8 位 | 6c02e277 | c496b609 |
| 脚本存在 | 是 | 是 |

## 2. AST 形态变更

- 形态签名数：A=11，B=11，变更=0。
- 无形态频次变更。

## 3. 字段名集合变更

- 共同字段名：484 个；新增 1；消失 1。
- 新增字段名：
  - `Contact%20the%20website%E2%80%99s%20administrator%20or%20support%20team%20for%2…`
- 消失字段名：
  - `Contact%20the%20website%E2%80%99s%20administrator%20or%20support%20team%20for%2…`

## 4. 断言矩阵变更

| 断言 | A | B | 转移 |
| --- | --- | --- | --- |
| dispatcher | fail | fail | 无变化 |
| collection_fn | fail | fail | 无变化 |
| probe_state | fail | fail | 无变化 |
| counter | pass | fail | pass→fail |
| token | fail | fail | 无变化 |
| input | pass | pass | 无变化 |

## 5. 建议动作

- `dispatcher`：fail → fail；建议动作：PATCHES.json 无对应补丁登记，需新增补丁并在 PATCHES.json 里登记断言（否则补丁净收益不可见）。
- `collection_fn`：fail → fail；建议动作：PATCHES.json 无对应补丁登记，需新增补丁并在 PATCHES.json 里登记断言（否则补丁净收益不可见）。
- `probe_state`：fail → fail；建议动作：PATCHES.json 无对应补丁登记，需新增补丁并在 PATCHES.json 里登记断言（否则补丁净收益不可见）。
- `counter`：pass → fail；建议动作：检查/回归补丁 `newcf-computed-key-20261103`（assertion_ids: R-computed-key-static-eval, R-stage2-counter-candidates）。
- `token`：fail → fail；建议动作：PATCHES.json 无对应补丁登记，需新增补丁并在 PATCHES.json 里登记断言（否则补丁净收益不可见）。
