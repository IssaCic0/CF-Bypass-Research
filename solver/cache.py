"""求解结果的缓存层。

为什么要单独一层：
  · 原实现是进程内 `dict`：重启即失、跨进程不共享。而"单机多进程扩容"的前提正是
    **同一域的 clearance 全局只解一次**（CF 按域+IP+UA 绑定，缓存语义天然正确）。
  · 多进程各自为政时，4 个 worker 会把同一个域解 4 次 —— 吞吐数字好看，实际是重复劳动。

两种实现：
  · `MemoryStore`：默认，单进程用。保留它是因为一旦落盘目录不可用必须能降级。
  · `FileStore`：落盘 + 加密 + 原子写 + 上限淘汰，供多进程共享。

安全取向（按既有决策 C-2）：
  · 落盘内容用 Fernet 加密，密钥由服务端 token 经 PBKDF2 派生 —— 换 token 自动作废旧缓存。
  · 文件权限 0600、目录 0700；`strings` 不应看到 `cf_clearance=`。
  · 读失败 / 解密失败 / schema 不符 → 视为未命中并删文件，绝不抛异常打断服务。

并发取向（禁区：不加文件锁）：
  · 写入用「同目录临时文件 + os.replace」原子替换，读者要么看到旧值要么看到新值。
  · 不做跨进程互斥：极端情况下两个进程同时解同一域，代价是多烧一次浏览器，
    比引入 flock/msvcrt 的跨平台坑更划算。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from typing import Any, Iterable, Protocol

SCHEMA_VERSION = 1
SALT_FILENAME = "salt.bin"
PBKDF2_ITERATIONS = 200_000


class CacheStore(Protocol):
    """缓存存储协议。`key` 已由调用方算好（含域名+出口标识）。"""

    def get(self, key: str) -> dict | None: ...
    def set(self, key: str, value: dict) -> None: ...
    def delete(self, key: str) -> None: ...
    def items(self) -> Iterable[tuple[str, dict]]: ...
    def purge_expired(self) -> int: ...


class MemoryStore:
    """进程内缓存（默认/降级用）。有上限，避免长跑进程无限增长。"""

    def __init__(self, max_items: int = 4096) -> None:
        self._data: dict[str, dict] = {}
        self._max = max_items

    def get(self, key: str) -> dict | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        if entry.get("expires_at", 0) <= time.time():
            self._data.pop(key, None)
            return None
        return entry

    def set(self, key: str, value: dict) -> None:
        self._data[key] = value
        while len(self._data) > self._max:
            self._data.pop(next(iter(self._data)))

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def items(self) -> Iterable[tuple[str, dict]]:
        return list(self._data.items())

    def purge_expired(self) -> int:
        now = time.time()
        gone = [k for k, v in self._data.items() if v.get("expires_at", 0) <= now]
        for k in gone:
            self._data.pop(k, None)
        return len(gone)

    def __len__(self) -> int:
        return len(self._data)


class FileStore:
    """落盘缓存：加密 + 原子写 + 过期清理 + 数量上限。

    目录布局：`<cache_dir>/<sha256(key)[:32]>.bin`，内容为 Fernet 密文。
    """

    def __init__(self, cache_dir: str, token: str, max_files: int = 4096) -> None:
        self.dir = os.path.abspath(cache_dir)
        self._max_files = max_files
        os.makedirs(self.dir, exist_ok=True)
        _harden_dir(self.dir)
        self._fernet = _build_fernet(self.dir, token)
        # 近似计数：让大多数 set 不必 listdir+stat（那会落在求解完成后的同步路径上）。
        # 口径说明：它统计的是**本实例的写次数**（覆盖写也会 +1，属于高估）。
        # 多进程共享同一目录时，各进程只数自己的写，所以 --cache-max-files 的实际上限
        # 可能到 N 倍。清理只是空间优化，不影响正确性（过期项读取时仍会被判 miss 并删除）。
        self._count = len(self._list_files())

    # -- 协议实现 ----------------------------------
    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            blob = open(path, "rb").read()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            payload = json.loads(self._fernet.decrypt(blob).decode("utf-8"))
        except Exception:  # noqa: BLE001 —— 解密失败/坏文件一律当未命中并清理
            self._safe_unlink(path)
            return None
        if payload.get("schema_version") != SCHEMA_VERSION:
            self._safe_unlink(path)
            return None
        if payload.get("expires_at", 0) <= time.time():
            self._safe_unlink(path)
            return None
        value = payload.get("value")
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "expires_at": value.get("expires_at", 0),
            "created_at": time.time(),
            "value": value,
        }
        blob = self._fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        path = self._path(key)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".tmp-", suffix=".bin")
            with os.fdopen(fd, "wb") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)      # 原子替换：读者不会看到半截文件
            tmp = None
            _harden_file(path)
        finally:
            if tmp:
                self._safe_unlink(tmp)
        self._count += 1
        if self._count > self._max_files:
            self._enforce_limit()

    def delete(self, key: str) -> None:
        self._safe_unlink(self._path(key))
        self._count = max(0, self._count - 1)

    def items(self) -> Iterable[tuple[str, dict]]:
        # 文件名是 key 的哈希，无法反推 key；对外只提供遍历值的能力，
        # 因此这里用文件名当 key 返回（调用方只关心 value）。
        out = []
        for name in self._list_files():
            path = os.path.join(self.dir, name)
            try:
                payload = json.loads(self._fernet.decrypt(open(path, "rb").read()).decode("utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if payload.get("schema_version") == SCHEMA_VERSION and isinstance(payload.get("value"), dict):
                out.append((name, payload["value"]))
        return out

    def purge_expired(self) -> int:
        gone = 0
        now = time.time()
        for name in self._list_files():
            path = os.path.join(self.dir, name)
            try:
                payload = json.loads(self._fernet.decrypt(open(path, "rb").read()).decode("utf-8"))
            except Exception:  # noqa: BLE001
                self._safe_unlink(path)
                gone += 1
                continue
            if payload.get("schema_version") != SCHEMA_VERSION or payload.get("expires_at", 0) <= now:
                self._safe_unlink(path)
                gone += 1
        self._count = len(self._list_files())
        return gone

    def __len__(self) -> int:
        return len(self._list_files())

    # -- 内部 --------------------------------------
    def _path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.dir, digest + ".bin")

    def _list_files(self) -> list[str]:
        """列出缓存文件。**必须排除盐文件**——否则 purge 会把它当坏文件删掉，
        进而导致所有缓存再也解不开（这个坑被 tests/test_cache.py 抓到过）。"""
        try:
            return [
                n
                for n in os.listdir(self.dir)
                if n.endswith(".bin") and not n.startswith(".tmp-") and n != SALT_FILENAME
            ]
        except OSError:
            return []

    def _enforce_limit(self) -> None:
        files = self._list_files()
        excess = len(files) - self._max_files
        if excess <= 0:
            return
        # 按 mtime 淘汰最旧的
        stamped = []
        for name in files:
            path = os.path.join(self.dir, name)
            try:
                stamped.append((os.stat(path).st_mtime, path))
            except OSError:
                continue
        stamped.sort()
        for _, path in stamped[:excess]:
            self._safe_unlink(path)
        self._count = len(self._list_files())

    @staticmethod
    def _safe_unlink(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass


def _build_fernet(cache_dir: str, token: str):
    """由 token + 盐派生 Fernet 密钥。盐首次生成后固定下来（原子写）。"""
    from cryptography.fernet import Fernet  # 延迟导入：纯内存模式不需要该依赖

    salt_path = os.path.join(cache_dir, SALT_FILENAME)
    salt = _load_or_create_salt(salt_path)
    key = hashlib.pbkdf2_hmac("sha256", token.encode("utf-8"), salt, PBKDF2_ITERATIONS, 32)
    return Fernet(base64.urlsafe_b64encode(key))


def _load_or_create_salt(path: str) -> bytes:
    try:
        data = open(path, "rb").read()
        if len(data) >= 16:
            return data[:16]
    except FileNotFoundError:
        pass
    except OSError:
        pass
    salt = os.urandom(16)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".salt-")
        with os.fdopen(fd, "wb") as f:
            f.write(salt)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
        _harden_file(path)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return salt


def _harden_dir(path: str) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _harden_file(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def build_store(cache_dir: str | None, token: str, max_files: int = 4096) -> tuple[Any, str]:
    """按配置构造缓存：给了目录就落盘，否则内存。返回 (store, 实际类型描述)。

    落盘失败（缺 cryptography / 目录不可写）时**降级为内存**而不是启动失败——
    但会把原因返回给调用方打日志，不允许静默降级。
    """
    if not cache_dir:
        return MemoryStore(), "memory"
    try:
        return FileStore(cache_dir, token, max_files=max_files), f"file:{os.path.abspath(cache_dir)}"
    except Exception as e:  # noqa: BLE001
        return MemoryStore(), f"memory(fallback: {type(e).__name__}: {str(e)[:100]})"
