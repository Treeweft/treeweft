"""Priority slots — a concurrency limit whose waiters are served by priority.

An `asyncio.Semaphore` serves waiters first come, first served. When
background work queues hundreds of calls, an interactive call that arrives
later waits behind all of them. Here the capacity is the same, but when a
slot frees it goes to the waiting caller with the highest priority; callers
of equal priority are served in arrival order.

Not thread-safe: use from one event loop, like `asyncio.Semaphore`.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from enum import IntEnum
from typing import AsyncGenerator


class Priority(IntEnum):
    """Lower value is served first."""

    INTERACTIVE = 0  # A caller is waiting on the result (query-time work)
    BACKGROUND = 1  # Queued work that may wait (indexing)


class PrioritySlots:
    """Usage:
        slots = PrioritySlots(4)
        async with slots.hold(Priority.INTERACTIVE):
            ...
    """

    def __init__(self, capacity: int):
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        self._capacity = capacity
        self._in_use = 0
        self._waiters: dict[Priority, deque[asyncio.Future[None]]] = {
            p: deque() for p in sorted(Priority)
        }

    @property
    def in_use(self) -> int:
        return self._in_use

    def waiting(self, priority: Priority | None = None) -> int:
        """Callers waiting for a slot, at one priority or at all of them."""
        queues = self._waiters.values() if priority is None else [self._waiters[priority]]
        return sum(1 for q in queues for fut in q if not fut.done())

    async def acquire(self, priority: Priority) -> None:
        if self._in_use < self._capacity and not self._outranked(priority):
            self._in_use += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[priority].append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # The slot was handed over just before the cancellation
                # arrived. Pass it on, or it is lost for good.
                self.release()
            else:
                self._waiters[priority].remove(fut)
            raise

    def release(self) -> None:
        self._in_use -= 1
        self._hand_over()

    @asynccontextmanager
    async def hold(self, priority: Priority) -> AsyncGenerator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()

    def _outranked(self, priority: Priority) -> bool:
        """True if a caller of the same or a higher priority is already
        waiting, so a free slot belongs to that caller first."""
        return any(self.waiting(p) for p in Priority if p <= priority)

    def _hand_over(self) -> None:
        # The slot is counted as in use from here, on the waiter's behalf, so
        # a caller that arrives before the waiter runs cannot take it.
        while self._in_use < self._capacity:
            fut = self._next_waiter()
            if fut is None:
                return
            self._in_use += 1
            fut.set_result(None)

    def _next_waiter(self) -> asyncio.Future[None] | None:
        for priority in sorted(Priority):
            queue = self._waiters[priority]
            while queue:
                fut = queue.popleft()
                if not fut.done():
                    return fut
        return None
