"""PrioritySlots: a concurrency limit that serves interactive waiters first.

Pure asyncio, no I/O. Waiters are parked on events the test controls, so
the order slots are granted in is observed directly, not inferred from
timing.
"""
from __future__ import annotations

import asyncio

import pytest

from treeweft.domain.priority_slots import Priority, PrioritySlots

pytestmark = pytest.mark.asyncio


async def _settle():
    """Let every task that can run, run."""
    for _ in range(5):
        await asyncio.sleep(0)


class _Worker:
    """Takes a slot, records that it did, holds it until told to finish."""

    def __init__(self, slots: PrioritySlots, name: str, priority: Priority, order: list[str]):
        self.name = name
        self.finish = asyncio.Event()
        self.task = asyncio.create_task(self._run(slots, priority, order))

    async def _run(self, slots, priority, order):
        async with slots.hold(priority):
            order.append(self.name)
            await self.finish.wait()


async def _drain(*workers: _Worker):
    for w in workers:
        w.finish.set()
    await asyncio.gather(*(w.task for w in workers), return_exceptions=True)


class TestCapacity:
    async def test_rejects_a_capacity_below_one(self):
        with pytest.raises(ValueError, match="capacity"):
            PrioritySlots(0)

    async def test_never_runs_more_than_capacity_at_once(self):
        slots = PrioritySlots(2)
        order: list[str] = []
        workers = [_Worker(slots, f"w{i}", Priority.BACKGROUND, order) for i in range(5)]
        await _settle()

        assert order == ["w0", "w1"]
        assert slots.in_use == 2
        assert slots.waiting() == 3

        workers[0].finish.set()
        await _settle()
        assert order == ["w0", "w1", "w2"]
        assert slots.in_use == 2

        await _drain(*workers)
        assert slots.in_use == 0
        assert slots.waiting() == 0


class TestOrder:
    async def test_interactive_waiter_is_served_before_earlier_background_waiters(self):
        slots = PrioritySlots(1)
        order: list[str] = []
        running = _Worker(slots, "running", Priority.BACKGROUND, order)
        queued = [_Worker(slots, f"bg{i}", Priority.BACKGROUND, order) for i in range(3)]
        await _settle()
        late = _Worker(slots, "interactive", Priority.INTERACTIVE, order)
        await _settle()
        assert order == ["running"]

        running.finish.set()
        await _settle()

        assert order == ["running", "interactive"]
        await _drain(late, *queued)
        assert order == ["running", "interactive", "bg0", "bg1", "bg2"]

    async def test_equal_priority_is_served_in_arrival_order(self):
        slots = PrioritySlots(1)
        order: list[str] = []
        workers = [_Worker(slots, f"w{i}", Priority.INTERACTIVE, order) for i in range(4)]
        await _settle()

        await _drain(*workers)

        assert order == ["w0", "w1", "w2", "w3"]

    async def test_new_background_caller_cannot_take_a_slot_promised_to_a_waiter(self):
        """A slot is handed to the waiter when it frees, not when the waiter
        next runs — a caller arriving in between must queue."""
        slots = PrioritySlots(1)
        order: list[str] = []
        running = _Worker(slots, "running", Priority.BACKGROUND, order)
        await _settle()
        waiter = _Worker(slots, "waiter", Priority.INTERACTIVE, order)
        await _settle()

        running.finish.set()
        await running.task  # the slot is released here ...
        barger = _Worker(slots, "barger", Priority.BACKGROUND, order)  # ... and this arrives next
        await _settle()

        assert order == ["running", "waiter"]
        await _drain(waiter, barger)
        assert order == ["running", "waiter", "barger"]

    async def test_background_caller_does_not_pass_a_waiting_interactive_caller(self):
        slots = PrioritySlots(2)
        order: list[str] = []
        a = _Worker(slots, "a", Priority.BACKGROUND, order)
        b = _Worker(slots, "b", Priority.BACKGROUND, order)
        await _settle()
        interactive = _Worker(slots, "interactive", Priority.INTERACTIVE, order)
        background = _Worker(slots, "background", Priority.BACKGROUND, order)
        await _settle()

        a.finish.set()
        await _settle()

        assert order == ["a", "b", "interactive"]
        await _drain(a, b, interactive, background)


class TestCancellation:
    async def test_cancelled_waiter_leaves_the_queue_without_taking_a_slot(self):
        slots = PrioritySlots(1)
        order: list[str] = []
        running = _Worker(slots, "running", Priority.BACKGROUND, order)
        await _settle()
        gone = _Worker(slots, "gone", Priority.INTERACTIVE, order)
        stays = _Worker(slots, "stays", Priority.BACKGROUND, order)
        await _settle()

        gone.task.cancel()
        await _settle()
        assert slots.waiting() == 1

        running.finish.set()
        await _settle()

        assert order == ["running", "stays"]
        await _drain(stays)
        assert slots.in_use == 0

    async def test_slot_handed_to_a_waiter_cancelled_at_the_same_moment_is_passed_on(self):
        """The timeout of a latency-bound caller can fire in the same loop
        iteration its slot is granted. That slot must not be lost."""
        slots = PrioritySlots(1)
        order: list[str] = []
        await slots.acquire(Priority.BACKGROUND)  # held by the test itself
        doomed = _Worker(slots, "doomed", Priority.INTERACTIVE, order)
        next_up = _Worker(slots, "next", Priority.BACKGROUND, order)
        await _settle()

        # No await between the two: the slot is granted to `doomed`, which
        # is cancelled before it has run.
        slots.release(Priority.BACKGROUND)
        doomed.task.cancel()
        await _settle()

        assert order == ["next"]
        assert slots.in_use == 1
        await _drain(next_up)
        assert slots.in_use == 0

    async def test_timeout_while_waiting_does_not_leak_a_slot(self):
        slots = PrioritySlots(1)
        order: list[str] = []
        running = _Worker(slots, "running", Priority.BACKGROUND, order)
        await _settle()

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slots.acquire(Priority.INTERACTIVE), timeout=0.01)

        assert slots.waiting() == 0
        await _drain(running)
        assert slots.in_use == 0
        await asyncio.wait_for(slots.acquire(Priority.BACKGROUND), timeout=0.1)
        assert slots.in_use == 1


class TestReviewFindings:
    async def test_waiter_cancelled_just_before_a_release_still_raises_cancelled(self):
        """A latency-bound caller times out in the queue, and a slot is
        released before its task wakes. It must see CancelledError (which
        wait_for turns into TimeoutError), not an error from the queue."""
        slots = PrioritySlots(1)
        await slots.acquire(Priority.BACKGROUND)  # held by the test itself
        waiter = asyncio.create_task(slots.acquire(Priority.INTERACTIVE))
        await _settle()

        waiter.cancel()   # cancels its queue entry at once ...
        slots.release(Priority.BACKGROUND)  # ... which the release skips over and drops
        result = (await asyncio.gather(waiter, return_exceptions=True))[0]

        assert isinstance(result, asyncio.CancelledError)
        assert slots.in_use == 0
        assert slots.waiting() == 0


class TestBackgroundIsNotStarved:
    async def test_interactive_callers_leave_one_slot_to_waiting_background_work(self):
        """A steady stream of searches: every slot is held by one, more are
        queued, and so is a chunk summary. The next free slot is the
        summary's."""
        slots = PrioritySlots(3)
        order: list[str] = []
        holders = [_Worker(slots, f"search{i}", Priority.INTERACTIVE, order) for i in range(3)]
        await _settle()
        more = [_Worker(slots, f"search{i}", Priority.INTERACTIVE, order) for i in range(3, 6)]
        summary = _Worker(slots, "summary", Priority.BACKGROUND, order)
        await _settle()

        holders[0].finish.set()
        await _settle()
        assert order[-1] == "summary"

        holders[1].finish.set()
        await _settle()
        assert order[-1] == "search3"  # summaries hold their one slot; searches get the rest

        await _drain(*holders, *more, summary)
        assert slots.in_use == 0

    async def test_interactive_callers_use_every_slot_when_no_background_work_waits(self):
        slots = PrioritySlots(3)
        order: list[str] = []
        searches = [_Worker(slots, f"search{i}", Priority.INTERACTIVE, order) for i in range(3)]
        await _settle()

        assert order == ["search0", "search1", "search2"]
        await _drain(*searches)

    async def test_with_one_slot_interactive_callers_still_go_first(self):
        slots = PrioritySlots(1)
        order: list[str] = []
        first = _Worker(slots, "search0", Priority.INTERACTIVE, order)
        await _settle()
        summary = _Worker(slots, "summary", Priority.BACKGROUND, order)
        second = _Worker(slots, "search1", Priority.INTERACTIVE, order)
        await _settle()

        await _drain(first, second, summary)

        assert order == ["search0", "search1", "summary"]
