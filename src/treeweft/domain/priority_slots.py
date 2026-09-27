"""Priority slots — a concurrency limit whose waiters are served by priority.

An `asyncio.Semaphore` serves waiters first come, first served. When
background work queues hundreds of calls, an interactive call that arrives
later waits behind all of them. Here the capacity is the same, but when a
slot frees it goes to the waiting caller with the highest priority; callers
of equal priority are served in arrival order.

Background work is not starved: while it has a caller waiting, interactive
callers hold at most `capacity - 1` slots between them (all of them when
the capacity is 1).

A slot is handed to a waiter the moment it frees, so a free slot and a
waiting caller never exist together.

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
        self._held = {p: 0 for p in Priority}
        self._waiters: dict[Priority, deque[asyncio.Future[None]]] = {
            p: deque() for p in Priority
        }

    @property
    def in_use(self) -> int:
        return sum(self._held.values())

    def waiting(self, priority: Priority | None = None) -> int:
        """Callers waiting for a slot, at one priority or at all of them."""
        queues = self._waiters.values() if priority is None else [self._waiters[priority]]
        return sum(1 for q in queues for fut in q if not fut.done())

    async def acquire(self, priority: Priority) -> None:
        if self.in_use < self._capacity:
            self._held[priority] += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters[priority].append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # The slot was handed over just before the cancellation
                # arrived. Pass it on, or it is lost for good.
                self.release(priority)
            else:
                try:
                    self._waiters[priority].remove(fut)
                except ValueError:
                    pass  # A release already dropped it from the queue.
            raise

    def release(self, priority: Priority) -> None:
        """Give back a slot acquired at `priority`."""
        self._held[priority] -= 1
        self._hand_over()

    @asynccontextmanager
    async def hold(self, priority: Priority) -> AsyncGenerator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release(priority)

    def _hand_over(self) -> None:
        # The slot counts as held from here, on the waiter's behalf, so a
        # caller that arrives before the waiter runs cannot take it.
        while self.in_use < self._capacity:
            priority = self._next_priority()
            if priority is None:
                return
            self._held[priority] += 1
            self._waiters[priority].popleft().set_result(None)

    def _next_priority(self) -> Priority | None:
        """The priority whose first waiter gets the free slot."""
        for queue in self._waiters.values():
            while queue and queue[0].done():  # cancelled while waiting
                queue.popleft()
        interactive = bool(self._waiters[Priority.INTERACTIVE])
        background = bool(self._waiters[Priority.BACKGROUND])
        if interactive and background:
            limit = max(1, self._capacity - 1)
            if self._held[Priority.INTERACTIVE] >= limit:
                return Priority.BACKGROUND
        if interactive:
            return Priority.INTERACTIVE
        if background:
            return Priority.BACKGROUND
        return None
