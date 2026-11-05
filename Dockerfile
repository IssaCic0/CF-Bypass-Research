# 求解服务镜像（v4.0 T-41）
#
# ✅ 状态：**已在本机实测**（v4.7，Docker 29.9.2 / linux-amd64）。
#    构建：`docker build --build-arg SLIM_FONTS=1 -t cf-bypass-solver:6.3.0 .` → 3.52 GB
#    运行：容器 **5–6s** 就绪（/health → state=ready）；`/detect` / `/solve` / `/metrics`
#          / `/shutdown`（202 + exit 0）六项全部实测通过。见 `evidence/v47-container.md`。
#    唯一需要重新确认的点：**换 camoufox 版本时** Debian 依赖包名可能变（base 是 python:3.12-slim，
#    包名跟 Debian 主版本走）。
#
# 两个刻意的取舍：
#   1. **camoufox 二进制在 build 阶段 fetch**，不放到运行期。
#      运行期 fetch 需要外网 + 几百 MB 下载，容器会在第一次求解时超时失败——
#      这是这套栈最常见的"镜像能起、一求就挂"的坑。
#   2. 用非 root 用户跑。求解服务会执行浏览器进程，root 身份放大一切提权面。

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CF_SOLVE_TOKEN_FILE=/data/.env \
    CF_CACHE_DIR=/data/cache

# Camoufox 是一个打过补丁的 Firefox：需要 GTK/ALSA/X11 等运行库。
# 不装这些的表现是"浏览器进程启动即退出"，而 /health 只会说 browser-unavailable。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgtk-3-0 libdbus-glib-1-2 libxt6 libx11-xcb1 libasound2 \
        libxcomposite1 libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libatk1.0-0 libatk-bridge2.0-0 libnss3 libnspr4 \
        fonts-liberation tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY solver/ ./solver/
COPY scripts/ ./scripts/
# 万盾嫁接层（v6.1）：11 厂商求解器副本。不含 ONNX 模型（.gitignore）——
# 构建上下文里若本地存在 *.onnx 会一起打进去（aliyun/arkose 的视觉分类更强）；
# 没有也能用：aliyun 回落 cv2 gap 检测、reCAPTCHA classifier=auto 回落 mistral。
# cloakbrowser 未装时扩展 want 报 not-configured，CF 主链路完全不受影响。
COPY captchas/ ./captchas/

RUN useradd --create-home --uid 10001 solver \
    && mkdir -p /data/cache && chown -R solver:solver /data /app

# 关键：让 fetch 落到**运行期用户的 HOME**，再把属主交给他。
#
# 两个实测教训（v4.4 真跑容器时抓到）：
#  1. 直接在 USER solver 之前 fetch，浏览器会落进 root 的 HOME（/root/.cache/camoufox），
#     而运行期用户是 solver（HOME=/home/solver）→ 缓存是空的 → **每个新容器首次启动
#     都重新下载浏览器**（实测白等 110s、多占 ~150MB）。
#  2. 单纯改成 `USER solver` 后 fetch，camoufox 的 fpgen 模型那一步会报
#     "A model this user cannot read was installed by another account"（它按目录属主判权限）。
# 所以：把 HOME 指到 solver 的家目录，以 root 完成 fetch，**再把属主整体交给 solver**。
ENV HOME=/home/solver

# 可选瘦身：Camoufox 自带 **2.1 GB 字体包**（用于字体枚举指纹的真实性），
# 压缩后约占镜像 1.3 GB。删掉能显著变小，但**会削弱指纹真实性**，对"查字体"的站点
# 可能降低成功率 —— 所以默认不删（保真优先）。
#   docker build --build-arg SLIM_FONTS=1 -t cf-bypass-solver:slim .   # 只删 CJK 字体包
#   docker build --build-arg SLIM_FONTS=2 -t cf-bypass-solver:slim .   # 删全部字体
#
# 关键：删除必须**与 fetch 在同一个 RUN 里**。单独写一个 RUN 去 rm，
# 只是在新层里加了一个 whiteout，镜像一点都不会变小（实测：分开写时 slim 仍是 4.97GB）。
ARG SLIM_FONTS=0
ARG PREWARM_ADDONS=1
# 为什么还要单独预热插件：camoufox 的 uBlock Origin **不在** `camoufox fetch` 的产物里 ——
# 它是在**浏览器首次启动**时才下载并解压的。实测：只 build 不预热时镜像里 `addons/` 是空目录，
# 每个新容器启动都会打 "Downloading addon (UBO) 0%→100%"。
# 这与本文件开头的取舍直接矛盾（"运行期 fetch 需要外网，容器会起不来/超时"）：
# 离线或受限网络下，浏览器就永远起不来。所以这里在 build 阶段**真的把浏览器拉起来一次**
# （headless，不需要显示器），把插件预热进镜像。体积代价 ≈2 MB。
# 顺带修掉一个反向操作：原来的 slim 分支会 `rm -rf addons/*/` —— 为了省 2MB 而把
# "离线可用"删掉，代价比收益大得多，已移除。
RUN python -m camoufox fetch \
 && if [ "$SLIM_FONTS" != "0" ]; then \
        B=$(ls -d /home/solver/.cache/camoufox/browsers/official/*/ | head -1); \
        echo "SLIM_FONTS=$SLIM_FONTS -> $B"; \
        if [ "$SLIM_FONTS" = "1" ]; then rm -rf "$B/fonts/M"; else rm -rf "$B/fonts"; fi; \
    fi \
 && if [ "$PREWARM_ADDONS" != "0" ]; then \
        ok=0; \
        for i in 1 2 3; do \
            if python -c "from camoufox.sync_api import Camoufox as C; c=C(headless=True); b=c.__enter__(); b.close(); c.__exit__(None,None,None)"; then ok=1; break; fi; \
            echo "插件预热第 $i 次失败，30s 后重试"; sleep 30; \
        done; \
        if [ "$ok" != "1" ]; then \
            echo "ERROR: 插件预热失败，构建中止（不是『少个插件』那么简单）。"; \
            echo "  实测（2026-11-04）：camoufox 的 UBO 插件源依赖 api.github.com 列 release，"; \
            echo "  该接口会 403 限流 / 404 抖动；而真正的下载很慢（容器内首次启动实测 40s 才 7%，"; \
            echo "  全量分钟级）。没有预热 → 每个新容器首启都要现场下载几分钟 → /health 长时间 503"; \
            echo "  → 求解服务在 CI/离线环境里等于起不来。"; \
            echo "  为什么不再只打 WARN：旧写法 exit 0 但镜像缺件，CI 会绿、发布物在有网环境下才偶然能用。"; \
            echo "  当前可用的做法：构建时显式 --build-arg PREWARM_ADDONS=0（接受『首启慢数分钟』的代价），"; \
            echo "  或先在能稳定访问 GitHub 的网络里构建一次并把 /home/solver/.cache 复用到镜像。"; \
            exit 1; \
        fi; \
    fi \
 && chown -R solver:solver /home/solver/.cache

USER solver

VOLUME ["/data"]
EXPOSE 8878

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8878/health', timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
# 容器内必须绑 0.0.0.0；token 首启自动写入 /data/.env，
# 不带 Authorization 头调用会拿到 401（详见 docs/API.md）。
CMD ["python", "solver/service.py", "--host", "0.0.0.0", "--port", "8878", \
     "--cache-dir", "/data/cache", "--token-file", "/data/.env"]
