"""鉴权 token 的装载与指纹（从 service.py 搬出来，只搬不改，v4.4）。

为什么单独一个模块：这段逻辑是"凭证入口"，安全审查要一眼能看完；
埋在 2.4k 行的服务文件里既难审也不好单独测。

语义（原实现保留）：
  · 优先级：环境变量 `CF_SOLVE_TOKEN` → token 文件 → **自动生成并原子写入**（0600）；
  · 并发首启用 `O_CREAT|O_EXCL` 独占创建，抢不到就回读对端已经写好的那个；
  · 日志里只打 SHA256 指纹前 12 位，绝不打 token 本体。
"""

from __future__ import annotations

import hashlib
import os
import secrets

TOKEN_ENV = "CF_SOLVE_TOKEN"
TOKEN_LINE_PREFIX = "CF_SOLVE_TOKEN="


def load_or_create_token(path: str) -> tuple[str, str]:
    """按优先级取 token：环境变量 → token 文件 → 自动生成并原子写入。

    返回 (token, source)，source ∈ {"env", "file", "generated"}。
    自动生成时用 O_CREAT|O_EXCL 独占创建；若并发下已存在则回读（多进程首启竞态）。
    """
    env = (os.environ.get(TOKEN_ENV) or "").strip()
    if env:
        return env, "env"

    if os.path.exists(path):
        token = _read_token_file(path)
        if token:
            return token, "file"

    token = secrets.token_urlsafe(32)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    line = f"{TOKEN_LINE_PREFIX}{token}\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = _read_token_file(path)
        if existing:
            return existing, "file"
        # 文件存在但没有 token 行：追加（保留用户其它 env 内容）
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        os.chmod(path, 0o600)
        return token, "generated"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(line)
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return token, "generated"


def _read_token_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line.startswith(TOKEN_LINE_PREFIX):
                    return line[len(TOKEN_LINE_PREFIX):].strip()
    except OSError:
        return ""
    return ""


def token_fingerprint(token: str) -> str:
    """日志里只打指纹，不打 token 本体。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
