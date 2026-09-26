"""PromptPinStore (ADR-003, migration 021) and the SourceRecord summary-state
methods on PostgreSourceRepository.

Mocks only the asyncpg pool, like test_source_repository.py and
test_summary_rejection_cache.py:163-186. No real Postgres.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fake asyncpg pool with transaction ordering tracked in one shared call log
# ---------------------------------------------------------------------------

class _TxCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.calls.append(("BEGIN", None, None))
        return self._conn

    async def __aexit__(self, *exc):
        self._conn.calls.append(("COMMIT" if exc[0] is None else "ROLLBACK", None, None))
        return False


class _FakeConn:
    def __init__(self, rows=None, fetchrow_result=None, fetchval_results=None):
        # rows: list[dict] backing the "table"
        self.rows = rows if rows is not None else []
        self.calls: list[tuple] = []
        self._fetchrow_result = fetchrow_result

    def transaction(self):
        return _TxCtx(self)

    async def execute(self, query: str, *args):
        self.calls.append(("execute", " ".join(query.split()), args))
        q = " ".join(query.split())
        if q.startswith("NOTIFY"):
            return "NOTIFY"
        if q.startswith("INSERT INTO prompt_pins") and "ON CONFLICT (operation, scope) DO UPDATE" in q:
            operation, scope, version, updated_by = args
            existing = next(
                (r for r in self.rows if r["operation"] == operation and r["scope"] == scope), None
            )
            if existing is None:
                self.rows.append(
                    {
                        "operation": operation,
                        "scope": scope,
                        "version": version,
                        "updated_at": datetime.now(timezone.utc),
                        "updated_by": updated_by,
                    }
                )
            else:
                existing["version"] = version
                existing["updated_at"] = datetime.now(timezone.utc)
                existing["updated_by"] = updated_by
            return "INSERT 0 1"
        if q.startswith("INSERT INTO prompt_pins") and "ON CONFLICT DO NOTHING" in q:
            operation, version = args
            existing = next(
                (r for r in self.rows if r["operation"] == operation and r["scope"] == "deployment"),
                None,
            )
            if existing is not None:
                return "INSERT 0 0"
            self.rows.append(
                {
                    "operation": operation,
                    "scope": "deployment",
                    "version": version,
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": None,
                }
            )
            return "INSERT 0 1"
        if q.startswith("DELETE FROM prompt_pins WHERE operation"):
            operation, scope = args
            before = len(self.rows)
            self.rows[:] = [
                r for r in self.rows if not (r["operation"] == operation and r["scope"] == scope)
            ]
            return f"DELETE {before - len(self.rows)}"
        if q.startswith("DELETE FROM prompt_pins WHERE scope"):
            (source_id,) = args
            before = len(self.rows)
            self.rows[:] = [
                r for r in self.rows if not (r["scope"] == source_id and r["scope"] != "deployment")
            ]
            return f"DELETE {before - len(self.rows)}"
        if q.startswith("UPDATE source_records SET summary_refresh_target"):
            self._last_update = args
            return "UPDATE 1"
        if q.startswith("UPDATE source_records"):
            self._last_update = args
            return "UPDATE 1"
        if q.startswith("INSERT INTO source_records"):
            return "OK"
        raise AssertionError(f"unexpected execute: {q}")

    async def fetchrow(self, query: str, *args):
        self.calls.append(("fetchrow", " ".join(query.split()), args))
        q = " ".join(query.split())
        if "INSERT INTO prompt_pins" in q and "RETURNING" in q:
            operation, scope, version, updated_by = args
            row = next(
                (r for r in self.rows if r["operation"] == operation and r["scope"] == scope), None
            )
            if row is None:
                row = {
                    "operation": operation,
                    "scope": scope,
                    "version": version,
                    "updated_at": datetime.now(timezone.utc),
                    "updated_by": updated_by,
                }
                self.rows.append(row)
            else:
                row["version"] = version
                row["updated_at"] = datetime.now(timezone.utc)
                row["updated_by"] = updated_by
            return dict(row)
        return self._fetchrow_result

    async def fetch(self, query: str, *args):
        self.calls.append(("fetch", " ".join(query.split()), args))
        q = " ".join(query.split())
        if "FROM prompt_pins" in q:
            return [dict(r) for r in self.rows]
        if "FROM source_records" in q and "id = ANY" in q:
            (ids,) = args
            return [
                {"id": r["id"], "summary_prompt_version": r.get("summary_prompt_version")}
                for r in self.rows
                if r.get("id") in ids
            ]
        if "summary_prompt_version" in q and "GROUP BY" in q:
            hist: dict[int, int] = {}
            for r in self.rows:
                v = r.get("summary_prompt_version")
                if v is not None:
                    hist[v] = hist.get(v, 0) + 1
            return [{"version": v, "n": n} for v, n in hist.items()]
        return []


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, conn: _FakeConn):
        self.conn = conn

    def acquire(self):
        return _AcquireCtx(self.conn)


@pytest.fixture
def conn():
    return _FakeConn()


@pytest.fixture
def patched_store(monkeypatch, conn):
    from treeweft.adapters.postgresql import prompt_pin_store as store_mod

    pool = _FakePool(conn)

    async def _get_pool():
        return pool

    monkeypatch.setattr(store_mod, "get_pool", _get_pool)
    return store_mod.PromptPinStore()


# ---------------------------------------------------------------------------
# upsert / delete: NOTIFY inside the same transaction as the write
# ---------------------------------------------------------------------------

class TestUpsertNotifyOrdering:
    async def test_upsert_notifies_inside_same_transaction_block(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "deployment", 4, "admin")

        begin_idx = next(i for i, c in enumerate(conn.calls) if c[0] == "BEGIN")
        commit_idx = next(i for i, c in enumerate(conn.calls) if c[0] in ("COMMIT", "ROLLBACK"))
        notify_idx = next(
            i for i, c in enumerate(conn.calls) if c[0] == "execute" and c[1].startswith("NOTIFY")
        )
        write_idx = next(
            i for i, c in enumerate(conn.calls) if c[0] == "fetchrow" and "INSERT INTO prompt_pins" in c[1]
        )

        assert begin_idx < write_idx < notify_idx < commit_idx
        assert conn.calls[notify_idx][1] == "NOTIFY prompt_pins_changed"

    async def test_upsert_uses_on_conflict_do_update(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "deployment", 3, None)
        await patched_store.upsert("chunk_summary", "deployment", 4, "admin")

        rows = [r for r in conn.rows if r["operation"] == "chunk_summary" and r["scope"] == "deployment"]
        assert len(rows) == 1
        assert rows[0]["version"] == 4
        assert rows[0]["updated_by"] == "admin"


class TestDeleteNotifyOrdering:
    async def test_delete_notifies_inside_same_transaction_block(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "src1", 4, "admin")
        conn.calls.clear()

        deleted = await patched_store.delete("chunk_summary", "src1")

        assert deleted is True
        begin_idx = next(i for i, c in enumerate(conn.calls) if c[0] == "BEGIN")
        commit_idx = next(i for i, c in enumerate(conn.calls) if c[0] in ("COMMIT", "ROLLBACK"))
        notify_idx = next(
            i for i, c in enumerate(conn.calls) if c[0] == "execute" and c[1].startswith("NOTIFY")
        )
        write_idx = next(
            i
            for i, c in enumerate(conn.calls)
            if c[0] == "execute" and c[1].startswith("DELETE FROM prompt_pins WHERE operation")
        )
        assert begin_idx < write_idx < notify_idx < commit_idx

    async def test_delete_of_missing_row_returns_false_and_still_in_transaction(
        self, patched_store, conn
    ):
        deleted = await patched_store.delete("chunk_summary", "does-not-exist")
        assert deleted is False
        assert any(c[0] == "BEGIN" for c in conn.calls)


# ---------------------------------------------------------------------------
# seed_if_absent
# ---------------------------------------------------------------------------

class TestSeedIfAbsent:
    async def test_seed_if_absent_uses_on_conflict_do_nothing(self, patched_store, conn):
        await patched_store.seed_if_absent("chunk_summary", 3)
        insert_call = next(
            c for c in conn.calls if c[0] == "execute" and c[1].startswith("INSERT INTO prompt_pins")
        )
        assert "ON CONFLICT DO NOTHING" in insert_call[1]
        assert "'deployment'" in insert_call[1] or insert_call[2] == ("chunk_summary", 3)

    async def test_seed_if_absent_notifies_only_when_a_row_was_inserted(self, patched_store, conn):
        await patched_store.seed_if_absent("chunk_summary", 3)
        assert any(c[0] == "execute" and c[1].startswith("NOTIFY") for c in conn.calls)

        conn.calls.clear()
        await patched_store.seed_if_absent("chunk_summary", 4)  # already present
        assert not any(c[0] == "execute" and c[1].startswith("NOTIFY") for c in conn.calls)
        # unchanged: still v3
        row = next(r for r in conn.rows if r["operation"] == "chunk_summary")
        assert row["version"] == 3

    async def test_seed_if_absent_writes_deployment_scope_and_null_updated_by(self, patched_store, conn):
        await patched_store.seed_if_absent("hyde", 1)
        row = next(r for r in conn.rows if r["operation"] == "hyde")
        assert row["scope"] == "deployment"
        assert row["updated_by"] is None


# ---------------------------------------------------------------------------
# delete_overrides_for_source
# ---------------------------------------------------------------------------

class TestDeleteOverridesForSource:
    async def test_deletes_only_rows_scoped_to_that_source(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "deployment", 3, None)
        await patched_store.upsert("chunk_summary", "src-1", 4, "admin")
        await patched_store.upsert("chunk_summary", "src-2", 5, "admin")

        await patched_store.delete_overrides_for_source("src-1")

        remaining_scopes = {r["scope"] for r in conn.rows}
        assert remaining_scopes == {"deployment", "src-2"}

    async def test_never_deletes_the_deployment_scope(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "deployment", 3, None)

        await patched_store.delete_overrides_for_source("deployment")

        assert any(r["scope"] == "deployment" for r in conn.rows)

    async def test_notifies_in_the_same_transaction(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "src-1", 4, "admin")
        conn.calls.clear()

        await patched_store.delete_overrides_for_source("src-1")

        begin_idx = next(i for i, c in enumerate(conn.calls) if c[0] == "BEGIN")
        commit_idx = next(i for i, c in enumerate(conn.calls) if c[0] in ("COMMIT", "ROLLBACK"))
        notify_idx = next(
            i for i, c in enumerate(conn.calls) if c[0] == "execute" and c[1].startswith("NOTIFY")
        )
        assert begin_idx < notify_idx < commit_idx


# ---------------------------------------------------------------------------
# list_all
# ---------------------------------------------------------------------------

class TestListAll:
    async def test_list_all_returns_pin_rows(self, patched_store, conn):
        await patched_store.upsert("chunk_summary", "deployment", 3, None)
        rows = await patched_store.list_all()
        assert len(rows) == 1
        assert rows[0].operation == "chunk_summary"
        assert rows[0].scope == "deployment"
        assert rows[0].version == 3
        d = rows[0].to_dict()
        assert d["operation"] == "chunk_summary"
        assert d["version"] == 3
        assert "updated_at" in d


class TestNoPool:
    async def test_all_methods_raise_without_pool(self, monkeypatch):
        from treeweft.adapters.postgresql import prompt_pin_store as store_mod

        async def _get_pool():
            return None

        monkeypatch.setattr(store_mod, "get_pool", _get_pool)
        store = store_mod.PromptPinStore()

        with pytest.raises(RuntimeError):
            await store.list_all()
        with pytest.raises(RuntimeError):
            await store.upsert("chunk_summary", "deployment", 3, None)
        with pytest.raises(RuntimeError):
            await store.delete("chunk_summary", "deployment")
        with pytest.raises(RuntimeError):
            await store.seed_if_absent("chunk_summary", 3)
        with pytest.raises(RuntimeError):
            await store.delete_overrides_for_source("src-1")


# ---------------------------------------------------------------------------
# PostgreSourceRepository summary-state methods
# ---------------------------------------------------------------------------

def _source_conn():
    return _FakeConn()


def _source_pool(conn):
    return _FakePool(conn)


class TestMarkSummaryRefresh:
    async def test_sets_only_summary_refresh_target(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        await repo.mark_summary_refresh("src-1", 4)

        call = next(
            c
            for c in conn.calls
            if c[0] == "execute" and c[1].startswith("UPDATE source_records SET summary_refresh_target")
        )
        assert "summary_prompt_version" not in call[1]
        assert call[2] == ("src-1", 4)

    async def test_database_error_propagates(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()

        async def _boom(*_a, **_k):
            raise ConnectionError("db down")

        conn.execute = _boom
        repo = PostgreSourceRepository(pool=_source_pool(conn))
        with pytest.raises(ConnectionError):
            await repo.mark_summary_refresh("src-1", 4)


class TestRecordSummaryVersion:
    async def test_single_update_sets_version_and_clears_target(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        await repo.record_summary_version("src-1", 4)

        update_calls = [
            c for c in conn.calls if c[0] == "execute" and c[1].startswith("UPDATE source_records")
        ]
        assert len(update_calls) == 1
        query, args = update_calls[0][1], update_calls[0][2]
        assert "summary_prompt_version" in query
        assert "summary_refresh_target" in query
        assert "= NULL" in query
        assert args == ("src-1", 4)

    async def test_accepts_none_to_clear_the_recorded_version(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        await repo.record_summary_version("src-1", None)

        update_calls = [
            c for c in conn.calls if c[0] == "execute" and c[1].startswith("UPDATE source_records")
        ]
        assert update_calls[0][2] == ("src-1", None)


class TestSummaryVersionHistogram:
    async def test_groups_non_null_versions(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        conn.rows = [
            {"summary_prompt_version": 3},
            {"summary_prompt_version": 3},
            {"summary_prompt_version": 4},
            {"summary_prompt_version": None},
        ]
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        histogram = await repo.summary_version_histogram()

        assert histogram == {3: 2, 4: 1}

    async def test_empty_table_gives_empty_histogram(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        assert await repo.summary_version_histogram() == {}


class TestSaveDoesNotWriteNewColumns:
    async def test_save_sql_contains_neither_new_column(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository
        from treeweft.domain.sources import SourceRecord

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        record = SourceRecord(
            id="src-1",
            path="/tmp/x",
            indexed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        await repo.save(record)

        insert_call = next(c for c in conn.calls if c[0] == "execute" and "INSERT INTO source_records" in c[1])
        assert "summary_prompt_version" not in insert_call[1]
        assert "summary_refresh_target" not in insert_call[1]


class TestSummaryVersionsFor:
    """Batch lookup replacing one get_by_id per distinct source in the
    summary_tail read path (retrieval._recorded_versions)."""

    async def test_one_query_returns_dict_keyed_by_id(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        conn.rows = [
            {"id": "src-1", "summary_prompt_version": 5},
            {"id": "src-2", "summary_prompt_version": None},
            {"id": "src-3", "summary_prompt_version": 7},
        ]
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        result = await repo.summary_versions_for(["src-1", "src-2", "src-9"])

        assert result == {"src-1": 5, "src-2": None}
        fetch_calls = [c for c in conn.calls if c[0] == "fetch"]
        assert len(fetch_calls) == 1
        assert "id = ANY" in fetch_calls[0][1]

    async def test_empty_input_returns_empty_without_querying(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()
        pool = _source_pool(conn)
        repo = PostgreSourceRepository(pool=pool)

        assert await repo.summary_versions_for([]) == {}
        assert conn.calls == []

    async def test_no_pool_returns_empty_dict(self, monkeypatch):
        from treeweft.adapters.sources import repository as repo_mod

        async def _get_pool():
            return None

        monkeypatch.setattr(repo_mod, "get_pool", _get_pool)
        repo = repo_mod.PostgreSourceRepository()

        assert await repo.summary_versions_for(["src-1"]) == {}

    async def test_database_error_is_logged_not_raised(self):
        from treeweft.adapters.sources.repository import PostgreSourceRepository

        conn = _source_conn()

        async def _boom(*_a, **_k):
            raise ConnectionError("db down")

        conn.fetch = _boom
        repo = PostgreSourceRepository(pool=_source_pool(conn))

        assert await repo.summary_versions_for(["src-1"]) == {}
