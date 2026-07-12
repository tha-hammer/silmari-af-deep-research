"""Producer half of INT Phase 2: build the ``research.completed`` CloudEvent.

The deep-research producer announces a *terminal, succeeded* run to the durable
substrate by publishing a CloudEvent (envelope + a SMALL owner DTO) through the
shipped ``DurableExecutionBus.Publish`` at the execution-terminal state-write
seam. This module owns only the *building* of that event; the Go call site that
publishes it inside the terminal-write transaction is wired separately by the
control-plane owner (see the handoff note).

Contracts realized here (``specs/cross-app-handoff.pattern.md`` §4):
- **C-Notification** — ``data`` carries ids + primitives + a small snapshot
  ONLY. The document body (``research_package``) is NEVER copied into the event;
  reel-af fetches it by reference via ``result_ref`` on demand.
- **C-Correlation** — ``subject = execution_id`` (INT-01's UNIQUE key), the
  single cross-app join key.
- **C-Own** — deep-research emits its own event describing its own run.

The ``Publish``-in-the-terminal-write-tx atomicity (**C-Outbox**) is a property
of the call site, not of this builder.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

# --------------------------------------------------------------------------- #
# Named constants — never magic strings (CodeCleanup: NamedConstantsOverLiterals)
# --------------------------------------------------------------------------- #

#: CloudEvents ``type`` for the completion announcement.
EVENT_TYPE_RESEARCH_COMPLETED = "research.completed"

#: CloudEvents ``source`` — the producing app.
EVENT_SOURCE = "silmari-af-deep-research"

#: The one status this plan emits ``research.completed`` for. A failed or
#: cancelled run has no research output to hand off (whether failure emits a
#: distinct ``research.failed`` event is a separate reactor concern — B1 edge).
SUCCEEDED_STATUS = "succeeded"

# Execution / result field keys (the terminal execution snapshot shape).
EXECUTION_ID_KEY = "execution_id"
RUN_ID_KEY = "run_id"
STATUS_KEY = "status"
RESULT_KEY = "result"
METADATA_KEY = "metadata"
META_QUERY_KEY = "query"
META_TITLE_KEY = "title"
#: The body — asserted ABSENT from ``data`` (C-Notification). Named so the
#: builder and its tests reference the same key.
RESEARCH_PACKAGE_KEY = "research_package"

#: Canonical, execution-id-keyed address of the result the consumer fetches by
#: reference (C-Notification). The event never carries the body itself.
RESULT_REF_SCHEME = "cp-execution"


def _result_ref(execution_id: str) -> str:
    """The by-reference result address keyed by ``execution_id``."""
    return f"{RESULT_REF_SCHEME}://{execution_id}/{RESULT_KEY}"


def _new_event_id() -> str:
    """A fresh, unique-per-emit CloudEvents ``id``."""
    return f"ce-{uuid.uuid4()}"


def _now_iso() -> str:
    """Current UTC time as a CloudEvents ``time`` (RFC 3339, ``Z`` suffix)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_research_completed(
    execution: Mapping[str, Any],
    *,
    event_id: str | None = None,
    time: str | None = None,
) -> dict[str, Any]:
    """Build the ``research.completed`` CloudEvent for a terminal execution.

    ``execution`` is the terminal execution snapshot — a mapping carrying
    ``execution_id``, ``run_id``, ``status`` (must be ``"succeeded"``), and
    ``result`` (with ``metadata.query`` / ``metadata.title`` and the
    ``research_package`` body, which is NOT copied into the event).

    ``event_id`` and ``time`` are the non-deterministic envelope fields; they
    default to a fresh id and the current time, and are injectable so the emit
    is reproducible against the shared golden fixture (B6).

    Raises ``ValueError`` unless the run reached terminal ``succeeded`` — the
    builder emits for ``succeeded`` only.
    """
    status = execution.get(STATUS_KEY)
    if status != SUCCEEDED_STATUS:
        raise ValueError(
            f"research.completed is emitted for {SUCCEEDED_STATUS!r} only, "
            f"got status={status!r}"
        )

    execution_id = execution[EXECUTION_ID_KEY]
    result = execution.get(RESULT_KEY) or {}
    metadata = result.get(METADATA_KEY) or {}

    data = {
        RUN_ID_KEY: execution.get(RUN_ID_KEY),
        STATUS_KEY: SUCCEEDED_STATUS,
        "title": metadata.get(META_TITLE_KEY),
        "result_ref": _result_ref(execution_id),
        "research_prompt": metadata.get(META_QUERY_KEY),
        "research_document_id": execution_id,
    }

    return {
        "id": event_id or _new_event_id(),
        "source": EVENT_SOURCE,
        "type": EVENT_TYPE_RESEARCH_COMPLETED,
        "subject": execution_id,  # C-Correlation: subject == execution_id
        "time": time or _now_iso(),
        "data": data,  # C-Notification: small DTO only; no research_package
    }
