"""Schema readiness probe for the ``deepresearch.research_run`` table.

``check_ready(dsn)`` returns ``True`` iff the table exists with every column the
domain mapper depends on. Connection failures surface as ``RepositoryUnavailable``.
No connection is opened at import time.
"""

from __future__ import annotations

import psycopg

from ..ports import RepositoryUnavailable

# The columns ``from_row`` reads plus the thin-slice extras (title). Keep in sync
# with schema.sql.
_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "run_id",
        "org_id",
        "created_by",
        "title",
        "query",
        "params",
        "status",
        "visibility",
        "result_ref",
        "execution_id",
        "started_at",
        "completed_at",
        "created_at",
        "duration_ms",
    }
)

_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = %(schema)s AND table_name = %(table)s"
)


def check_ready(dsn: str) -> bool:
    """True iff ``deepresearch.research_run`` exists with all expected columns."""
    try:
        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                _COLUMNS_SQL, {"schema": "deepresearch", "table": "research_run"}
            ).fetchall()
    except psycopg.OperationalError as exc:
        raise RepositoryUnavailable(str(exc)) from exc
    present = {row[0] for row in rows}
    return _EXPECTED_COLUMNS.issubset(present)
