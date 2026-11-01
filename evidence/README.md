# evidence/ — 验收证据落地处

一个任务卡一个文件：`evidence/<task_id>.json`。

## 必填字段

```json
{
  "task_id": "T-10",
  "status": "done | partial | blocked",
  "timestamp": "2026-11-01T22:30:00+08:00",
  "baseline_commit": "<sha>",
  "commands_executed": [
    {"cmd": "cd gocfsolve && go test -race ./...", "exit_code": 0, "raw_output_tail": "ok  ..."}
  ],
  "files_changed": ["gocfsolve/cfsolve.go"],
  "tests": [
    {"name": "TestDoWith_403_TriggersRevalidate", "type": "unit", "status": "pass", "evidence": "cfsolve_test.go:NNN"}
  ],
  "coverage_summary": {"python_total_pct": 69.0, "method": "pytest --cov=solver"},
  "seams_touched": ["Go 客户端 → 求解服务 HTTP 契约"],
  "mock_scenarios_covered": ["success", "403", "challenge-page", "timeout", "ctx-cancel"],
  "browser_validation": null,
  "limitations": ["未在真实站点验证（预算约束）"]
}
```

## 铁律

1. 没跑过的命令不许写进 `commands_executed`。
2. `exit_code` 必须是真实退出码。
3. `raw_output_tail` 是真实输出尾巴，不是预期结果。
4. 凭证脱敏：`cf_clearance` / cookie / token 只写长度或指纹。
5. 没有证据文件的验收项，一律视为**未验收**。
