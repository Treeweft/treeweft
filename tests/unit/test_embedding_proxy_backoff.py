"""Embedding proxy back-off under load.

A CPU TEI server works through one batch at a time. When the indexer sends it
more concurrent /embed requests than it can answer within the HTTP timeout,
the queued ones time out; each timeout used to count toward the circuit
breaker, so a busy-but-healthy backend was tripped OPEN and every later embed
failed ("All embedding backends are unavailable"). The proxy now keeps a
per-backend concurrency limit: requests over it wait in the proxy (where the
HTTP timeout does not run), the limit halves on a timeout, a 429 or a slow
answer, and grows again while answers are fast.

The fake server below reproduces that shape: one worker, a fixed service time
per batch, and work that keeps running after the client gives up (as TEI's
does).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from treeweft.adapters.tei.embedding_proxy import (
    Backend,
    BackendClass,
    CircuitState,
    EmbeddingProxy,
)


class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=None, response=None  # type: ignore[arg-type]
            )


class _SingleWorkerServer:
    """One worker, `service` seconds per batch, client read timeout `timeout`."""

    def __init__(self, *, service: float, timeout: float):
        self.service = service
        self.timeout = httpx.Timeout(timeout)
        self._worker = asyncio.Lock()
        self.active = 0
        self.max_active = 0
        self.timeouts = 0

    async def _serve(self, n: int):
        async with self._worker:
            await asyncio.sleep(self.service)
        return [[0.1, 0.2] for _ in range(n)]

    async def post(self, url, json=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            work = asyncio.ensure_future(self._serve(len(json["inputs"])))
            try:
                payload = await asyncio.wait_for(asyncio.shield(work), self.timeout.read)
            except TimeoutError:
                self.timeouts += 1
                raise httpx.ReadTimeout("timed out")
            return _Resp(200, payload)
        finally:
            self.active -= 1


def _proxy(client, **kw) -> EmbeddingProxy:
    return EmbeddingProxy(
        [Backend(url="http://cpu", klass=BackendClass.CPU)], client=client, **kw
    )


@pytest.mark.asyncio
async def test_saturated_backend_answers_every_batch_instead_of_tripping():
    # 24 concurrent batches at 20 ms each is 480 ms of work; the timeout is
    # 100 ms, so sending them all at once times out most of them.
    server = _SingleWorkerServer(service=0.02, timeout=0.1)
    proxy = _proxy(server)
    backend = proxy._backends[0]

    results = await asyncio.gather(*(proxy.embed([f"chunk {i}"]) for i in range(24)))

    assert all(r == [[0.1, 0.2]] for r in results)
    assert backend.cb_state is CircuitState.CLOSED
    assert backend.failures == 0


@pytest.mark.asyncio
async def test_limit_shrinks_under_load_and_requests_wait_in_the_proxy():
    server = _SingleWorkerServer(service=0.02, timeout=0.1)
    # 16 at once is 320 ms of work against a 100 ms timeout: most time out,
    # and the server keeps working on them after they do, so the re-sent
    # ones time out too until that backlog is gone.
    proxy = _proxy(server, initial_concurrency=16)
    backend = proxy._backends[0]

    await asyncio.gather(*(proxy.embed([f"chunk {i}"]) for i in range(48)))

    # Slow answers (over half the timeout) pull the limit down to what one
    # worker answers in time: at most 100 ms / 20 ms = 5 in flight.
    assert backend.concurrency_limit <= 5
    assert backend.failures == 0


@pytest.mark.asyncio
async def test_limit_grows_while_answers_are_fast():
    server = _SingleWorkerServer(service=0.0, timeout=1.0)
    proxy = _proxy(server, initial_concurrency=2, max_concurrency=8)
    backend = proxy._backends[0]

    await asyncio.gather(*(proxy.embed([f"chunk {i}"]) for i in range(64)))

    assert backend.concurrency_limit == 8
    assert server.max_active <= 8


@pytest.mark.asyncio
async def test_limit_does_not_grow_while_idle():
    # Serial calls never fill the limit, so it has not been shown to be too
    # small and must not creep up toward the maximum.
    server = _SingleWorkerServer(service=0.0, timeout=1.0)
    proxy = _proxy(server, initial_concurrency=2, max_concurrency=32)
    backend = proxy._backends[0]

    for i in range(20):
        await proxy.embed([f"chunk {i}"])

    assert backend.concurrency_limit == 2


@pytest.mark.asyncio
async def test_timeout_alone_on_a_backend_that_never_answered_opens_the_breaker():
    # Nothing else in flight, no answer from the backend within the last two
    # read timeouts, and timing out alone past the grace: it is not
    # overloaded, it is not answering. Open the breaker so queued batches
    # stop waiting on it.
    server = _SingleWorkerServer(service=0.2, timeout=0.05)
    proxy = _proxy(server)
    backend = proxy._backends[0]

    with pytest.raises(httpx.ReadTimeout):
        await proxy.embed(["chunk"])

    assert backend.failures == 1
    assert backend.cb_state is CircuitState.OPEN


class _SharingServer:
    """Works on every request at once, each at 1/n speed (TEI batching
    concurrent requests, or a CPU shared by them). Abandoned work still runs."""

    def __init__(self, *, service: float, timeout: float):
        self.service = service
        self.timeout = httpx.Timeout(timeout)
        self._jobs: dict = {}
        self._ticker = None

    async def _tick(self):
        # Progress follows the clock, not the number of ticks, so a slow
        # (loaded) event loop does not make the server itself slower.
        loop = asyncio.get_running_loop()
        last = loop.time()
        while True:
            await asyncio.sleep(0.001)
            now = loop.time()
            step, last = now - last, now
            if self._jobs:
                share = step / len(self._jobs)
                for fut, left in list(self._jobs.items()):
                    left -= share
                    if left <= 0:
                        del self._jobs[fut]
                        if not fut.done():
                            fut.set_result(None)
                    else:
                        self._jobs[fut] = left

    async def post(self, url, json=None):
        if self._ticker is None:
            self._ticker = asyncio.ensure_future(self._tick())
        fut = asyncio.get_running_loop().create_future()
        self._jobs[fut] = self.service
        try:
            await asyncio.wait_for(asyncio.shield(fut), self.timeout.read)
        except TimeoutError:
            raise httpx.ReadTimeout("timed out") from None
        return _Resp(200, [[0.1, 0.2] for _ in json["inputs"]])

    def close(self):
        if self._ticker is not None:
            self._ticker.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [0.03, 0.05])
async def test_first_request_of_a_burst_is_not_treated_as_sent_alone(service):
    # The first request of a burst is sent before the others, so at send
    # time it looks alone. On a server that shares its capacity, the ones
    # sent after it slow it past the timeout: that is load, not failure.
    server = _SharingServer(service=service, timeout=0.1)
    proxy = _proxy(server)
    backend = proxy._backends[0]
    try:
        results = await asyncio.gather(
            *(proxy.embed([f"chunk {i}"]) for i in range(24)), return_exceptions=True
        )
    finally:
        server.close()

    assert [r for r in results if isinstance(r, Exception)] == []
    assert backend.failures == 0


class _Hung:
    """Accepts every request and never answers."""

    def __init__(self, timeout: float):
        self.timeout = httpx.Timeout(timeout)
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        await asyncio.sleep(self.timeout.read)
        raise httpx.ReadTimeout("timed out")


@pytest.mark.asyncio
async def test_hung_backend_fails_every_batch_within_a_few_timeouts():
    # Before the back-off, a hung backend failed every batch after one
    # timeout. Back-off must not turn that into hours of retries: the limit
    # falls to 1, the lone request that then times out opens the breaker,
    # and every queued batch fails at once instead of being sent.
    server = _Hung(timeout=0.05)
    proxy = _proxy(server)
    backend = proxy._backends[0]

    loop = asyncio.get_running_loop()
    started = loop.time()
    results = await asyncio.gather(
        *(proxy.embed([f"chunk {i}"]) for i in range(24)), return_exceptions=True
    )
    elapsed = loop.time() - started

    assert all(isinstance(r, Exception) for r in results)
    assert backend.cb_state is CircuitState.OPEN
    assert elapsed < 10 * 0.05
    assert server.calls < 24


class _ConnectTimeout:
    timeout = httpx.Timeout(1.0)

    async def post(self, url, json=None):
        raise httpx.ConnectTimeout("no route to host")


@pytest.mark.asyncio
async def test_connect_timeout_is_not_load():
    proxy = _proxy(_ConnectTimeout())
    backend = proxy._backends[0]

    results = await asyncio.gather(
        *(proxy.embed([f"chunk {i}"]) for i in range(3)), return_exceptions=True
    )

    assert all(isinstance(r, httpx.ConnectTimeout) for r in results)
    assert backend.failures == 3
    assert backend.concurrency_limit == 4


@pytest.mark.asyncio
async def test_cancelled_callers_leave_no_load_behind():
    # index_guard wraps embeds in asyncio.wait_for; a cancelled caller, in
    # the queue or mid-request, must not leave its cost or slot counted.
    server = _SingleWorkerServer(service=0.02, timeout=1.0)
    proxy = _proxy(server, initial_concurrency=2)
    backend = proxy._backends[0]

    tasks = [asyncio.ensure_future(proxy.embed([f"chunk {i}"])) for i in range(10)]
    await asyncio.sleep(0.03)
    for t in tasks[::2]:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    assert backend.in_flight_tokens == 0
    assert backend.active == 0
    assert await proxy.embed(["after"]) == [[0.1, 0.2]]


class _OverloadedThenOk:
    """429 for the first `n` requests (TEI's 'model is overloaded'), then 200."""

    def __init__(self, n: int):
        self.remaining = n
        self.calls = 0

    async def post(self, url, json=None):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            return _Resp(429)
        return _Resp(200, [[0.1, 0.2] for _ in json["inputs"]])


@pytest.mark.asyncio
async def test_429_backs_off_and_retries_on_the_same_backend():
    client = _OverloadedThenOk(2)
    proxy = _proxy(client, initial_concurrency=4)
    backend = proxy._backends[0]

    assert await proxy.embed(["chunk"]) == [[0.1, 0.2]]

    assert client.calls == 3
    assert backend.failures == 0
    assert backend.concurrency_limit < 4


class _Status:
    def __init__(self, status: int):
        self.status = status

    async def post(self, url, json=None):
        return _Resp(self.status, [[0.0]])


@pytest.mark.asyncio
async def test_server_errors_still_count_toward_the_breaker():
    proxy = _proxy(_Status(503))
    backend = proxy._backends[0]

    with pytest.raises(httpx.HTTPStatusError):
        await proxy.embed(["chunk"])

    assert backend.failures == 1
