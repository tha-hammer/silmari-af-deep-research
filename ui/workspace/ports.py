"""Repository and control-plane ports plus the domain error taxonomy.

These are the boundary contracts the domain talks to. Concrete adapters
(in-memory fake now, psycopg in B6) implement ``RunRepo``; the control plane
is reached through ``ControlPlanePort``. All type references to domain
aggregates and the request context are import-cycle-safe: they are resolved
only under ``TYPE_CHECKING`` (annotations are lazy strings at runtime).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Mapping, Protocol, TypedDict

from .dto import JSONValue

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..tenancy.context import RunContext
    from .research_run import ResearchRunRef, RunStatus


# --------------------------------------------------------------------------- #
# Domain errors
# --------------------------------------------------------------------------- #
class Conflict(Exception):
    """A uniqueness constraint was violated (e.g. duplicate public run_id)."""


class NotFound(Exception):
    """A requested row does not exist in the active org scope."""


class Denied(Exception):
    """Access is refused. The only ownership predicate raises this."""


class RepositoryUnavailable(Exception):
    """The backing store could not be reached (wraps connection failures)."""


# --------------------------------------------------------------------------- #
# Run repository port (Behavior 1)
# --------------------------------------------------------------------------- #
class RunRepo(Protocol):
    """Durable, org-scoped index of run ownership.

    Every read/write is scoped to ``ctx.org_id``; ``list_by_context`` is
    additionally scoped to ``ctx.user_id``. Same-org/other-user rows are
    fetchable via ``get_by_context`` and then denied by ``assert_run_access``.
    """

    def ensure_ready(self) -> None: ...

    def add(self, ref: "ResearchRunRef") -> None:
        """Insert a new ref. Duplicate public ``run_id`` raises ``Conflict``."""
        ...

    def get_by_context(self, ctx: "RunContext", run_id: str) -> "ResearchRunRef":
        """Return the org-scoped row for ``run_id`` or raise ``NotFound``."""
        ...

    def list_by_context(self, ctx: "RunContext") -> list["ResearchRunRef"]:
        """Return the caller's own runs in the active org, newest first."""
        ...

    def update_status(
        self,
        ctx: "RunContext",
        run_id: str,
        status: "RunStatus",
        completed_at: datetime | None,
        duration_ms: int | None,
    ) -> "ResearchRunRef":
        """Update an org-scoped row's status. Missing row raises ``NotFound``."""
        ...


# --------------------------------------------------------------------------- #
# Control-plane port (Behavior 4)
# --------------------------------------------------------------------------- #
class ExecutionPayload(TypedDict, total=False):
    status: str
    completed_at: str | datetime | None
    duration_ms: int | None
    result: Mapping[str, JSONValue]
    error: str
    error_message: str


class CancelResult(TypedDict, total=False):
    cancelled: bool
    execution_id: str
    error: str


class ControlPlanePort(Protocol):
    def get_execution(self, execution_id: str) -> ExecutionPayload | None: ...

    def cancel_execution(
        self, execution_id: str, reason: str
    ) -> CancelResult | None: ...
