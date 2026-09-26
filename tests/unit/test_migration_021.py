"""Static checks for migration 021_prompt_versions.sql (ADR-003 §2).

These are file-content assertions only -- no real Postgres connection, per
the unit-test taxonomy (integration tests exercise migrations against a
live database elsewhere).
"""

from __future__ import annotations

import re
from pathlib import Path

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "treeweft"
    / "adapters"
    / "postgresql"
    / "migrations"
)

MIGRATION_021 = MIGRATIONS_DIR / "021_prompt_versions.sql"


def test_migration_021_file_exists() -> None:
    assert MIGRATION_021.is_file(), f"missing {MIGRATION_021}"


def test_migration_numbers_have_no_gaps_or_duplicates() -> None:
    numbers = []
    for f in MIGRATIONS_DIR.glob("*.sql"):
        m = re.match(r"^(\d+)_", f.name)
        assert m, f"migration file {f.name} does not start with NNN_"
        numbers.append(int(m.group(1)))

    numbers.sort()

    assert len(numbers) == len(set(numbers)), (
        f"duplicate migration numbers found: {numbers}"
    )
    assert numbers == list(range(numbers[0], numbers[-1] + 1)), (
        f"gap in migration numbers: {numbers}"
    )
    assert 21 in numbers


def test_every_create_and_add_column_is_if_not_exists() -> None:
    sql = MIGRATION_021.read_text()

    # Every CREATE TABLE / CREATE INDEX must have IF NOT EXISTS immediately after.
    for m in re.finditer(r"CREATE\s+(TABLE|INDEX)\b", sql, re.IGNORECASE):
        tail = sql[m.end() : m.end() + 20]
        assert "IF NOT EXISTS" in tail.upper(), (
            f"CREATE {m.group(1)} at offset {m.start()} is missing IF NOT EXISTS"
        )

    # Every ADD COLUMN must have IF NOT EXISTS immediately after.
    for m in re.finditer(r"ADD\s+COLUMN\b", sql, re.IGNORECASE):
        tail = sql[m.end() : m.end() + 20]
        assert "IF NOT EXISTS" in tail.upper(), (
            f"ADD COLUMN at offset {m.start()} is missing IF NOT EXISTS"
        )


def test_no_pin_rows_inserted() -> None:
    sql = MIGRATION_021.read_text()
    assert re.search(r"INSERT\s+INTO\s+prompt_pins", sql, re.IGNORECASE) is None, (
        "021_prompt_versions.sql must not insert pin rows; pins are seeded at startup"
    )


def test_check_constraint_present() -> None:
    sql = MIGRATION_021.read_text()
    normalized = " ".join(sql.split())
    assert "CHECK (operation <> 'hyde' OR scope = 'deployment')" in normalized
