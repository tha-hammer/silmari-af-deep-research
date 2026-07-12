"""B6 (producer half): the ``research.completed`` envelope + small DTO shape is
pinned to the SHARED golden fixture that the reel-af consumer also pins to.

``build_research_completed`` fed the golden's terminal execution (with the
golden's injected ``id``/``time``) MUST reproduce the golden CloudEvent exactly.
A producer change that copies the body into ``data``, drops a DTO field, or
renames the envelope fails here loudly.

A cross-repo drift check asserts the producer's fixture copy is byte-for-byte
identical to the canonical consumer copy in the sibling ``carousel-impl`` repo,
so the two pins cannot silently diverge.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_completed_event import build_research_completed

# Non-deterministic envelope fields pinned from the golden fixture so the build
# is reproducible.
_GOLDEN_EVENT_ID = "ce-0f9b2c7a-4d1e-4a2b-9c3d-000000000001"
_GOLDEN_TIME = "2026-07-12T18:00:00Z"

# Canonical consumer-side location of the shared golden fixture. The producer
# repo is nested at ``ntm_Dev/silmari-agentfield-system/silmari-af-deep-research``;
# ``carousel-impl`` is a sibling under ``ntm_Dev`` (parents[4] from this file).
_CANONICAL = (
    Path(__file__).resolve().parents[4]
    / "carousel-impl"
    / "tests"
    / "web"
    / "fixtures"
    / "research_completed.cloudevent.json"
)


def test_builder_reproduces_the_golden_cloudevent(
    terminal_execution, golden_cloudevent
):
    built = build_research_completed(
        terminal_execution, event_id=_GOLDEN_EVENT_ID, time=_GOLDEN_TIME
    )
    assert built == golden_cloudevent


def test_golden_data_is_small_dto_with_no_body(golden_cloudevent):
    assert "research_package" not in golden_cloudevent["data"]  # C-Notification
    assert set(golden_cloudevent["data"].keys()) == {
        "run_id",
        "status",
        "title",
        "result_ref",
        "research_prompt",
        "research_document_id",
    }


def test_golden_subject_is_execution_id(golden_cloudevent):
    assert (
        golden_cloudevent["subject"]
        == golden_cloudevent["data"]["research_document_id"]
    )


def test_producer_fixture_matches_canonical_consumer_copy(fixtures_dir: Path):
    """Byte-for-byte drift check across the two repos (B6 fixture sharing)."""
    if not _CANONICAL.exists():
        pytest.skip(f"canonical consumer fixture not present at {_CANONICAL}")
    producer_copy = (fixtures_dir / "research_completed.cloudevent.json").read_bytes()
    canonical = _CANONICAL.read_bytes()
    assert producer_copy == canonical, (
        "producer and consumer golden fixtures have drifted; re-sync "
        f"{fixtures_dir / 'research_completed.cloudevent.json'} with {_CANONICAL}"
    )
