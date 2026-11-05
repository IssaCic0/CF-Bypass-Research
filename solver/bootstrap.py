"""CLI 装配与启动（v5.0 T-56 自 service.py 下沉，只搬不改）。

为什么拆：`main` 是 220+ 行的"参数解析 + 组件装配 + 信号处理"纯装配代码，
与 Solver/Handler 的业务逻辑无状态耦合（只引用它们的构造器与常量）。
service 侧只留 `main = bootstrap_main` 的再导出 —— 测试与文档里
`service.main` 的引用零感知；`python solver/service.py` 入口行为不变。
"""

from __future__ import annotations

import argparse
import os
import signal
import threading

try:  # 作为包导入
    from .service import (
        COMPAT_DEFAULT_MAX_PENDING,
        DEFAULT_MAX_DETECT,
        DEFAULT_MAX_INFLIGHT,
        DEFAULT_MAX_QUEUE,
        DEFAULT_TTL,
        MAX_URL_LEN,
        MIN_TTL,
        DETECT_CACHE_TTL,
        SHUTDOWN_GRACE,
        TOKEN_ENV,
        TOKEN_LINE_PREFIX,
        SolveError,
        BackendRouter,
        CamoufoxBackend,
        CfsolveBackend,
        Engine,
        FlareSolverrBackend,
        Handler,
        Prewarmer,
        PydollBackend,
        Server,
        Solver,
        build_engine_factory,
        engine_preflight,
        find_chrome,
        load_or_create_token,
        parse_proxy_url,
        parse_targets,
        token_fingerprint,
        ProxyPool,
    )
except ImportError:  # 直接以脚本运行
    from service import (  # type: ignore[no-redef]
        COMPAT_DEFAULT_MAX_PENDING,
        DEFAULT_MAX_DETECT,
        DEFAULT_MAX_INFLIGHT,
        DEFAULT_MAX_QUEUE,
        DEFAULT_TTL,
        MAX_URL_LEN,
        MIN_TTL,
        DETECT_CACHE_TTL,
        SHUTDOWN_GRACE,
        TOKEN_ENV,
        TOKEN_LINE_PREFIX,
        SolveError,
        BackendRouter,
        CamoufoxBackend,
        CfsolveBackend,
        Engine,
        FlareSolverrBackend,
        Handler,
        Prewarmer,
        PydollBackend,
        Server,
        Solver,
        build_engine_factory,
        engine_preflight,
        find_chrome,
        load_or_create_token,
        parse_proxy_url,
        parse_targets,
        token_fingerprint,
        ProxyPool,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Cloudflare cf_clearance 求解服务（Camoufox）")
    ap.add_argument("--port", type=int, default=8878)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--ttl", type=int, default=DEFAULT_TTL,
                    help="缓存有效期**上限**（秒）。真实值取 cf_clearance 自带的 expires，且不超过本值")
    ap.add_argument("--headed", action="store_true", help="有头模式（调试用）")
    ap.add_argument("--max-queue", type=int, default=DEFAULT_MAX_QUEUE, help="有界队列容量，满即 429")
    ap.add_argument("--max-inflight", type=int, default=DEFAULT_MAX_INFLIGHT, help="并发求解上限，满即 429")
    ap.add_argument("--allow-remote-shutdown", action="store_true",
                    help="允许非回环地址触发 POST /shutdown（容器/端口映射需要；token 仍是第一道门）")
    ap.add_argument("--audit-log", default="", help="审计日志（JSONL）路径；空=关闭。只记白名单字段，不含凭证")
    ap.add_argument("--detect-cache-ttl", type=float, default=DETECT_CACHE_TTL,
                    help="探测结果（/detect 与 route=auto）按域缓存的 TTL（秒）；0=关闭")
    ap.add_argument("--max-detect", type=int, default=DEFAULT_MAX_DETECT,
                    help="并发探测（/detect）上限，满即 429；与求解额度分开计")
    ap.add_argument("--no-wedge-restart", dest="wedge_restart", action="store_false",
                    help="worker 卡死时**不**自重启进程（默认会以退出码 75 退出，由 supervisor 拉起）")
    ap.add_argument("--wedge-grace", type=float, default=30.0,
                    help="任务超出预算后再等多久判定卡死（秒，默认 30）")
    ap.add_argument("--compat-max-pending", type=int, default=COMPAT_DEFAULT_MAX_PENDING,
                    help="YesCaptcha 兼容层（/createTask）待处理任务上限，满即 ERROR_NO_SLOT_AVAILABLE")
    ap.add_argument("--egress-id", default=os.environ.get("CF_EGRESS_ID", "default"),
                    help="本进程的出口标识；换出口必须换 id，否则会复用别处 IP 的凭证")
    # 环境变量回退不是"顺手加的便利"：Dockerfile 与 docker-compose.yml 早就设了
    # `CF_SOLVE_TOKEN_FILE`，但代码里从来没人读过它 —— 也就是说这个变量一直是死的。
    # 后果很具体：脱离 compose 的 `docker run` 里 `--token-file` 会退回容器内的 `.env`，
    # 而那不是挂载卷 ⇒ 每次重启都重新生成 token ⇒ 客户端旧 token 全 401，
    # 且缓存密钥由 token 派生（cache.py:723）⇒ 整个共享缓存被作废。
    ap.add_argument("--token-file", default=os.environ.get("CF_SOLVE_TOKEN_FILE", ".env"),
                    help=f"token 文件；首次启动自动生成并写入 {TOKEN_LINE_PREFIX}<token>。"
                         f"默认取环境变量 CF_SOLVE_TOKEN_FILE，未设置时用 .env")
    ap.add_argument("--no-auth", action="store_true", help="关闭鉴权（仅限本机调试；生产禁用）")
    ap.add_argument("--allow-private-hosts", action="store_true", help="允许访问私网/回环地址（仅限本机联调）")
    ap.add_argument("--cache-dir", default=os.environ.get("CF_CACHE_DIR", ""),
                    help="落盘缓存目录（多进程共享）的路径；空=进程内内存缓存")
    ap.add_argument("--cache-max-files", type=int, default=4096, help="落盘缓存的文件数上限（超出按最旧淘汰）")
    ap.add_argument("--proxy", action="append", default=[],
                    help="代理出口，可重复：scheme://user:pass@host:port。按域一致性哈希做粘性")
    ap.add_argument("--proxy-egress-ip", action="append", default=[],
                    help="登记某出口的实测 IP，格式 http://host:port=1.2.3.4（改 IP 会自动失效旧缓存）")
    ap.add_argument("--browser-os", default=os.environ.get("CF_BROWSER_OS", ""),
                    choices=["", "windows", "macos", "linux"],
                    help="固定 Camoufox 的 OS persona（windows/macos/linux）；空=随机（Camoufox 默认，每次启动会变）")
    ap.add_argument("--prewarm", default=os.environ.get("CF_PREWARM", ""),
                    help="保活目标，逗号分隔（可只写域名）。空=不启用保活")
    ap.add_argument("--prewarm-file", default=os.environ.get("CF_PREWARM_FILE", ""),
                    help="保活目标文件：一行一个，允许 # 注释")
    ap.add_argument("--prewarm-interval", type=float, default=60.0, help="保活扫描间隔（秒）")
    ap.add_argument("--renew-before", type=float, default=300.0,
                    help="剩余寿命低于该值时提前续期（秒）；应大于单次求解耗时")
    ap.add_argument("--engine", default=os.environ.get("CF_ENGINE", "camoufox"),
                    choices=["camoufox", "cloakbrowser", "patchright"],
                    help="浏览器引擎：camoufox（默认，C++ 层改指纹）/ cloakbrowser（Chromium 源码层补丁，"
                         "二进制专有）/ patchright（反检测 Chromium，免费的弱一些）")
    ap.add_argument("--cfsolve-bin", default=os.environ.get("CFSOLVE_BIN", ""),
                    help="纯协议后端：cfsolve 的 challenge 可执行文件路径（见 docs/纯协议-cfsolve.md）")
    ap.add_argument("--cfsolve-profile", default=os.environ.get("CFSOLVE_PROFILE", ""),
                    help="纯协议后端：浏览器 profile 文件路径（cfsolve profiles/*.json）")
    ap.add_argument("--cfsolve-timeout", type=float, default=120.0, help="纯协议后端单次超时（秒）")
    ap.add_argument("--pydoll", action="store_true",
                    help="启用 pydoll 后端（零 WebDriver 的 CDP 直连 Chrome，自带 CF 求解器）")
    ap.add_argument("--chrome-path", default=os.environ.get("CHROME_PATH", ""),
                    help="Chrome 可执行文件路径（空=自动探测常见位置）")
    ap.add_argument("--pydoll-headless", action="store_true",
                    help="pydoll 用 headless（注意：headless 下 UA 会带 HeadlessChrome，成功率通常更低）")
    ap.add_argument("--pydoll-wait", type=float, default=45.0, help="pydoll 单次等待上限（秒）")
    ap.add_argument("--flaresolverr-session", default=os.environ.get("FLARESOLVERR_SESSION", ""),
                    help="FlareSolverr 会话名：非空时启用会话粘性（首次使用自动 sessions.create，"
                         "退出时 destroy）。留空=每次请求新建 webdriver（更慢，但不泄漏）")
    ap.add_argument("--flaresolverr", default=os.environ.get("FLARESOLVERR_URL", ""),
                    help="可选兜底后端：自托管 FlareSolverr 兼容服务的地址（例如 http://127.0.0.1:8191）")
    args = ap.parse_args(argv)

    token = ""
    source = "disabled"
    if not args.no_auth:
        token, source = load_or_create_token(args.token_file)

    try:
        engine_enum = Engine.parse(args.engine)
        engine_preflight(engine_enum)
    except (ValueError, SolveError) as e:
        msg = getattr(e, "message", str(e))
        hint = getattr(e, "hint", "")
        print(f"[cfsolve] 引擎不可用：{msg}" + (f" | 安装：{hint}" if hint else ""), flush=True)
        return 2

    # 代理池 与 cfsolve/pydoll **不能同时启用**（这是启动即失败的硬冲突，不是警告）。
    # 原因：cfsolve 与 pydoll 都在自己进程里直连（cfsolve 自带 BoringSSL、pydoll 自带 Chrome），
    # 无法走代理池 ⇒ 它们拿到的 cf_clearance 绑定的是**本机直连 IP**；而 Go 侧回放会按
    # 凭证的出口标识去走代理 ⇒ 求解与回放出口不一致 ⇒ 稳定的静默 403。
    # 与其让它上线后表现为"随机 403"，不如启动就说清楚。
    if args.proxy and (args.cfsolve_bin or args.pydoll):
        print(
            "[cfsolve] 配置冲突：--proxy（代理池）与 --cfsolve-bin/--pydoll 不能同时启用。\n"
            "  原因：cfsolve/pydoll 在各自进程里直连，走不了代理池；它们发出的凭证绑的是直连 IP，\n"
            "        而 Go 侧回放会按凭证出口走代理 → 出口不一致 → 稳定 403。\n"
            "  处理：二选一 —— 去掉 --proxy，或去掉 --cfsolve-bin/--pydoll。",
            flush=True,
        )
        return 2

    pool = None
    if args.proxy:
        pool = ProxyPool(args.proxy)
        for item in args.proxy_egress_ip:
            # rpartition：口令里的 `=` 不能当分隔符（IP 里不会有 `=`）
            server, _, ip = item.rpartition("=")
            if ip and not pool.set_egress_ip_by_server(server.strip(), ip.strip()):
                print(f"[cfsolve] 警告：--proxy-egress-ip 里的出口不在 --proxy 列表中：{server}", flush=True)

    solver = Solver(
        headless=not args.headed,
        ttl=args.ttl,
        max_queue=args.max_queue,
        max_inflight=args.max_inflight,
        max_detect=args.max_detect,
        egress_id=args.egress_id,
        allow_private_hosts=args.allow_private_hosts,
        cache_dir=args.cache_dir or None,
        token=token or "memory-only",     # 无 token 时也给个固定值，保证能派生密钥
        cache_max_files=args.cache_max_files,
        proxy_pool=pool,
        browser_os=args.browser_os,
        detect_cache_ttl=args.detect_cache_ttl,
        compat_max_pending=args.compat_max_pending,
        # 服务入口默认开自重启：worker 卡死时进程退出码 75，由 docker restart / run-pool 拉起。
        # 库/测试构造 Solver 时不传，保持默认 False —— 不能因为构造一个 Solver 就杀掉宿主。
        wedge_restart=bool(args.wedge_restart),
        wedge_grace=float(args.wedge_grace),
        browser_factory=None if engine_enum is Engine.CAMOUFOX else build_engine_factory(
            engine_enum, os_persona=args.browser_os
        ),
    )
    Handler.solver = solver
    Handler.token = token
    Handler.allow_remote_shutdown = bool(args.allow_remote_shutdown)
    Handler.audit_path = (args.audit_log or "").strip()
    if Handler.audit_path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(Handler.audit_path)) or ".", exist_ok=True)
            with open(Handler.audit_path, "a", encoding="utf-8"):
                pass
            print(f"[cfsolve] 审计日志已启用：{Handler.audit_path}", flush=True)
        except OSError as e:
            Handler.audit_path = ""
            print(f"[cfsolve] 警告：审计日志不可写（{e}），已关闭", flush=True)
    # 后端优先级：纯协议（若配置）→ 本地浏览器 → 自托管 FlareSolverr
    backends = []
    if args.cfsolve_bin and args.cfsolve_profile:
        backends.append(CfsolveBackend(
            args.cfsolve_bin, args.cfsolve_profile, timeout=args.cfsolve_timeout,
            egress_id=args.egress_id,
        ))
    if args.pydoll:
        pyd = PydollBackend(
            args.chrome_path, headless=args.pydoll_headless, wait=args.pydoll_wait,
            egress_id=args.egress_id,
        )
        if not pyd.is_configured():
            print(f"[cfsolve] 警告：--pydoll 已开启但不可用（chrome={find_chrome(args.chrome_path) or '未找到'}；"
                  f"或缺 pydoll-python）。它会在后端链里被 skip。", flush=True)
        backends.append(pyd)
    backends.append(CamoufoxBackend(solver))
    if args.flaresolverr:
        backends.append(FlareSolverrBackend(
            args.flaresolverr, session=(args.flaresolverr_session or "").strip()
        ))
    # 把 solver 作为治理对象交给 router：缓存/准入/计数/按域统计对**所有后端**生效，
    # 而不是只对"恰好走了 Solver.solve 的那一棒"生效。
    Handler.router = BackendRouter(backends, governance=solver) if len(backends) > 1 else None
    solver.backend_chain = list(Handler.router.backends) if Handler.router else ["camoufox"]

    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"[cfsolve] 端口 {args.host}:{args.port} 绑定失败：{e}", flush=True)
        print("[cfsolve] 换一个 --port，或用 `netstat -ano | findstr :PORT` 找到并停掉占用进程", flush=True)
        solver.close()
        return 2

    print(
        f"[cfsolve] 监听 http://{args.host}:{args.port} "
        f"(camoufox, ttl={args.ttl}s, egress={args.egress_id}, auth={source}, "
        f"cache={solver.cache_backend}, proxies={0 if pool is None else len(args.proxy)})",
        flush=True,
    )
    print(f"[cfsolve] 浏览器引擎：{engine_enum.value}"
          + (f"（os={args.browser_os}）" if args.browser_os else ""), flush=True)
    if Handler.router is not None:
        print(f"[cfsolve] 后端优先级：{Handler.router.backends}（未配置的会被 skip）", flush=True)
    if token:
        print(f"[cfsolve] token 指纹 {token_fingerprint(token)}（来源 {source}；"
              f"完整 token 见 {args.token_file} 或环境变量 {TOKEN_ENV}）", flush=True)

    def _shutdown(signum, _frame):
        print(f"[cfsolve] 收到信号 {signum}，开始优雅退出", flush=True)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass    # 非主线程/平台不支持时忽略

    prewarmer = None
    targets: list[str] = []
    if args.prewarm or args.prewarm_file:
        try:
            targets = parse_targets(args.prewarm, args.prewarm_file)
        except ValueError as e:
            print(f"[cfsolve] 保活配置错误：{e}", flush=True)
            solver.close()
            return 2
        if targets:
            prewarmer = Prewarmer(
                solver,
                targets,
                renew_before=args.renew_before,
                interval=args.prewarm_interval,
            )
            solver.prewarmer = prewarmer
            prewarmer.start()
            print(f"[cfsolve] 保活已启用：{len(targets)} 个域，提前 {args.renew_before:.0f}s 续期，"
                  f"扫描间隔 {args.prewarm_interval:.0f}s", flush=True)
        else:
            print("[cfsolve] --prewarm/--prewarm-file 为空，保活未启用", flush=True)

    try:
        srv.serve_forever(poll_interval=0.5)
    finally:
        if prewarmer is not None:
            prewarmer.stop()
        # 优雅退出三步曲：已经停止接收新连接 → 等在途请求收尾 → 再关资源。
        # 少了"等"这一步，正在求解的调用方会拿到连接被重置（实测 WinError 10054）。
        left = srv.drain(SHUTDOWN_GRACE)
        if left:
            print(f"[cfsolve] 优雅退出：{SHUTDOWN_GRACE:.0f}s 内仍有 {left} 个请求未完成", flush=True)
        else:
            print("[cfsolve] 优雅退出：在途请求已全部完成", flush=True)
        srv.server_close()
        if Handler.router is not None:
            Handler.router.close()      # 释放远端 session（否则会在 FlareSolverr 侧积累）
        solver.close()
    print("[cfsolve] 已退出", flush=True)
    return 0


