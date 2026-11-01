# v4.7.0 容器与热更新真机验收（2026-11-02）

> 环境：Windows 10 Pro · Docker 29.9.2（linux/amd64, Docker Desktop）· Python 3.14.3（宿主机）· Go 1.26.1
> 镜像：`cf-bypass-solver:4.7.0`（当前 HEAD 构建，`--build-arg SLIM_FONTS=1`）
> 目的：把 `workflow_status.md` §7 里两条一直挂着的"未验证/无法执行"（R3 镜像未构建、R4 无部署环境）
> 换成**真跑过的数据**，并把"热更新"从散文变成可复现的断言。

---

## 1. 镜像构建

```
$ docker build --build-arg SLIM_FONTS=1 -t cf-bypass-solver:4.7.0 .
... exporting to image / unpacking ...  DONE 43.8s
[exited with code 0]

$ docker images cf-bypass-solver
cf-bypass-solver:4.7.0   3.54GB
```

v4.4 时代记录是 4.63GB → slim 3.27GB；本次 3.54GB 的差额是**刻意加回来的 18MB 插件**（见 §2）+ 层对齐差异。

## 2. 本轮修掉的容器专属缺陷（N-13 插件运行期下载）

`Dockerfile` 开头写着"camoufox 二进制在 build 阶段 fetch，不放到运行期（运行期 fetch 需要外网，
容器会在第一次求解时超时失败）"——**但这条承诺对插件不成立**：

```
# 修复前的镜像里：
$ docker run --rm --entrypoint sh cf-bypass-solver:4.7.0 -c 'ls /home/solver/.cache/camoufox/addons'
（空目录）

# 于是每个新容器首次启动都要联网下载并解压 ~18MB 的 uBlock Origin：
Downloading addon (UBO): 0% → 100%
Extracting addon (UBO): 0% → 100%
```

后果：离线/受限网络环境里浏览器永远起不来；有网环境里每个新容器多等几秒。
同时发现 slim 分支里有一句反向操作 `rm -rf addons/*/` —— 为了省 2MB 把"离线可用"删掉，收益为负。

**修法**（`Dockerfile`）：build 阶段真的把浏览器拉起一次（headless），把插件预热进镜像；删掉那句 `rm -rf`。

```
$ docker run --rm --entrypoint sh cf-bypass-solver:4.7.0 -c 'ls /home/solver/.cache/camoufox/addons; du -sh ...'
UBO
18M    /home/solver/.cache/camoufox/addons
```

**验证（修复后）**：新起 3 个容器，日志里 `Downloading addon` 出现次数 **0/0/0**。

## 3. 三 worker（compose）启动与就绪

```
$ docker compose up -d
 Container cf-solver-1/2/3  Started
3/3 ready after 3s
```

| 项 | 实测 |
|---|---|
| 3 个 worker 全部 `state=ready` | **3s**（修复插件预热前，首次启动要等下载；v4.5 记录 10s） |
| 日志里运行期插件下载 | **0 次**（三个容器都是） |
| 内存（`docker stats`） | 690.9 MiB + 1.046 GiB + 643.2 MiB ≈ **2.33 GiB**（其中 cf-solver-2 刚重启、正在预热浏览器，CPU 87%） |
| token 一致性（三个容器共用卷里的 `/data/.env`） | 三个容器 `md5sum` 完全相同 |

## 4. 端点验收（容器内真跑，`127.0.0.1:8878`）

| # | 断言 | 实测 |
|---|---|---|
| 1 | 不带 token 访问 `/metrics` | **401**（结构化 JSON，`error_code=unauthorized`） |
| 2 | 带 token 访问 `/metrics` | **200**，`text/plain`，含 `cfsolve_*` 指标 |
| 3 | `/detect?url=https://example.com/` | `protected=False`，`recommendation.route=direct` |
| 4 | `POST /solve {route:auto}` 同一 URL | `no_challenge=True`，`elapsed=0.0`（**探测结果命中 T-34 的按域缓存**） |
| 5 | 畸形 JSON body | **400**，`error_code=invalid-request`，且容器日志 `Traceback` 计数 **0** |
| 6 | SSRF：`POST /solve {url:169.254.169.254}` | **400**，`目标解析到受限地址` |

第 5 项是 v4.7 修的 N-14（早退路径 `url` 未绑定 → `UnboundLocalError` 打穿 `_log`）在**容器里**的复核：
修复前这条请求不会出现在审计/事件流里、且会打一整段 traceback；现在能看到

```
{"event": "solve", "req_id": "1cc4af52", "host": "", "ok": false,
 "error_code": "invalid-request", "stage": "validate", "route": null, "ms": 0}
```

## 5. 滚动重启（容器语义的"热更新"）

在 `docker compose up -d --force-recreate solver-2` **期间**持续打 `solver-1` 的 `/metrics`：

```
150 200
```

即：**150/150 全部 200，零失败**。`solver-2` 重建后 **1s** 恢复 `ready`。
（逐个摘是 compose 多容器部署唯一的正确姿势；同时重建 = 全站空窗。）

## 6. 优雅退出（`/shutdown`）

独立容器（`--allow-remote-shutdown`，端口映射下客户端地址是 docker 网关）：

```
$ curl -X POST .../shutdown   →  {"ok":true,"state":"shutting-down","peer":"172.17.0.1"}  HTTP=202
$ docker wait cf-graceful     →  0
```

容器日志尾部：`[cfsolve] 优雅退出：在途请求已全部完成` / `[cfsolve] 已退出`。
即 **202 → 排空 → exit 0**，不是被 SIGKILL 砍掉。

### 6.1 与 `restart: unless-stopped` 的配合（滚动重启的真实机制）

文档说"滚动重启用 `/shutdown`，不要 `taskkill /F`"。这一步在本轮也真跑过：

```
before:  {"ok": true, "state": "ready", ...}                     # cf-solver-3
$ curl -X POST .../shutdown  → HTTP=202  {"state":"shutting-down","peer":"172.18.0.1"}
after 6s: {"ok": true, "state": "ready", ...}                     # 同一个端口，容器已被 compose 拉回来
$ docker compose ps
cf-solver-1 Up 8 minutes (healthy)
cf-solver-2 Up 8 minutes (healthy)
cf-solver-3 Up 4 seconds (health: starting)
```

即：**容器自己 exit 0 → compose 按 `restart: unless-stopped` 立刻重建 → 4s 内 ready**。
这条路成立，"滚动重启不掉请求"才成立；否则只能 `kill -9`，在途请求直接吃连接重置。

## 7. 本机多进程热更新（不依赖 Docker，可重复）

`scripts/hot_update_drill.py` → **17/17 通过**，Json 落盘 `evidence/hot-update.json`：

| 段 | 断言 |
|---|---|
| A 排空 | 在途求解耗时 6.07s，`/shutdown`（202）之后**仍收到 200 + 结构化响应**；进程 exit=0 |
| B 滚动 | 逐个重启 w1 期间，w2 **56 次请求 0 失败**；重启后的 w1 立刻能服务 |
| C blue-green | 备用端口先起新进程 → 健康检查 200 → 才摘旧进程（202/exit 0）→ 新进程继续服务 |

## 8. 一个真踩到的运维坑（已写进 `docs/部署-容器.md` §5）

在 **Git Bash** 里执行 `docker run ... --cache-dir /data/cache`，MSYS 会把 `/data/cache`
改写成 `C:/Program Files/Git/data/cache` 再交给 docker，容器启动日志因此变成：

```
[cfsolve] 监听 http://0.0.0.0:8878 (... cache=sqlite:/app/C:/Program Files/Git/data/cache ...)
```

表现极具误导性：容器"起来了"，但 `/data/.env` 里没有 token（写到了别处），客户端只能拿到 401。
判定方法就一句：**看容器启动日志第一行的 `cache=`**。规避：命令前加 `MSYS_NO_PATHCONV=1`。

---

## 9. 边界（不要把这些当已覆盖）

- 本机没有生产环境/云凭证：这里的"部署"是 compose 与本机多进程，**不是**线上灰度/回滚；
- 未压测"容器 + 真浏览器并发求解"的稳态吞吐（离线压测只覆盖回放与准入路径）；
- 未验证多宿主机（Swarm/K8s）下的共享卷缓存一致性；
- `evidence/hot-update.json` 的 `limitations` 也写明：没有负载均衡器，端口切换由操作者完成。
