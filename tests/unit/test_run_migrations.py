"""`run_migrations()` fails loud and applies each migration atomically.

Constitution V: a migration that fails must abort indexer startup with a
message naming the file, not be logged and swallowed while the indexer
starts on a partial schema. Each migration and its `treeweft_migrations`
row commit in one transaction, so a retry resumes exactly where it stopped.

Mocks only the asyncpg pool, like test_prompt_pin_store.py. No real
Postgres — the same scenario against a real server is
tests/integration/test_migrations_pg.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from treeweft.adapters import postgresql as pg

# ---------------------------------------------------------------------------
# Fake asyncpg pool: one shared "database", transactions that really discard
# their writes on rollback
# ---------------------------------------------------------------------------

class _FakeDb:
    """Committed state, shared by every connection the pool hands out."""

    def __init__(self):
        self.table_exists = False       # treeweft_migrations
        self.recorded: list[str] = []   # treeweft_migrations rows
        self.applied_sql: list[str] = []  # migration scripts that committed
        self.calls: list[tuple] = []
        self.timeouts: dict[str, float | None] = {}  # executed SQL -> timeout
        self.isolations: list[str | None] = []
        self.fail: dict[str, Exception] = {}  # SQL substring -> error to raise
        # SQL substring -> the script ends the runner's transaction, in a way
        # the text of the script does not show (a procedure that commits).
        self.ends_transaction: list[str] = []


class _TxCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.db.calls.append(("BEGIN",))
        self._conn.in_tx = True
        return self._conn

    async def __aexit__(self, *exc):
        conn = self._conn
        committed = exc[0] is None
        conn.db.calls.append(("COMMIT",) if committed else ("ROLLBACK",))
        if committed:
            conn.flush()
        conn.pending_recorded.clear()
        conn.pending_sql.clear()
        conn.in_tx = False
        return False


class _FakeConn:
    def __init__(self, db: _FakeDb):
        self.db = db
        self.in_tx = False
        self.pending_recorded: list[str] = []
        self.pending_sql: list[str] = []

    def flush(self):
        self.db.recorded.extend(self.pending_recorded)
        self.db.applied_sql.extend(self.pending_sql)

    def transaction(self, isolation=None):
        self.db.isolations.append(isolation)
        return _TxCtx(self)

    def is_in_transaction(self) -> bool:
        return self.in_tx

    async def execute(self, query: str, *args, timeout=None):
        q = " ".join(query.split())
        self.db.calls.append(("execute", q, args))
        self.db.timeouts[q] = timeout
        for needle, error in self.db.fail.items():
            if needle in q:
                raise error
        if "pg_advisory_xact_lock" in q:
            return "OK"
        if q.startswith("CREATE TABLE treeweft_migrations"):
            self.db.table_exists = True
            return "OK"
        if q.startswith("INSERT INTO treeweft_migrations"):
            self.pending_recorded.append(args[0])
        else:
            self.pending_sql.append(q)  # a migration script
        if not self.in_tx or any(needle in q for needle in self.db.ends_transaction):
            # Autocommit outside a transaction. Inside one, a script that
            # commits ends the runner's transaction there, work and all —
            # what a real server does.
            self.flush()
            self.pending_recorded.clear()
            self.pending_sql.clear()
            self.in_tx = False
        return "OK"

    async def fetchval(self, query: str, *args):
        self.db.calls.append(("fetchval", " ".join(query.split()), args))
        assert "to_regclass" in query
        return self.db.table_exists

    async def fetch(self, query: str, *args):
        self.db.calls.append(("fetch", " ".join(query.split()), args))
        return [{"name": n} for n in self.db.recorded + self.pending_recorded]

    async def fetchrow(self, query: str, *args):
        self.db.calls.append(("fetchrow", " ".join(query.split()), args))
        if args[0] in self.db.recorded + self.pending_recorded:
            return {"name": args[0]}
        return None


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *_exc):
        return False


class _FakePool:
    def __init__(self, db: _FakeDb):
        self._db = db

    def acquire(self):
        return _AcquireCtx(_FakeConn(self._db))


class _UndefinedTable(Exception):
    """Stands in for asyncpg.exceptions.UndefinedTableError."""


@pytest.fixture
def db(monkeypatch):
    fake = _FakeDb()

    async def get_pool():
        return _FakePool(fake)

    monkeypatch.setattr(pg, "get_pool", get_pool)
    return fake


@pytest.fixture
def migrations_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(pg, "_MIGRATIONS_DIR", tmp_path)
    return tmp_path


def _three_files(directory):
    """The issue's scenario: a good migration, a broken one, one after it."""
    (directory / "001_ok.sql").write_text("CREATE TABLE t_one (id int);")
    (directory / "002_broken.sql").write_text(
        "CREATE TABLE t_two (id int); ALTER TABLE does_not_exist ADD COLUMN x int;"
    )
    (directory / "003_after.sql").write_text("CREATE TABLE t_three (id int);")


def _migration_scripts_run(db: _FakeDb) -> list[str]:
    return [
        c[1] for c in db.calls
        if c[0] == "execute" and c[1].startswith("CREATE TABLE t_")
    ]


@pytest.mark.asyncio
class TestAppliesPending:
    async def test_applies_in_name_order_and_returns_the_names(self, db, migrations_dir):
        (migrations_dir / "002_b.sql").write_text("CREATE TABLE t_b (id int);")
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")
        (migrations_dir / "notes.txt").write_text("not a migration")

        applied = await pg.run_migrations()

        assert applied == ["001_a.sql", "002_b.sql"]
        assert db.recorded == ["001_a.sql", "002_b.sql"]
        assert db.applied_sql == ["CREATE TABLE t_a (id int);", "CREATE TABLE t_b (id int);"]

    async def test_skips_migrations_already_recorded(self, db, migrations_dir):
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")
        (migrations_dir / "002_b.sql").write_text("CREATE TABLE t_b (id int);")
        db.table_exists = True
        db.recorded.append("001_a.sql")

        applied = await pg.run_migrations()

        assert applied == ["002_b.sql"]
        assert db.applied_sql == ["CREATE TABLE t_b (id int);"]

    async def test_second_run_applies_nothing(self, db, migrations_dir):
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        await pg.run_migrations()
        applied = await pg.run_migrations()

        assert applied == []
        assert db.applied_sql == ["CREATE TABLE t_a (id int);"]


@pytest.mark.asyncio
class TestAtomicity:
    async def test_script_and_tracking_row_share_one_transaction(self, db, migrations_dir):
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        await pg.run_migrations()

        kinds = [c[0] if c[0] != "execute" else c[1].split(" (")[0] for c in db.calls]
        script = kinds.index("CREATE TABLE t_a")
        insert = kinds.index("INSERT INTO treeweft_migrations")
        begin = max(i for i, k in enumerate(kinds[:script]) if k == "BEGIN")
        commit = kinds.index("COMMIT", script)
        assert begin < script < insert < commit
        # Nothing closed that transaction between the script and the insert.
        assert "COMMIT" not in kinds[begin:insert]
        assert "ROLLBACK" not in kinds[begin:insert]

    async def test_failed_tracking_insert_rolls_the_migration_back(self, db, migrations_dir):
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")
        db.fail["INSERT INTO treeweft_migrations"] = RuntimeError("disk full")

        with pytest.raises(pg.MigrationError, match="001_a.sql"):
            await pg.run_migrations()

        # Neither half survived, so the next start re-runs it from scratch
        # against a schema that does not already contain it.
        assert db.applied_sql == []
        assert db.recorded == []

    async def test_pending_check_is_repeated_under_the_lock(self, db, migrations_dir):
        """Two indexers starting together: the loser of the lock must see the
        winner's row and skip, not run the migration a second time."""
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        await pg.run_migrations()

        kinds = [c[0] if c[0] != "execute" else c[1] for c in db.calls]
        lock = max(i for i, k in enumerate(kinds) if "pg_advisory_xact_lock" in k)
        recheck = kinds.index("fetchrow", lock)
        script = kinds.index("CREATE TABLE t_a (id int);")
        assert kinds[lock - 1] == "BEGIN"
        assert lock < recheck < script


@pytest.mark.asyncio
class TestFailsLoud:
    async def test_failure_propagates_naming_the_file_and_the_database_error(
        self, db, migrations_dir
    ):
        _three_files(migrations_dir)
        db.fail["does_not_exist"] = _UndefinedTable('relation "does_not_exist" does not exist')

        with pytest.raises(pg.MigrationError) as excinfo:
            await pg.run_migrations()

        message = str(excinfo.value)
        assert "002_broken.sql" in message
        assert 'relation "does_not_exist" does not exist' in message
        assert isinstance(excinfo.value.__cause__, _UndefinedTable)

    async def test_no_later_migration_runs_after_a_failure(self, db, migrations_dir):
        _three_files(migrations_dir)
        db.fail["does_not_exist"] = _UndefinedTable('relation "does_not_exist" does not exist')

        with pytest.raises(pg.MigrationError):
            await pg.run_migrations()

        assert not any("t_three" in sql for sql in _migration_scripts_run(db))
        assert db.recorded == ["001_ok.sql"]
        assert db.applied_sql == ["CREATE TABLE t_one (id int);"]

    async def test_retry_after_the_fix_resumes_where_it_stopped(self, db, migrations_dir):
        _three_files(migrations_dir)
        db.fail["does_not_exist"] = _UndefinedTable('relation "does_not_exist" does not exist')
        with pytest.raises(pg.MigrationError):
            await pg.run_migrations()

        db.fail.clear()
        (migrations_dir / "002_broken.sql").write_text("CREATE TABLE t_two (id int);")
        applied = await pg.run_migrations()

        assert applied == ["002_broken.sql", "003_after.sql"]
        assert db.recorded == ["001_ok.sql", "002_broken.sql", "003_after.sql"]
        assert db.applied_sql.count("CREATE TABLE t_one (id int);") == 1

    async def test_failure_to_create_the_tracking_table_propagates(self, db, migrations_dir):
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")
        db.fail["CREATE TABLE treeweft_migrations"] = RuntimeError(
            "permission denied for schema public"
        )

        with pytest.raises(pg.MigrationError, match="permission denied for schema public"):
            await pg.run_migrations()

        assert db.applied_sql == []

    async def test_script_with_transaction_control_is_rejected_before_it_runs(
        self, db, migrations_dir
    ):
        """A script with its own COMMIT would commit before the tracking row
        is written — the non-atomic state this runner exists to prevent."""
        (migrations_dir / "001_own_txn.sql").write_text(
            "BEGIN; CREATE TABLE t_a (id int); COMMIT;"
        )
        (migrations_dir / "002_after.sql").write_text("CREATE TABLE t_b (id int);")

        with pytest.raises(pg.MigrationError) as excinfo:
            await pg.run_migrations()

        message = str(excinfo.value)
        assert "001_own_txn.sql" in message
        assert "BEGIN" in message and "COMMIT" in message
        assert db.recorded == []
        assert db.applied_sql == []
        assert not any("t_a" in c[1] or "t_b" in c[1] for c in db.calls if c[0] == "execute")

    async def test_script_that_ends_the_transaction_unseen_is_rejected(
        self, db, migrations_dir
    ):
        """The backstop: the script's text shows no transaction control, but
        running it ends the transaction (a procedure that commits)."""
        (migrations_dir / "001_calls_proc.sql").write_text("CALL commits_inside();")
        (migrations_dir / "002_after.sql").write_text("CREATE TABLE t_b (id int);")
        db.ends_transaction.append("commits_inside")

        with pytest.raises(pg.MigrationError, match="001_calls_proc.sql"):
            await pg.run_migrations()

        assert db.recorded == []
        assert not any("t_b" in c[1] for c in db.calls if c[0] == "execute")

    async def test_timeout_message_says_what_timed_out(self, db, migrations_dir):
        """str(TimeoutError()) is empty; the message must still name a cause."""
        (migrations_dir / "001_slow.sql").write_text("CREATE TABLE t_a (id int);")
        db.fail["CREATE TABLE t_a"] = TimeoutError()

        with pytest.raises(pg.MigrationError) as excinfo:
            await pg.run_migrations()

        message = str(excinfo.value)
        assert "001_slow.sql" in message
        assert "TimeoutError" in message
        assert "time limit" in message


@pytest.mark.asyncio
class TestTrackingTable:
    async def test_is_not_created_again_when_it_exists(self, db, migrations_dir):
        """CREATE TABLE IF NOT EXISTS needs CREATE on the schema even when
        the table exists; a role with read/write rights only must still be
        able to start when nothing is pending."""
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")
        db.table_exists = True
        db.recorded.append("001_a.sql")
        db.fail["CREATE TABLE"] = RuntimeError("permission denied for schema public")

        assert await pg.run_migrations() == []

    async def test_is_created_when_missing(self, db, migrations_dir):
        await pg.run_migrations()

        assert db.table_exists is True


@pytest.mark.asyncio
class TestLimits:
    async def test_script_and_lock_wait_do_not_use_the_pool_timeout(self, db, migrations_dir):
        """The pool's 10s command_timeout would stop startup on every attempt
        for a migration that needs longer, and for the process waiting on it."""
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        await pg.run_migrations()

        lock = next(q for q in db.timeouts if "pg_advisory_xact_lock" in q)
        assert db.timeouts["CREATE TABLE t_a (id int);"] == pg._MIGRATION_TIMEOUT_SECONDS
        assert db.timeouts[lock] == pg._MIGRATION_TIMEOUT_SECONDS
        assert pg._MIGRATION_TIMEOUT_SECONDS >= 600

    async def test_every_transaction_is_read_committed(self, db, migrations_dir):
        """Under REPEATABLE READ the re-check after the lock would not see
        the row its previous holder committed."""
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        await pg.run_migrations()

        assert db.isolations
        assert set(db.isolations) == {"read_committed"}


@pytest.mark.asyncio
class TestPoolUnavailable:
    async def test_returns_empty_and_logs_that_migrations_were_skipped(
        self, monkeypatch, migrations_dir, caplog
    ):
        async def no_pool():
            return None

        monkeypatch.setattr(pg, "get_pool", no_pool)
        (migrations_dir / "001_a.sql").write_text("CREATE TABLE t_a (id int);")

        with caplog.at_level(logging.WARNING, logger=pg.__name__):
            applied = await pg.run_migrations()

        assert applied == []
        assert any("migrations" in r.getMessage().lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Transaction control in a script, and the shipped migration files
# ---------------------------------------------------------------------------

class TestTransactionControlDetection:
    @pytest.mark.parametrize(
        "sql",
        [
            "BEGIN;\nCREATE TABLE t (id int);\nCOMMIT;",
            "BEGIN; CREATE TABLE t (id int); COMMIT;",
            "begin transaction; create table t (id int); commit transaction;",
            "START TRANSACTION; CREATE TABLE t (id int); END TRANSACTION;",
            "BEGIN ISOLATION LEVEL SERIALIZABLE; CREATE TABLE t (id int); END;",
            "CREATE TABLE t (id int); COMMIT; BEGIN; CREATE TABLE u (id int);",
            "CREATE TABLE t (id int);\nROLLBACK;",
            "CREATE TABLE t (id int); ABORT",
        ],
    )
    def test_detects(self, sql):
        assert pg._transaction_control(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "CREATE TABLE t (id int);",
            "-- COMMIT; is not allowed here, and don't add BEGIN;\nCREATE TABLE t (id int);",
            "/* BEGIN;\nCOMMIT; */ CREATE TABLE t (id int);",
            "INSERT INTO t (note) VALUES ('then run COMMIT; by hand');",
            "INSERT INTO t (note) VALUES ('it''s; COMMIT;');",
            "CREATE TEMPORARY TABLE tmp ON COMMIT DROP AS SELECT 1;",
            "DO $$\nBEGIN\n  UPDATE t SET id = 1;\nEND;\n$$;",
            "CREATE FUNCTION f() RETURNS int AS $body$\nBEGIN\n  RETURN 1;\nEND;\n$body$ LANGUAGE plpgsql;",
            'CREATE TABLE "commit" (id int);',
            "UPDATE t SET ended = true; -- END;",
        ],
    )
    def test_ignores(self, sql):
        assert pg._transaction_control(sql) == []


_SHIPPED = sorted((Path(pg.__file__).resolve().parent / "migrations").glob("*.sql"))


class TestShippedMigrations:
    def test_there_are_shipped_migrations_to_check(self):
        assert len(_SHIPPED) >= 22

    @pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: p.name)
    def test_no_migration_controls_its_own_transaction(self, path):
        """The runner owns the transaction, and refuses to run a script that
        has its own — which would stop every fresh install at startup."""
        found = pg._transaction_control(path.read_text())
        assert not found, (
            f"{path.name} contains transaction control {found}; remove it — "
            "run_migrations() wraps every migration in a transaction"
        )
