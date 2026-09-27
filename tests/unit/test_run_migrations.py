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
import re
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
        self.recorded: list[str] = []   # treeweft_migrations rows
        self.applied_sql: list[str] = []  # migration scripts that committed
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}  # SQL substring -> error to raise


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

    def transaction(self):
        return _TxCtx(self)

    def is_in_transaction(self) -> bool:
        return self.in_tx

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        self.db.calls.append(("execute", q, args))
        for needle, error in self.db.fail.items():
            if needle in q:
                raise error
        if "pg_advisory_xact_lock" in q or q.startswith("CREATE TABLE IF NOT EXISTS treeweft_migrations"):
            return "OK"
        if q.startswith("INSERT INTO treeweft_migrations"):
            self.pending_recorded.append(args[0])
        else:
            self.pending_sql.append(q)  # a migration script
        if not self.in_tx or re.search(r"\bCOMMIT\b", q):
            # Autocommit outside a transaction. Inside one, a script carrying
            # its own COMMIT ends the runner's transaction there, work and
            # all — what a real server does.
            self.flush()
            self.pending_recorded.clear()
            self.pending_sql.clear()
            self.in_tx = False
        return "OK"

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
        db.fail["CREATE TABLE IF NOT EXISTS treeweft_migrations"] = RuntimeError(
            "permission denied for schema public"
        )

        with pytest.raises(pg.MigrationError, match="permission denied for schema public"):
            await pg.run_migrations()

        assert db.applied_sql == []

    async def test_script_that_ends_the_transaction_is_rejected(self, db, migrations_dir):
        """A script with its own COMMIT commits before the tracking row is
        written — the non-atomic state this runner exists to prevent."""
        (migrations_dir / "001_own_txn.sql").write_text(
            "BEGIN; CREATE TABLE t_a (id int); COMMIT;"
        )
        (migrations_dir / "002_after.sql").write_text("CREATE TABLE t_b (id int);")

        with pytest.raises(pg.MigrationError, match="001_own_txn.sql"):
            await pg.run_migrations()

        assert db.recorded == []
        assert not any("t_b" in c[1] for c in db.calls if c[0] == "execute")


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
# The shipped migration files
# ---------------------------------------------------------------------------

_SHIPPED = sorted((Path(pg.__file__).resolve().parent / "migrations").glob("*.sql"))

_TRANSACTION_CONTROL = re.compile(
    r"^\s*(BEGIN|START\s+TRANSACTION|COMMIT|END|ROLLBACK|ABORT)\s*;",
    re.IGNORECASE | re.MULTILINE,
)


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", "", sql)


class TestShippedMigrations:
    def test_there_are_shipped_migrations_to_check(self):
        assert len(_SHIPPED) >= 22

    @pytest.mark.parametrize("path", _SHIPPED, ids=lambda p: p.name)
    def test_no_migration_controls_its_own_transaction(self, path):
        """The runner owns the transaction. A script's own COMMIT would end
        it before the tracking row is written."""
        found = _TRANSACTION_CONTROL.findall(_strip_sql_comments(path.read_text()))
        assert not found, (
            f"{path.name} contains transaction control {found}; remove it — "
            "run_migrations() wraps every migration in a transaction"
        )
