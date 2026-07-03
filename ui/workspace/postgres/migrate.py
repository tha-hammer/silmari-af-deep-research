"""Idempotent schema migration for the ``deepresearch.research_run`` table.

``apply(dsn)`` connects via psycopg 3, executes ``schema.sql``, and commits.
Every statement in ``schema.sql`` uses ``IF NOT EXISTS`` so running twice is a
no-op. The ``python -m ui.workspace.postgres.migrate`` entrypoint reads the DSN
from ``DEEPRESEARCH_DATABASE_URL``.

No connection is opened at import time — only inside ``apply``/``main``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _statements(sql_text: str) -> list[str]:
    """Split a semicolon-separated DDL script into individual statements.

    Full-line ``--`` comments (which may themselves contain semicolons) and
    blank lines are dropped first; the remaining SQL — which contains no
    semicolons inside string literals — is split on ``;``. This keeps ``apply``
    compatible with the extended query protocol (one statement per ``execute``).
    """
    code_lines = [
        line
        for line in sql_text.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    cleaned = "\n".join(code_lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def apply(dsn: str) -> None:
    """Apply ``schema.sql`` against ``dsn``. Idempotent; safe to run repeatedly."""
    sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            for stmt in _statements(sql_text):
                cur.execute(stmt)
        conn.commit()


def main(argv: list[str] | None = None) -> int:
    dsn = os.environ.get("DEEPRESEARCH_DATABASE_URL")
    if not dsn:
        print("DEEPRESEARCH_DATABASE_URL is not set", file=sys.stderr)
        return 2
    apply(dsn)
    print("applied deepresearch.research_run schema")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
