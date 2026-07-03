"""psycopg 3 ``RunRepo`` adapter over ``deepresearch.research_run``.

Doctrine parity with ``FakeRunRepo``:
- org-scoped reads/writes; ``list_by_context`` additionally user-scoped;
- duplicate public ``run_id`` -> ``Conflict``;
- missing org-scoped row on get/update -> ``NotFound``;
- connection failure -> ``RepositoryUnavailable``;
- rows are mapped ONLY through the domain ``from_row`` serializer.

Construction opens NO connection. The DSN may be a string or a zero-arg callable
(resolved per operation) so wiring can defer a missing env var to first use
rather than import time. Every operation connects, acts, and closes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..ports import Conflict, NotFound, RepositoryUnavailable
from ..research_run import ResearchRunRef, RunStatus, from_row

_TABLE = "deepresearch.research_run"

_INSERT_SQL = f"""
INSERT INTO {_TABLE}
    (id, run_id, org_id, created_by, title, query, params, status,
     visibility, result_ref, execution_id, started_at, completed_at,
     created_at, duration_ms)
VALUES
    (%(id)s, %(run_id)s, %(org_id)s, %(created_by)s, %(title)s, %(query)s,
     %(params)s, %(status)s, %(visibility)s, %(result_ref)s, %(execution_id)s,
     %(started_at)s, %(completed_at)s, %(created_at)s, %(duration_ms)s)
"""

_SELECT_ONE_SQL = (
    f"SELECT * FROM {_TABLE} "
    "WHERE org_id = %(org_id)s AND run_id = %(run_id)s"
)

_SELECT_LIST_SQL = (
    f"SELECT * FROM {_TABLE} "
    "WHERE org_id = %(org_id)s AND created_by = %(user_id)s "
    "ORDER BY created_at DESC"
)

_UPDATE_STATUS_SQL = f"""
UPDATE {_TABLE}
   SET status = %(status)s,
       completed_at = COALESCE(%(completed_at)s::timestamptz, completed_at),
       duration_ms = COALESCE(%(duration_ms)s::bigint, duration_ms)
 WHERE org_id = %(org_id)s AND run_id = %(run_id)s
RETURNING *
"""

_TABLE_REGCLASS_SQL = "SELECT to_regclass(%(qualified)s) AS reg"


class ResearchRunRepository:
    """Durable, org-scoped ``RunRepo`` backed by Postgres via psycopg 3."""

    def __init__(self, dsn: "str | Callable[[], str]") -> None:
        # No connection here — connect per operation.
        self._dsn = dsn

    # -- internals --------------------------------------------------------- #
    def _resolve_dsn(self) -> str:
        return self._dsn() if callable(self._dsn) else self._dsn

    def _connect(self) -> "psycopg.Connection[Any]":
        try:
            return psycopg.connect(self._resolve_dsn(), row_factory=dict_row)
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc

    # -- RunRepo ----------------------------------------------------------- #
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

    def add(self, ref: ResearchRunRef) -> None:
        params = {
            "id": ref.id,
            "run_id": ref.run_id,
            "org_id": ref.org_id,
            "created_by": ref.created_by,
            "title": None,
            "query": ref.query,
            "params": Json(dict(ref.params)),
            "status": ref.status,
            "visibility": ref.visibility,
            "result_ref": ref.result_ref,
            "execution_id": ref.execution_id,
            "started_at": ref.started_at,
            "completed_at": ref.completed_at,
            "created_at": ref.created_at,
            "duration_ms": ref.duration_ms,
        }
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(_INSERT_SQL, params)
                conn.commit()
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"duplicate run_id: {ref.run_id}") from exc
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc

    def get_by_context(self, ctx: Any, run_id: str) -> ResearchRunRef:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    _SELECT_ONE_SQL, {"org_id": ctx.org_id, "run_id": run_id}
                ).fetchone()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        if row is None:
            raise NotFound(f"run not found: {run_id}")
        return from_row(row)

    def list_by_context(self, ctx: Any) -> list[ResearchRunRef]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    _SELECT_LIST_SQL,
                    {"org_id": ctx.org_id, "user_id": ctx.user_id},
                ).fetchall()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        return [from_row(row) for row in rows]

    def update_status(
        self,
        ctx: Any,
        run_id: str,
        status: RunStatus,
        completed_at: datetime | None,
        duration_ms: int | None,
    ) -> ResearchRunRef:
        args: Mapping[str, Any] = {
            "status": status,
            "completed_at": completed_at,
            "duration_ms": duration_ms,
            "org_id": ctx.org_id,
            "run_id": run_id,
        }
        try:
            with self._connect() as conn:
                row = conn.execute(_UPDATE_STATUS_SQL, args).fetchone()
                conn.commit()
        except psycopg.OperationalError as exc:
            raise RepositoryUnavailable(str(exc)) from exc
        if row is None:
            raise NotFound(f"run not found: {run_id}")
        return from_row(row)
