"""跨事件循环安全的异步锁（v6.1 嫁接层配套）。

为什么需要：厂商求解器（akamai/awswaf/botguard/cloudflare/datadome/hcaptcha/
perimeterx/recaptcha）在模块级各持一把 `asyncio.Lock()` 做"同类型串行"。
上游是单进程 uvicorn（一个事件循环）跑，所以没问题。

本仓库的主服务是 `ThreadingHTTPServer`：**每个请求在自己的线程里
`asyncio.run(...)` 开一个新事件循环**。模块级 `asyncio.Lock` 会在首次等待时
绑定到那个循环，第二个请求在另一个循环里 await 它就直接
`RuntimeError: ... is bound to a different event loop` 并**永久挂住**
（实测：4 线程同时求解，2 个报错、进程要 kill）。

`loop_lock()` 返回的锁把"互斥"拆到两层：
  · 同一事件循环内：真正的 `asyncio.Lock`（保持上游行为）；
  · 跨线程/跨循环：由 `solver/multicaptcha.py` 的每类型 `threading.Lock` 保证
    （它包在 `_dispatch` 外层，同一类型永远不会有两个线程同时进来）。
所以这里只需要"按循环各一把"，不需要跨线程互斥 —— 那层已经在上面了。

诚实披露：如果把这个子包**单独**当服务跑（不用 multicaptcha 层），跨循环就没有
第二道闸门了。所以不要绕过 `solver/multicaptcha.py` 直接开自己的事件循环池。
"""

from __future__ import annotations

import asyncio
import threading


class LoopLock:
    """按运行中的事件循环惰性分配一把 asyncio.Lock 的异步上下文管理器。

    ⚠️ 释放语义：`async with _solve_lock:` 只调用**本对象**的 `__aenter__`/`__aexit__`
    （不会用 `__aenter__` 的返回值去释放），所以"这一次拿到了哪把锁"必须按
    **当前 task** 记录，不能存在共享列表里 —— 同一循环里两个协程并发进入时，
    共享列表按 LIFO 出栈会配错锁，表现为"提前释放/永不释放"。
    """

    __slots__ = ("_locks", "_guard", "_held_by_task")

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._guard = threading.Lock()
        self._held_by_task: dict[object, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        key = id(loop)
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = asyncio.Lock()
            return lock

    async def __aenter__(self) -> "LoopLock":
        lock = self._lock()
        await lock.acquire()
        self._held_by_task[asyncio.current_task()] = lock
        return self

    async def __aexit__(self, *exc) -> None:
        held = self._held_by_task.pop(asyncio.current_task(), None)
        if held is not None:
            held.release()

    # 兼容可能存在的 `lock.locked()` 检查
    def locked(self) -> bool:
        with self._guard:
            return any(lk.locked() for lk in self._locks.values())


def loop_lock() -> LoopLock:
    """厂商求解器模块级锁的替代品：`_solve_lock = loop_lock()`。"""
    return LoopLock()