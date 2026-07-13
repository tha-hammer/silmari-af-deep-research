"""psycopg 3 ``ReelJobPort`` adapter over ``deepresearch.reel_job`` (B2/B6).

Doctrine parity with ``ResearchRunRepository``:
- org-scoped reads/writes;
- duplicate ``(org_id, created_by, client_request_id)`` -> ``Conflict``;
- missing org-scoped row on get/update -> ``NotFound``;
- connection failure -> ``RepositoryUnavailable``.

Construction opens NO connection (the DSN may be a zero-arg callable resolved
per operation). The ``deepresearch.reel_job`` table is AS-BUILT (meta-repo
migration 108); this adapter adds no migration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from ..ports import Conflict, NotFound, RepositoryUnavailable
from ..reel_job import ReelJobRef, ReelJobStatus

_TABLE = "deepresearch.reel_job"

_INSERT_SQL = f"""
INSERT INTO {_TABLE}
    (id, org_id, created_by, client_request_id, title,
     source_research_run_id, status, result_ref, execution_id,
     created_at, completed_at)
VALUES
    (%(id)s, %(org_id)s, %(created_by)s, %(client_request_id)s, %(title)s,
     %(source_research_run_id)s, %(status)s, %(result_ref)s, %(execution_id)s,
     %(created_at)s, %(completed_at)s)
"""

_SELECT_ONE_SQL = (
    f"SELECT * FROM {_TABLE} WHERE org_id = %(org_id)s AND id = %(id)s"
)

_UPDATE_STATUS_SQL = f"""
UPDATE {_TABLE}
   SET status = %(status)s,
       result_ref = COALESCE(%(result_ref)s, result_ref),
       completed_at = COALESCE(%(completed_at)s::timestamptz, completed_at)
 WHERE org_id = %(org_id)s AND id = %(id)s
RETURNING *
"""

_TABLE_REGCLASS_SQL = "SELECT to_regclass(%(qualified)s) AS reg"


def _from_row(row: Mapping[str, Any]) -> ReelJobRef:
    return ReelJobRef(
        id=row["id"] if isinstance(row["id"], UUID) else UUID(str(row["id"])),
        org_id=row["org_id"]
        if isinstance(row["org_id"], UUID)
        else UUID(str(row["org_id"])),
        created_by=row["created_by"]
        if isinstance(row["created_by"], UUID)
        else UUID(str(row["created_by"])),
        status=row["status"],
        source_research_run_id=row.get("source_research_run_id"),
        execution_id=row.get("execution_id"),
        result_ref=row.get("result_ref"),
        client_request_id=row.get("client_request_id"),
        title=row.get("title"),
        created_at=row.get("created_at"),
        completed_at=row.get("completed_at"),
    )


class ReelJobRepository:
    """Durable, org-scoped ``ReelJobPort`` backed by Postgres via psycopg 3."""

    def __init__(self, dsn: "str | Callable[[], str]") -> None:
        self._dsn = dsn

    def _resolve_dsn(self) -> str:
        return self._dsn() if callable(self._dsn) else self._dsn

    def _connect(self) -> "psycopg.Connection[Any]":
        try:
            return psycopg.connect(self._resolve_dsn(), row_factory=dict_row)
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc

    def ensure_ready(self) -> None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    _TABLE_REGCLASS_SQL, {"qualified": _TABLE}
                ).fetchone()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        if row is None or row["reg"] is None:
            raise RepositoryUnavailable(f"{_TABLE} is not provisioned")

    def create(self, ref: ReelJobRef) -> None:
        params = {
            "id": ref.id,
            "org_id": ref.org_id,
            "created_by": ref.created_by,
            "client_request_id": ref.client_request_id,
            "title": ref.title,
            "source_research_run_id": ref.source_research_run_id,
            "status": ref.status,
            "result_ref": ref.result_ref,
            "execution_id": ref.execution_id,
            "created_at": ref.created_at,
            "completed_at": ref.completed_at,
        }
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, params)
                conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"duplicate reel job: {ref.id}") from exc
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc

    def get_by_context(self, ctx: Any, job_id: str) -> ReelJobRef:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    _SELECT_ONE_SQL, {"org_id": ctx.org_id, "id": job_id}
                ).fetchone()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        if row is None:
            raise NotFound(f"reel job not found: {job_id}")
        return _from_row(row)

    def update_status(
        self,
        ctx: Any,
        job_id: str,
        status: ReelJobStatus,
        result_ref: str | None,
        completed_at: datetime | None,
    ) -> ReelJobRef:
        args: Mapping[str, Any] = {
            "status": status,
            "result_ref": result_ref,
            "completed_at": completed_at,
            "org_id": ctx.org_id,
            "id": job_id,
        }
        try:
            with self._connect() as conn:
                row = conn.execute(_UPDATE_STATUS_SQL, args).fetchone()
                conn.commit()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        if row is None:
            raise NotFound(f"reel job not found: {job_id}")
        return _from_row(row)
