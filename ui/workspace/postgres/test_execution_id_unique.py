"""INT-01 · Owner integration — execution_id becomes the safe canonical join key.

Proves the B1 (partial UNIQUE) + B2 (de-dupe, re-point-before-delete) behaviors of
``migrations/deepresearch/112_add_unique_execution_id_research_run.sql`` against a LIVE
Postgres. The migration is ROOT-owned (silmari-agentfield-system); this test reads the real
file and applies it, so the DDL under test is the shipped DDL, not a copy.

Fail-closed: requires ``TEST_DATABASE_URL``. When it is unset the tests FAIL (they never
silently pass) so an unconfigured environment can never report a false green.

Run:  TEST_DATABASE_URL=postgres://... uv run pytest -q -m integration test_execution_id_unique.py
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# ROOT migration under test — silmari-af-deep-research is nested inside silmari-agentfield-system,
# so parents[4] is the agentfield-system repo root.
MIGRATION_112 = (
    Path(__file__).resolve().parents[4]
    / "migrations" / "deepresearch" / "112_add_unique_execution_id_research_run.sql"
)

# Minimal fixture schema — the columns/keys the migration and its inserts touch. The
# reel_job/carousel FK `source_research_run_id ... ON DELETE SET NULL` is the load-bearing
# part of the re-point regression; org/user FKs are omitted to keep the fixture tight.
_SCHEMA = """
drop schema if exists deepresearch cascade;
create schema deepresearch;
create table deepresearch.research_run (
    id           uuid primary key,
    run_id       text not null unique,
    org_id       uuid not null,
    created_by   uuid not null,
    query        text not null,
    params       jsonb not null default '{}',
    status       text not null check (status in ('running','succeeded','failed','cancelled')),
    visibility   text not null check (visibility in ('org','private')),
    execution_id text,
    created_at   timestamptz
);
create table deepresearch.reel_job (
    id                     uuid primary key,
    org_id                 uuid not null,
    created_by             uuid not null,
    client_request_id      text not null,
    source_research_run_id uuid references deepresearch.research_run(id) on delete set null,
    execution_id           text,
    created_at             timestamptz not null default now()
);
create table deepresearch.carousel (
    id                     uuid primary key,
    org_id                 uuid not null,
    created_by             uuid not null,
    client_request_id      text not null,
    source_research_run_id uuid references deepresearch.research_run(id) on delete set null,
    execution_id           text,
    created_at             timestamptz not null default now()
);
"""

ORG_ID = str(uuid.uuid4())
USER_ID = str(uuid.uuid4())


def _dsn() -> str:
    dsn = os.getenv("TEST_DATABASE_URL")
    if not dsn:
        pytest.fail(
            "TEST_DATABASE_URL unset — INT-01 unique-constraint test cannot verify; "
            "fail-closed (do not skip-to-green)."
        )
    return dsn


def _migration_up_sql() -> str:
    """Return the `migrate:up` half of the real 112 migration file."""
    text = MIGRATION_112.read_text()
    up = text.split("-- migrate:down", 1)[0]
    return up.replace("-- migrate:up", "", 1)


@pytest.fixture()
def pg():
    import psycopg

    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute(_SCHEMA)
        yield conn


def _insert_run(conn, *, execution_id, created_at="2026-01-01T00:00:00Z", run_id=None):
    rid = str(uuid.uuid4())
    conn.execute(
        "insert into deepresearch.research_run "
        "(id, run_id, org_id, created_by, query, status, visibility, execution_id, created_at) "
        "values (%s,%s,%s,%s,'q','running','org',%s,%s)",
        (rid, run_id or f"run_{rid[:8]}", ORG_ID, USER_ID, execution_id, created_at),
    )
    return rid


def _seed_reel_job(conn, *, execution_id, source_research_run_id):
    jid = str(uuid.uuid4())
    conn.execute(
        "insert into deepresearch.reel_job "
        "(id, org_id, created_by, client_request_id, source_research_run_id, execution_id) "
        "values (%s,%s,%s,%s,%s,%s)",
        (jid, ORG_ID, USER_ID, f"crid_{jid[:8]}", source_research_run_id, execution_id),
    )
    return jid


def _apply_112(conn):
    conn.execute(_migration_up_sql())


def _row_count(conn):
    return conn.execute("select count(*) from deepresearch.research_run").fetchone()[0]


# --- B1: partial UNIQUE(execution_id) -------------------------------------------------

def test_two_rows_same_execution_id_violate_unique(pg):
    import psycopg

    exec_id = "exec_" + uuid.uuid4().hex
    _insert_run(pg, execution_id=exec_id)
    _apply_112(pg)
    with pytest.raises(psycopg.errors.UniqueViolation) as ei:
        _insert_run(pg, execution_id=exec_id)
    msg = str(ei.value).lower()
    assert "ux_research_run_execution_id" in msg or "unique" in msg


def test_two_null_execution_ids_both_insert(pg):
    _apply_112(pg)
    _insert_run(pg, execution_id=None)
    _insert_run(pg, execution_id=None)  # partial index ignores NULLs — no raise
    assert _row_count(pg) == 2


# --- B2: de-dupe repair (re-point before delete) --------------------------------------

def test_dedupe_collapses_duplicate_execution_id(pg):
    exec_id = "exec_" + uuid.uuid4().hex
    _insert_run(pg, execution_id=exec_id, created_at="2026-01-01T00:00:00Z")
    _insert_run(pg, execution_id=exec_id, created_at="2026-06-01T00:00:00Z")  # pre-index duplicate
    _apply_112(pg)
    got = pg.execute(
        "select count(*) from deepresearch.research_run where execution_id=%s", (exec_id,)
    ).fetchone()[0]
    assert got == 1  # collapsed to the winner


def test_dedupe_is_noop_when_clean(pg):
    # doubles as the migration-idempotency test: applying 112 twice on a clean DB changes
    # nothing and raises nothing.
    _insert_run(pg, execution_id="exec_" + uuid.uuid4().hex)
    _insert_run(pg, execution_id=None)
    before = _row_count(pg)
    _apply_112(pg)
    _apply_112(pg)
    assert _row_count(pg) == before


def test_dedupe_repoints_downstream_provenance_not_null(pg):
    # A reel_job whose provenance points at the LOSER must, after 112, point at the WINNER —
    # proving the ON DELETE SET NULL cascade never nulled provenance (contract C11).
    exec_id = "exec_" + uuid.uuid4().hex
    winner_id = _insert_run(pg, execution_id=exec_id, created_at="2026-01-01T00:00:00Z")
    loser_id = _insert_run(pg, execution_id=exec_id, created_at="2026-06-01T00:00:00Z")
    _seed_reel_job(pg, execution_id=exec_id, source_research_run_id=loser_id)  # points at loser
    _apply_112(pg)
    got = pg.execute(
        "select source_research_run_id from deepresearch.reel_job where execution_id=%s",
        (exec_id,),
    ).fetchone()[0]
    assert got is not None                 # provenance NOT nulled by the cascade (C11)
    assert str(got) == winner_id           # re-pointed to the survivor
