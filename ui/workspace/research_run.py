"""The ``ResearchRunRef`` aggregate, status model, use cases, and serializers.

Doctrine:
- The aggregate owns its invariants (``__post_init__``) and the single
  status-transition method (``with_status``).
- ``assert_run_access`` is the ONLY ownership predicate.
- Serializers convert between the aggregate and its JSON / SQL-row / legacy
  representations; nothing else maps rows.

Note on ``run_id`` validation: rather than importing the heavy ``server``
module (``import server as srv``) just to reach ``srv.valid_run_id``, we
replicate its regex here in ``is_valid_run_id`` so unit tests stay pure and
import-light. The regex is kept byte-for-byte identical to
``server.valid_run_id`` (``run_[A-Za-z0-9_]+``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional, cast
from uuid import UUID

from .dto import JSONValue, LaunchResult, ResearchRunDTO
from .ports import Denied

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime import cycle
    from ..tenancy.context import RunContext
    from .ports import ControlPlanePort, RunRepo

# --------------------------------------------------------------------------- #
# Status / visibility model
# --------------------------------------------------------------------------- #
RunStatus = Literal["running", "succeeded", "failed", "cancelled"]
Visibility = Literal["private", "org"]

_VALID_STATUSES: frozenset[str] = frozenset(
    {"running", "succeeded", "failed", "cancelled"}
)
_VALID_VISIBILITIES: frozenset[str] = frozenset({"private", "org"})
_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

# Replicated regex, identical to server.valid_run_id (kept pure — see module docstring).
_RUN_ID_RE = re.compile(r"run_[A-Za-z0-9_]+")

CP_STATUS_TO_RUN_STATUS: dict[str, RunStatus] = {
    "": "running",
    "pending": "running",
    "queued": "running",
    "registered": "running",
    "submitted": "running",
    "running": "running",
    "succeeded": "succeeded",
    "success": "succeeded",
    "completed": "succeeded",
    "complete": "succeeded",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


class MapperError(ValueError):
    """A SQL row or legacy payload could not be mapped to the aggregate."""


def is_valid_run_id(run_id: object) -> bool:
    """True iff ``run_id`` is a non-empty ``run_<alnum/underscore>`` string."""
    return isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id) is not None


def normalize_cp_status(raw: str | None, logger: Any | None = None) -> RunStatus:
    """Map a raw control-plane status onto a ``RunStatus``.

    Pure and total: known statuses map through ``CP_STATUS_TO_RUN_STATUS``;
    an unknown *non-empty* status normalizes to ``"failed"`` and, when a
    ``logger`` is supplied, logs ``unknown_cp_status`` with the raw value so
    the failure stays visible without producing an invalid DB write.
    """
    key = (raw or "").strip().lower()
    mapped = CP_STATUS_TO_RUN_STATUS.get(key)
    if mapped is not None:
        return mapped
    if logger is not None:  # loggable, but stays pure (no IO of its own)
        logger.warning("unknown_cp_status", extra={"raw_cp_status": raw})
    return "failed"


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ResearchRunRef:
    id: UUID
    run_id: str
    org_id: UUID
    created_by: UUID
    query: str
    params: Mapping[str, JSONValue]
    status: RunStatus
    visibility: Visibility
    result_ref: str | None
    execution_id: str | None
    created_at: datetime
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        if self.id is None:
            raise ValueError("id is required")
        if not is_valid_run_id(self.run_id):
            raise ValueError(f"invalid run_id: {self.run_id!r}")
        if self.org_id is None:
            raise ValueError("org_id is required")
        if self.created_by is None:
            raise ValueError("created_by is required")
        if not self.query:
            raise ValueError("query is required")
        if self.created_at is None:
            raise ValueError("created_at is required")
        if self.started_at is None:
            raise ValueError("started_at is required")
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {self.status!r}")
        if self.visibility not in _VALID_VISIBILITIES:
            raise ValueError(f"invalid visibility: {self.visibility!r}")
        if self.duration_ms is not None and self.duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {self.duration_ms}")

    def with_status(
        self,
        status: RunStatus,
        completed_at: datetime | None = None,
        duration_ms: int | None = None,
    ) -> "ResearchRunRef":
        return replace(
            self,
            status=status,
            completed_at=self.completed_at if completed_at is None else completed_at,
            duration_ms=self.duration_ms if duration_ms is None else duration_ms,
        )


# --------------------------------------------------------------------------- #
# Use cases
# --------------------------------------------------------------------------- #
def record_run_ownership(
    ctx: "RunContext",
    launch: LaunchResult,
    repo: "RunRepo",
    clock: "Callable[[], datetime]",
    uuid_factory: "Callable[[], UUID]",
) -> ResearchRunRef:
    """Persist ownership for a freshly dispatched launch and return the ref."""
    now = clock()
    ref = ResearchRunRef(
        id=uuid_factory(),
        run_id=launch.run_id,
        org_id=ctx.org_id,
        created_by=ctx.user_id,
        query=str(launch.params.get("query", "")),
        params=launch.params,
        status=normalize_cp_status(launch.status),
        visibility="private",
        result_ref=launch.root_execution_id,
        execution_id=launch.root_execution_id,
        created_at=launch.created_at or now,
        started_at=launch.created_at or now,
    )
    repo.add(ref)
    return ref


def list_user_runs(ctx: "RunContext", repo: "RunRepo") -> list[ResearchRunRef]:
    """Return only the caller's own runs in the active org (newest first)."""
    return repo.list_by_context(ctx)


def refresh_run_status(
    ref: ResearchRunRef,
    ctx: "RunContext",
    repo: "RunRepo",
    control_plane: "ControlPlanePort",
) -> ResearchRunRef:
    """Refresh a non-terminal run's status from the control plane.

    Terminal refs are a strict no-op: no control-plane call, no repo write.
    """
    if ref.status in _TERMINAL_STATUSES:
        return ref
    execution_id = ref.execution_id or ref.result_ref
    if not execution_id:
        return ref
    payload = control_plane.get_execution(execution_id)
    if payload is None:
        return ref
    new_status = normalize_cp_status(payload.get("status", ""))
    completed_at = _coerce_dt_opt(payload.get("completed_at"))
    duration_ms = _coerce_duration_opt(payload.get("duration_ms"))
    return repo.update_status(ctx, ref.run_id, new_status, completed_at, duration_ms)


def assert_run_access(ref: Optional[ResearchRunRef], ctx: "RunContext") -> None:
    """The one and only ownership predicate. Raises ``Denied`` unless the
    ref exists AND belongs to both the caller's org and the caller."""
    if ref is None or ref.org_id != ctx.org_id or ref.created_by != ctx.user_id:
        raise Denied("not allowed")


# --------------------------------------------------------------------------- #
# Serializers
# --------------------------------------------------------------------------- #
def to_run_json(ref: ResearchRunRef) -> ResearchRunDTO:
    """Serialize a ref to the ``/api/runs`` DTO, preserving frontend fields."""
    return ResearchRunDTO(
        run_id=ref.run_id,
        root_execution_id=ref.result_ref,
        created_at=ref.created_at.isoformat(),
        status=ref.status,
        params=dict(ref.params),
        completed_at=ref.completed_at.isoformat() if ref.completed_at else None,
        duration_ms=ref.duration_ms,
    )


def from_row(row: Mapping[str, Any]) -> ResearchRunRef:
    """Map a single SQL row (dict-like) to a ``ResearchRunRef``.

    The only row mapper. Raises ``MapperError`` on invalid UUIDs, unknown
    status, invalid visibility, timestamp parse errors, invalid JSON params,
    or negative duration.
    """
    try:
        params = _coerce_params(row.get("params"))
        status = _require_status(row.get("status"))
        visibility = _require_visibility(row.get("visibility"))
        return ResearchRunRef(
            id=_coerce_uuid(row.get("id"), "id"),
            run_id=_require_run_id(row.get("run_id")),
            org_id=_coerce_uuid(row.get("org_id"), "org_id"),
            created_by=_coerce_uuid(row.get("created_by"), "created_by"),
            query=_require_query(row.get("query")),
            params=params,
            status=status,
            visibility=visibility,
            result_ref=_coerce_str_opt(row.get("result_ref")),
            execution_id=_coerce_str_opt(row.get("execution_id")),
            created_at=_coerce_dt(row.get("created_at"), "created_at"),
            started_at=_coerce_dt(row.get("started_at"), "started_at"),
            completed_at=_coerce_dt_opt(row.get("completed_at")),
            duration_ms=_coerce_duration_opt(row.get("duration_ms")),
        )
    except MapperError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise MapperError(str(exc)) from exc


@dataclass(frozen=True)
class LegacyRunData:
    """Validated, context-free view of a legacy ``ui/runs/run_*.json`` file.

    Deliberately lacks ``org_id``/``created_by`` — those are resolved from
    config at import time (B8). This serializer only validates the file.
    """

    run_id: str
    query: str
    params: Mapping[str, JSONValue]
    status: RunStatus
    root_execution_id: str | None
    created_at: datetime
    completed_at: datetime | None
    duration_ms: int | None


def from_legacy_json(path: Any, payload: Mapping[str, Any]) -> LegacyRunData:
    """Validate a legacy run JSON payload without writing anything.

    Raises ``MapperError`` on malformed input. ``path``'s filename stem must
    match the payload ``run_id``.
    """
    try:
        run_id = _require_run_id(payload.get("run_id"))
    except MapperError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalize to MapperError
        raise MapperError(str(exc)) from exc

    stem = _path_stem(path)
    if stem and stem != run_id:
        raise MapperError(f"filename run id {stem!r} != payload run_id {run_id!r}")

    params = _coerce_params(payload.get("params"))
    query = params.get("query")
    if not isinstance(query, str) or not query:
        # research_run.query is NOT NULL — a legacy file without a query is invalid.
        raise MapperError("legacy payload missing non-empty params.query")

    try:
        created_at = _coerce_dt(payload.get("created_at"), "created_at")
        completed_at = _coerce_dt_opt(payload.get("completed_at"))
    except MapperError:
        raise

    return LegacyRunData(
        run_id=run_id,
        query=query,
        params=params,
        status=normalize_cp_status(payload.get("status", "")),
        root_execution_id=_coerce_str_opt(payload.get("root_execution_id")),
        created_at=created_at,
        completed_at=completed_at,
        duration_ms=_coerce_duration_opt(payload.get("duration_ms")),
    )


# --------------------------------------------------------------------------- #
# Coercion helpers (all pure; raise MapperError on bad input)
# --------------------------------------------------------------------------- #
def _coerce_uuid(value: Any, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise MapperError(f"invalid uuid for {field}: {value!r}") from exc
    raise MapperError(f"invalid uuid for {field}: {value!r}")


def _require_run_id(value: Any) -> str:
    if not is_valid_run_id(value):
        raise MapperError(f"invalid run_id: {value!r}")
    return cast(str, value)  # narrowed by is_valid_run_id


def _require_query(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise MapperError(f"invalid query: {value!r}")
    return value


def _require_status(value: Any) -> RunStatus:
    if value not in _VALID_STATUSES:
        raise MapperError(f"invalid status: {value!r}")
    return cast("RunStatus", value)


def _require_visibility(value: Any) -> Visibility:
    if value not in _VALID_VISIBILITIES:
        raise MapperError(f"invalid visibility: {value!r}")
    return cast("Visibility", value)


def _coerce_params(value: Any) -> Mapping[str, JSONValue]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    raise MapperError(f"invalid params (not a mapping): {value!r}")


def _coerce_str_opt(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise MapperError(f"invalid string: {value!r}")


def _coerce_dt(value: Any, field: str) -> datetime:
    dt = _coerce_dt_opt(value)
    if dt is None:
        raise MapperError(f"{field} is required")
    return dt


def _coerce_dt_opt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Accept trailing 'Z' (UTC) which fromisoformat rejects on 3.10.
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise MapperError(f"invalid timestamp: {value!r}") from exc
    raise MapperError(f"invalid timestamp: {value!r}")


def _coerce_duration_opt(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise MapperError(f"invalid duration_ms: {value!r}")
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float) and value.is_integer():
        coerced = int(value)
    elif isinstance(value, str):
        try:
            coerced = int(value)
        except ValueError as exc:
            raise MapperError(f"invalid duration_ms: {value!r}") from exc
    else:
        raise MapperError(f"invalid duration_ms: {value!r}")
    if coerced < 0:
        raise MapperError(f"duration_ms must be >= 0, got {coerced}")
    return coerced


def _path_stem(path: Any) -> str:
    try:
        from pathlib import Path

        return Path(str(path)).stem
    except Exception:  # noqa: BLE001
        return ""
