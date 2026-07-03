"""Typed DTOs and value objects that cross the workspace boundary.

Per ARCHITECTURE doctrine, raw dicts never leak inward: launch results,
API responses, and JSON payloads are all described by explicit types here.
``JSONValue`` is defined in this module (the boundary layer); ``RunStatus``
lives in ``research_run`` and is imported lazily to avoid a runtime cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Mapping, TypedDict

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime import cycle
    from .research_run import RunStatus

# Recursive JSON-compatible value. Used for run ``params`` and any payload that
# must round-trip through JSON without losing type information.
JSONValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JSONValue"]
    | dict[str, "JSONValue"]
)

# Display/config parameters attached to a run; always JSON-serializable.
ResearchRunParams = Mapping[str, JSONValue]


@dataclass(frozen=True)
class LaunchResult:
    """The immutable outcome of dispatching a run to the control plane.

    This is the launch boundary (plan B7): the only shape ``record_run_ownership``
    accepts. ``root_execution_id`` is the legacy/API alias for the stored
    ``result_ref``/``execution_id`` values.
    """

    run_id: str
    root_execution_id: str
    created_at: datetime
    status: "RunStatus"
    node: str
    reasoner: str
    params: Mapping[str, JSONValue]


class ResearchRunDTO(TypedDict):
    """`/api/runs` list item. Preserves the current frontend field set."""

    run_id: str
    root_execution_id: str | None
    created_at: str
    status: str
    params: Mapping[str, JSONValue]
    completed_at: str | None
    duration_ms: int | None


class LaunchRunDTO(TypedDict):
    """`POST /api/run` success response."""

    run_id: str
    root_execution_id: str
    created_at: str
    status: str
    node: str
    reasoner: str
    params: Mapping[str, JSONValue]


class ResearchResultDTO(TypedDict, total=False):
    """`GET /api/result` response. Required + optional (rendered) fields."""

    # Required
    status: str
    run_id: str
    params: Mapping[str, JSONValue]
    duration_ms: int | None
    source_count: int
    section_count: int
    # Optional / rendered
    html: str
    markdown: str
    sources: list[JSONValue]
    error: str
