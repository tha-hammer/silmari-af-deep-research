"""B6 — Postgres schema migration (integration).

Skipped unless ``TEST_DATABASE_URL`` points at a reachable throwaway Postgres.
Proves the migration is idempotent and produces exactly one, singular table:
``deepresearch.research_run``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")
    return dsn


def test_apply_twice_is_a_noop() -> None:
    from ui.workspace.postgres.migrate import apply

    dsn = _dsn()
    apply(dsn)
    apply(dsn)  # second application must not raise


def test_schema_has_singular_research_run_table() -> None:
    import psycopg

    from ui.workspace.postgres.migrate import apply

    dsn = _dsn()
    apply(dsn)
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %(schema)s ORDER BY table_name",
            {"schema": "deepresearch"},
        ).fetchall()
    tables = [r[0] for r in rows]
    assert tables == ["research_run"]


def test_check_ready_true_after_migration() -> None:
    from ui.workspace.postgres.migrate import apply
    from ui.workspace.postgres.readiness import check_ready

    dsn = _dsn()
    apply(dsn)
    assert check_ready(dsn) is True
