# 容器化实测记录（v4.4：从未验证 → 真跑通，并抓出 2 个容器专属缺陷）

> 2026-11-02 02:4x · Docker 29.9.2 (linux) / Compose v5.6.1 · 镜像 `cf-bypass-solver:4.4.0`（1.61 GB）
> 本机 Docker Desktop 起初未运行 → 启动后完成构建与运行验证。

---

## 1. 结论先行

| 项 | 结果 |
|---|---|
| `docker compose config -q` | ✅ 通过 |
| `docker build` | ✅ 成功（1.61 GB，fetch 层 ~105 s） |
| 容器**就绪耗时** | ✅ **5 s**（修复前 **110–120 s**，见缺陷 N-11） |
| `/health` | ✅ `state=ready, browser_alive=true, cache_backend=sqlite:/data/cache` |
| `/detect`（真出站） | ✅ `status=200, protected=false` |
| SSRF 守卫（容器内打自己） | ✅ `invalid-request / validate` |
| 无 token 调 `/solve` | ✅ `401` |
| `route=auto`（无盾站） | ✅ `no_challenge=true, action=skip`（不启动求解） |
| `POST /shutdown` | ✅ `202`，`peer=172.17.0.1`，随后 **`exit=0`**、日志 `优雅退出：在途请求已全部完成` |

---

## 2. 只有真跑容器才会暴露的两个缺陷（都已修）

### N-11｜build 期 fetch 落到 root 的 HOME ⇒ 每个新容器首次启动重下载浏览器

- **现象**：容器首次启动花了 **110–120 s**，日志里出现 `Downloading addon (UBO)` / `Extracting Camoufox`。
- **根因**：Dockerfile 在 `USER solver` **之前**执行 `python -m camoufox fetch`，
  浏览器落到 `/root/.cache/camoufox`；而运行期用户是 `solver`（`HOME=/home/solver`），缓存是空的。
- **修法**：`ENV HOME=/home/solver` → 以 root 完成 fetch → `chown -R solver:solver /home/solver/.cache` → `USER solver`。
- **为什么不是直接 `USER solver` 再 fetch**：实测那样会报
  `A model this user cannot read was installed by another account or elevation level`（fpgen 模型按目录属主判权限），构建直接失败。
- **效果**：就绪 **110–120 s → 5 s**。

### N-12｜`/shutdown` 的"仅回环"判断在容器+端口映射下不成立

- **现象**：容器内调 `POST /shutdown` → `401 只允许从回环地址触发优雅退出`，滚动重启直接失败。
- **根因**：端口映射时客户端地址是 **docker 网关**（实测 `172.17.0.1`），不是 `127.0.0.1`。
- **修法**：抽出可测的 `Handler.shutdown_allowed(peer)`；默认仍只放行回环，
  新增 `--allow-remote-shutdown`（**Bearer token 仍是第一道门**）；
  `docker-compose.yml` 的 `command` 显式带上该 flag；错误信息里直接给出这条指引。
- **回归保护**：`tests/test_governance.py::test_shutdown_allowed_matrix`（回环 4 种写法放行、网关/私网/公网拒绝、开 flag 后放行）。

### 附带：我自己踩的坑（不是镜像问题，但要写下来）

从 **Git-Bash** 传 `-v ...:/data` 和 `--cache-dir /data/cache` 时，MSYS 会把 `/data` 转成
Windows 路径（日志里出现 `cache=sqlite:/app/C:/Program Files/Git/data/cache`）。
→ 传参要 `export MSYS_NO_PATHCONV=1` 或用 PowerShell。这个坑会让服务"静默在 CWD 下建目录"，
看起来像镜像问题，其实是调用方式问题。

---

## 3. 镜像设计（每条对应一个真实失败模式）

| 设计 | 为什么 |
|---|---|
| `camoufox fetch` 在 build 阶段（且落到运行用户 HOME） | 运行期 fetch 需要外网 + 几百 MB，容器会在第一次求解时超时失败 |
| 非 root 运行（uid 10001） | 会拉起浏览器进程，root 身份放大一切提权面 |
| `tini` 做 init | 浏览器子进程容易留僵尸；`tini` 负责转发信号与回收 |
| `HEALTHCHECK` 打 `/health`（`start-period=120s`） | 冷启动要等浏览器，`start-period` 太短会被反复重启 |
| 数据卷 `/data`（token + cache） | token 与缓存必须跨容器存活，否则每次重启都重新求解 |

---

## 4. 未验证/边界

- 镜像只在 **本机 Docker Desktop（linux 容器）** 上跑过；未在 K8s / 其他发行版验证。
- `docker-compose.yml` 只做过 `config -q` 语法校验 + 单容器手工运行，**没有真起 3 个 worker**（内存 3×~0.9GB，本机另跑着东西）。
- 镜像 1.61 GB（含浏览器二进制），未做多阶段瘦身 —— 这是一个明确的优化项，不是缺陷。
