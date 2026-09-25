"""`lifecycle.startup()`'s statement order (ADR-004 §3, research R2).

Running the real `startup()` end to end needs a live Postgres, migrations,
and an embedding backend — too much to fake meaningfully. Instead this
parses the function body and asserts the ORDER the load-bearing statements
appear in, the same structural-testing approach test_route_table.py uses
for a property that is hard to observe by calling the code.

The invariant: the index-schema check runs after both job stores are
initialised, before the queue starts, and before any job is (re-)enqueued —
so no worker in this process can pick up work before the status is known.
"""
import ast
import inspect

from treeweft.application import lifecycle


def _startup_body() -> list[ast.stmt]:
    source = inspect.getsource(lifecycle.startup)
    tree = ast.parse(source)
    func = tree.body[0]
    assert isinstance(func, ast.AsyncFunctionDef)
    return func.body


def _flatten(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """Every statement in the function, including inside if/for/try blocks,
    in source order (depth-first, pre-order) — startup() has several."""
    out: list[ast.stmt] = []
    for stmt in stmts:
        out.append(stmt)
        for field in ("body", "orelse", "finalbody", "handlers"):
            child = getattr(stmt, field, None)
            if not child:
                continue
            for item in child:
                if isinstance(item, ast.excepthandler):
                    out.extend(_flatten(item.body))
                elif isinstance(item, ast.stmt):
                    out.extend(_flatten([item]))
    return out


def _first_index_containing(stmts: list[ast.stmt], needle: str) -> int:
    for i, stmt in enumerate(stmts):
        if needle in ast.unparse(stmt):
            return i
    raise AssertionError(f"no statement containing {needle!r} in startup()")


class TestOrder:
    def test_check_runs_after_both_job_stores_are_initialised(self):
        stmts = _flatten(_startup_body())
        job_store_init = _first_index_containing(stmts, "_job_store.init()")
        job_group_store_init = _first_index_containing(stmts, "_job_group_store.init()")
        check = _first_index_containing(stmts, "index_guard.run_check()")
        assert job_store_init < check
        assert job_group_store_init < check

    def test_check_runs_before_the_queue_starts(self):
        stmts = _flatten(_startup_body())
        check = _first_index_containing(stmts, "index_guard.run_check()")
        queue_start = _first_index_containing(stmts, "_job_queue.start()")
        assert check < queue_start

    def test_check_runs_before_any_job_is_recovered(self):
        stmts = _flatten(_startup_body())
        check = _first_index_containing(stmts, "index_guard.run_check()")
        recovery_enqueue = _first_index_containing(stmts, "_job_queue.enqueue(job.id)")
        assert check < recovery_enqueue

    def test_refresh_loop_starts_last(self):
        stmts = _flatten(_startup_body())
        check = _first_index_containing(stmts, "index_guard.run_check()")
        loop_start = _first_index_containing(stmts, "index_guard.start_refresh_loop()")
        queue_start = _first_index_containing(stmts, "_job_queue.start()")
        assert check < loop_start
        assert queue_start < loop_start


class TestShutdownStopsTheLoop:
    def test_shutdown_calls_stop_refresh_loop(self):
        source = inspect.getsource(lifecycle.shutdown)
        assert "index_guard.stop_refresh_loop()" in source
