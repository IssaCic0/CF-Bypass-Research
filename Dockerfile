# 求解服务镜像（v4.0 T-41）
#
# ⚠️ 状态：**未在本机验证**（本机 Docker daemon 未运行）。以下内容是按官方文档写的，
#    第一次在有权器的机器上 `docker build` 时必须重新确认依赖包名与 camoufox 版本。
#    验证命令见文末。
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
RUN python -m camoufox fetch && chown -R solver:solver /home/solver/.cache

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
