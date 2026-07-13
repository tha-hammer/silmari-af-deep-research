"""The ``ReelJobRef`` aggregate, status model, ownership predicate, port, and
status-refresh use case (MW Phase 3, B2/B6).

Doctrine parity with ``research_run``:
- the aggregate owns its invariants (``__post_init__``);
- ``assert_reel_job_access`` is the ONLY ownership predicate (mirrors
  ``assert_run_access``);
- the control-plane status is mapped by a pure, total normalizer;
- ``ReelJobPort`` is the boundary contract implemented by the fake (tests) and
  the psycopg ``ReelJobRepository`` (production).

The ``deepresearch.reel_job`` table is AS-BUILT (meta-repo migration 108); this
module adds NO migration — it is the Python read/write surface over that table.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Mapping, Optional, Protocol
from uuid import UUID

# Reuse the timestamp coercion + Denied from the sibling run module (same package,
# no new import cycle — research_run imports nothing from here at runtime).
from .ports import Denied
from .research_run import _coerce_dt_opt  # noqa: PLC2701 - shared pure coercion

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..tenancy.context import RunContext
    from .ports import ControlPlanePort

# --------------------------------------------------------------------------- #
# Status model
# --------------------------------------------------------------------------- #
ReelJobStatus = Literal["queued", "producing", "succeeded", "failed", "cancelled"]

_VALID_STATUSES: frozenset[str] = frozenset(
    {"queued", "producing", "succeeded", "failed", "cancelled"}
)
_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

# Control-plane execution status → reel-job status. A queued/pending execution is
# still "queued"; a running one is "producing"; unknown non-empty → "failed".
CP_STATUS_TO_JOB_STATUS: dict[str, ReelJobStatus] = {
    "": "queued",
    "pending": "queued",
    "queued": "queued",
    "registered": "queued",
    "submitted": "queued",
    "running": "producing",
    "processing": "producing",
    "producing": "producing",
    "rendering": "producing",
    "composing": "producing",
    "succeeded": "succeeded",
    "success": "succeeded",
    "completed": "succeeded",
    "complete": "succeeded",
    "failed": "failed",
    "error": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


def normalize_job_status(raw: str | None, logger: Any | None = None) -> ReelJobStatus:
    """Map a raw control-plane status onto a ``ReelJobStatus`` (pure, total).

    Unknown non-empty statuses normalize to ``"failed"`` (never fabricate
    ``succeeded``); an optional logger records the surprise without doing IO.
    """
    key = (raw or "").strip().lower()
    mapped = CP_STATUS_TO_JOB_STATUS.get(key)
    if mapped is not None:
        return mapped
    if logger is not None:
        logger.warning("unknown_cp_status_reel", extra={"raw_cp_status": raw})
    return "failed"


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ReelJobRef:
    """An org-scoped ``deepresearch.reel_job`` row. ``id`` is the local job id
    (returned to the client as ``job_id``); ``execution_id`` is the control-plane
    execution key the poll path reads; ``source_research_run_id`` carries the
    research→reel provenance (FK to ``research_run(id)``)."""

    id: UUID
    org_id: UUID
    created_by: UUID
    status: ReelJobStatus
    source_research_run_id: UUID | None = None
    execution_id: str | None = None
    result_ref: str | None = None
    client_request_id: str | None = None
    title: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.id is None:
            raise ValueError("id is required")
        if self.org_id is None:
            raise ValueError("org_id is required")
        if self.created_by is None:
            raise ValueError("created_by is required")
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"invalid status: {self.status!r}")

    def with_status(
        self,
        status: ReelJobStatus,
        result_ref: str | None = None,
        completed_at: datetime | None = None,
    ) -> "ReelJobRef":
        return replace(
            self,
            status=status,
            result_ref=self.result_ref if result_ref is None else result_ref,
            completed_at=self.completed_at if completed_at is None else completed_at,
        )


# --------------------------------------------------------------------------- #
# Ownership predicate (the ONE reel-job predicate)
# --------------------------------------------------------------------------- #
def assert_reel_job_access(ref: Optional[ReelJobRef], ctx: "RunContext") -> None:
    """Raise ``Denied`` unless the job exists AND belongs to both the caller's
    org and the caller (mirrors ``assert_run_access``)."""
    if ref is None or ref.org_id != ctx.org_id or ref.created_by != ctx.user_id:
        raise Denied("not allowed")


# --------------------------------------------------------------------------- #
# Port (Behavior 2/6)
# --------------------------------------------------------------------------- #
class ReelJobPort(Protocol):
    """Durable, org-scoped index of reel jobs. Every read/write is scoped to
    ``ctx.org_id``; a foreign-org row is indistinguishable from absent
    (``NotFound``)."""

    def ensure_ready(self) -> None: ...

    def create(self, ref: "ReelJobRef") -> None:
        """Insert a queued reel-job row. A duplicate
        ``(org_id, created_by, client_request_id)`` raises ``Conflict``."""
        ...

    def get_by_context(self, ctx: "RunContext", job_id: str) -> "ReelJobRef":
        """Return the org-scoped row for ``job_id`` or raise ``NotFound``."""
        ...

    def update_status(
        self,
        ctx: "RunContext",
        job_id: str,
        status: "ReelJobStatus",
        result_ref: str | None,
        completed_at: datetime | None,
    ) -> "ReelJobRef":
        """Persist a status transition. Missing org-scoped row raises ``NotFound``."""
        ...


# --------------------------------------------------------------------------- #
# Use case — poll-through status refresh (B6, close-the-loop)
# --------------------------------------------------------------------------- #
_REEL_REF_KEYS = ("reel_ref", "video_path", "result_ref", "download_url", "url")


def extract_reel_ref(result: Mapping[str, Any] | None) -> str | None:
    """Pull a reel reference out of a control-plane ``result`` payload."""
    if not isinstance(result, Mapping):
        return None
    for key in _REEL_REF_KEYS:
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def refresh_reel_job_status(
    job: ReelJobRef,
    ctx: "RunContext",
    repo: "ReelJobPort",
    control_plane: "ControlPlanePort",
) -> ReelJobRef:
    """Refresh a non-terminal reel job's status from the control plane.

    Terminal jobs are a strict no-op. An unreachable control plane (``None``
    payload) leaves the last-known status intact — a ``succeeded`` is NEVER
    fabricated (B6 red-at-seam).
    """
    if job.status in _TERMINAL_STATUSES:
        return job
    if not job.execution_id:
        return job
    payload = control_plane.get_execution(job.execution_id)
    if payload is None:
        return job
    new_status = normalize_job_status(payload.get("status", ""))
    result = payload.get("result") if isinstance(payload, Mapping) else None
    reel_ref = extract_reel_ref(result) if new_status == "succeeded" else None
    completed_at = _coerce_dt_opt(payload.get("completed_at"))
    return repo.update_status(ctx, str(job.id), new_status, reel_ref, completed_at)


# --------------------------------------------------------------------------- #
# Serializer
# --------------------------------------------------------------------------- #
def to_reel_status_json(ref: ReelJobRef) -> dict[str, Any]:
    """The ``GET /api/reel-status`` response body: ``{status, reel_ref?}``."""
    out: dict[str, Any] = {"status": ref.status}
    if ref.result_ref:
        out["reel_ref"] = ref.result_ref
    return out
