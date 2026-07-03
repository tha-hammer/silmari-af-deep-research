"""In-memory, IO-free fakes for the workspace ports.

Used by unit/contract tests and (later) the Flask e2e factory. They enforce
the same org-scoping and error contract as the psycopg adapter so the
``test_ports_contract`` suite can be parametrized across both (fake now,
``pg`` in B6). No IO, network, DB, wall-clock, or randomness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from ..tenancy.context import RunContext
from .dto import JSONValue, LaunchResult
from .ports import CancelResult, Conflict, ExecutionPayload, NotFound
from .research_run import ResearchRunRef, RunStatus


class FakeRunRepo:
    """Dict-backed ``RunRepo``. Keyed by public ``run_id``; org-scoped reads."""

    def __init__(self) -> None:
        self._rows: dict[str, ResearchRunRef] = {}
        self.ready_calls: int = 0

    def ensure_ready(self) -> None:
        self.ready_calls += 1

    def add(self, ref: ResearchRunRef) -> None:
        if ref.run_id in self._rows:
            raise Conflict(f"duplicate run_id: {ref.run_id}")
        self._rows[ref.run_id] = ref

    def get_by_context(self, ctx: RunContext, run_id: str) -> ResearchRunRef:
        ref = self._rows.get(run_id)
        # A row outside the active org is indistinguishable from absent data.
        if ref is None or ref.org_id != ctx.org_id:
            raise NotFound(f"run not found: {run_id}")
        return ref

    def list_by_context(self, ctx: RunContext) -> list[ResearchRunRef]:
        scoped = [
            ref
            for ref in self._rows.values()
            if ref.org_id == ctx.org_id and ref.created_by == ctx.user_id
        ]
        scoped.sort(key=lambda r: r.created_at, reverse=True)
        return scoped

    def update_status(
        self,
        ctx: RunContext,
        run_id: str,
        status: RunStatus,
        completed_at: datetime | None,
        duration_ms: int | None,
    ) -> ResearchRunRef:
        ref = self._rows.get(run_id)
        if ref is None or ref.org_id != ctx.org_id:
            raise NotFound(f"run not found: {run_id}")
        updated = ref.with_status(status, completed_at, duration_ms)
        self._rows[run_id] = updated
        return updated


class FakeControlPlane:
    """Fake ``ControlPlanePort`` backed by pre-seeded execution payloads."""

    def __init__(
        self,
        payloads: dict[str, ExecutionPayload] | None = None,
    ) -> None:
        self._payloads: dict[str, ExecutionPayload] = payloads or {}
        self.get_calls: list[str] = []
        self.cancel_calls: list[tuple[str, str]] = []

    def set_execution(self, execution_id: str, payload: ExecutionPayload) -> None:
        self._payloads[execution_id] = payload

    def get_execution(self, execution_id: str) -> ExecutionPayload | None:
        self.get_calls.append(execution_id)
        return self._payloads.get(execution_id)

    def cancel_execution(
        self, execution_id: str, reason: str
    ) -> CancelResult | None:
        self.cancel_calls.append((execution_id, reason))
        return CancelResult(cancelled=True, execution_id=execution_id)


class FakeLaunch:
    """Deterministic launch callable returning a fixed-shape ``LaunchResult``."""

    def __init__(
        self,
        run_id: str,
        root_execution_id: str,
        created_at: datetime,
        status: RunStatus = "running",
        node: str = "meta_deep_research",
        reasoner: str = "execute_deep_research",
    ) -> None:
        self._result = LaunchResult(
            run_id=run_id,
            root_execution_id=root_execution_id,
            created_at=created_at,
            status=status,
            node=node,
            reasoner=reasoner,
            params={},
        )
        self.calls: list[dict[str, JSONValue]] = []

    def __call__(self, params: dict[str, JSONValue]) -> LaunchResult:
        self.calls.append(dict(params))
        # Echo the caller's params (e.g. the query) into the result.
        from dataclasses import replace

        return replace(self._result, params=dict(params))


def fixed_clock(when: datetime) -> Callable[[], datetime]:
    """A deterministic ``clock`` returning ``when`` on every call."""

    def _clock() -> datetime:
        return when

    return _clock
