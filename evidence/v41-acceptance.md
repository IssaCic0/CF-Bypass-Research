# v4.1 四类验收记录（真实执行）

> 生成时间：2026-11-01 23:2x · 代码基线：`e7bc720`（含本文件提交前的最后一次改动）
> 机器：Windows 10 / AMD64 · Python 3.14.3 · Go 1.26.1 · Camoufox 0.5.8（二进制 156.0.1-beta.36）
>
> **本项目没有 UI**（只有 JSON API 与 Prometheus 文本），所以第 1 类验收按用户拍板
> 用 **CLI/JSON 输出实录** 替代截图：`evidence/cli/v41-cli-acceptance.txt`。

---

## 1. UI 截图验收 → 替代物：CLI/JSON 实录

**结论**：本项目**不存在任何界面**（仓库里 0 个模板、0 个静态资源；服务端只 `send_header("Content-Type", "application/json")` 与 `text/plain` metrics）。
所以"按钮能不能按成功、组件放这儿人性化吗"这类判断**没有对象**。经用户拍板，改用 CLI/JSON 实录，并对"每个交互点的反馈、逻辑、容错"逐条判定：

| 交互点（相当于"按钮"） | 点了会怎样（实测） | 失败会怎样 | 判定 |
|---|---|---|---|
| `GET /health` | `state=ready` + `browser_alive/worker_alive/cache_backend/ua_fingerprint/backends` 全字段 | 未就绪时 `503` + 同样的 JSON（不会给空壳） | ✅ 反馈清晰、可排障 |
| `GET /detect?url=` | `protected/providers/challenge_type/recommendation.route` + `final_url` | 内网 URL → `invalid-request` + 可执行 `hint` | ✅ 有结论也有建议 |
| `POST /solve route=auto`（无盾站） | `no_challenge:true` + `routed_by.action=skip` + 中文 hint，**0 次求解** | 探测失败 → 退回 `fixed`（不拒绝服务） | ✅ 不做无意义等待 |
| `POST /solve route=auto`（不支持厂商） | `ok:false, error_code=unsolved, stage=detect`，hint 点名厂商 | 不进入求解，不白等超时 | ✅ 诚实拒绝 |
| `POST /solve`（正常） | `ok/solved/cf_clearance/user_agent/cookie_list/expires_at/backend/elapsed/cached` | 求解失败 = **HTTP 200 + ok:false** + `error_code`/`stage`/`hint` | ✅ 契约稳定 |
| `GET /metrics` | 全局计数 + 直方图 + `backend_total` + `host_solves_total`(top-20) | 无数据时输出 0 值而非缺行 | ✅ 与真实调用一致 |
| `.\run.ps1 <cmd>` | 10 个子命令，任一失败返回非 0 | PowerShell 5.1 下用 `$LASTEXITCODE`（避开 `$?` 误判） | ✅ 可被 CI 直接调用 |
| `scripts/doctor.py` | 逐项 ✓/⚠/✗ + 中文 hint，退出码 0/1/2 | 缺依赖时给安装命令，不抛栈 | ✅ 自检不炸 |

**"组件放这儿人性化吗"** 的判定：调用方视角走一遍（new-api 接入者 / Go 客户端 / 运维）：

- 接入者：只需 `POST /solve` 一个端点；`route=auto` 是**可选**的，不传就与 v3 一致 → 不用重写对接代码 ✅
- Go 客户端：`Do()` 一行完成"取凭证→回放"，403 自愈内置；`pool` 提供多节点与探活 ✅
- 运维：`/health` 三态 + `doctor` 一条命令 + `run.ps1 verify` 一条命令 ✅
- 反方视角：**没有历史查询/图表**，要看趋势只能上 Prometheus；`/solve` 是同步阻塞的（一次最多 120s），
  没有任务队列/异步协议 → 大规模异步接入需要调用方自己做并发控制。**这是设计取舍，不是缺陷**（稳态 0.08 QPS）。

---

## 2. 真实调用验收（真目标站，消耗预算）

预算：`plans/下一步改进指南.md` §15 的 60 次；本轮实际使用 **≈46 次**（A/B 8 次作废 + A/B 35 次 + E2E 3 次）。

### 2.1 同站双臂 persona A/B（可归因的对照）

证据文件：`plans/证据/v41-persona-ab.json`

| 站点 | 固定 persona（`--browser-os windows`） | 随机 persona | 差异 |
|---|---|---|---|
| steamdb.info | **3/3 求解**，回放 3/3 200，平均 **6.03s** | 2/3（1 次 60s 超时），回放 2/3，平均 9.04s | 快 33%，稳 |
| glassdoor.co.uk | **3/3**，回放 3/3，平均 **6.64s** | 3/3，回放 3/3，平均 13.41s | **快 2.02×** |
| udemy.com | 3/3 求解，回放 2/3（1 次 403），平均 **5.31s** | 3/3，回放 3/3，平均 12.88s | 更快，回放偶发 403 |
| **合计** | 9/9 求解、8/9 回放、均 6.0s | 8/9 求解、8/9 回放、均 11.8s | — |

**结论（口径必须带上）**：这是**同站**双臂（每个站被两个臂各打 3 次，`force=true` 每次真求解），
所以可以说"固定 persona 在本次 3 站上更快（约 2×）且不掉成功率"；
样本量 n=3/站，**不能**外推成"固定 persona 值多少个百分点"。
**生产建议**：`--browser-os` 显式固定（默认随机会让 UA/时区与宿主/出口地理自相矛盾）。

失败明细（2 次，未触发"失败≥5 即停"）：
- `steamdb.info` random #2：60s 预算内未取得 clearance（随机 persona 抽到了不自洽的组合）
- `udemy.com` pinned #2：求解成功但回放 403（凭证与回放侧不匹配，Go 客户端会自愈重解）

### 2.2 `route=auto` 真实差异（同一 URL 对照）

| 场景 | 耗时 | 求解次数 |
|---|---|---|
| `route=auto` 打无盾站（example.com） | **0.55s**（首次）/ **0.0s**（T-34 缓存命中后） | **0** |
| 不传 route（v3 行为）打同一 URL | **20s**（超时未取得 clearance） | 1 |

---

## 3. 真实 E2E 验收（Go 客户端 → 服务 → 真实 CF 站 → 回放）

命令与**真实输出**：

```
$ cd gocfsolve && go run ./examples/endtoend -solver http://127.0.0.1:8941 \
    -target https://glassdoor.co.uk/ -token dummy

✅ 求解服务在线：state=ready browser_alive=true queue_depth=0
✅ 取得 cf_clearance（462 字节, UA=Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:156.0…,
   求解 0.00s, 总耗时 0.20s, cached=true, cookies=5）
📄 回放结果：status=200 长度=400000 挑战页=false 耗时=1.93s
🔁 二次调用：cached=true（命中缓存即不再启动浏览器）

✅ 端到端通过：求解 → 回放 → 200 真实页面
```

**这条的价值**：不是"接口通"，而是**拿到了 400KB 真实页面且 `挑战页=false`**
（脚本会检查正文里有没有 CF 挑战特征）。这是整条链路的最终判定。

服务端冒烟（独立于上面）：

```
$ .\run.ps1 smoke -Port 8911 -NoAuth
OK  /health ready（browser=camoufox, cache=sqlite:...）
OK  /detect example.com → protected=False route=direct
OK  SSRF 守卫拦住了内网 URL
```

---

## 4. 真实代码复审验收

| 项 | 状态 |
|---|---|
| 独立只读子代理对抗审查（证伪 v4.0 的三条"假 Done" + 审查提交信息与代码是否相符） | 见 §5 的独立结论 |
| 主控自查（有罪推定 + 斜率检测） | 本文件 §1 的"反方视角" + `workflow_status.md` §4.5 修正记录 |

> 说明：本轮最大的复审收获是**推翻了自己上一轮的"已修复"结论**。
> 三条 P0/P1 被 commit message 声称修好，实测全部不成立；
> 且修复过程中同一个坑踩了第二次（把治理加到 router、却忘了主路径 camoufox 的 backend/host 维度），
> 由 `/metrics` 抓出来后才补上。**这两件事都写进了 `workflow_status.md` 的修正记录。**

---

## 5. 修复后的治理证据（关键三条）

| 断言 | 修复前（实测） | 修复后（实测） |
|---|---|---|
| 内网 URL 是否到达后端 | 假 cfsolve 二进制**收到** `--page http://127.0.0.1:8930/health` | 返回 `invalid-request`，**argv.txt 未生成**（后端 0 次调用） |
| 纯协议结果是否进缓存 | 第 2 次 `cached:false`，`/metrics` 全 0 | 第 2 次 `cached:true`，`solves=1 cache_hits=1 cached_domains=1` |
| 指标是否有维度 | 无 `backend`/`host` 维度；`host_stats` 是死代码 | `cfsolve_backend_total{backend="cfsolve",result="ok"} 1`、`cfsolve_host_solves_total{host="glassdoor.co.uk"} 1` |

对应的自动化用例：`tests/test_governance.py`（12 例）+ `tests/test_acceptance_v41.py`（10 例，含 4 种后端组合的参数化）。

---

## 6. 本轮预算与环境清理

- 真实请求 ≈46/60 次（剩余 ~14 次留作下一步验证 T-32 的真实 CF 站路由收益）
- 临时服务与缓存目录已全部停止/删除（`%TEMP%/ab-*`、`v41-metrics`）
- 仓库无残留：`git status` 只剩 6 个本机工具配置（未跟踪、未提交）
