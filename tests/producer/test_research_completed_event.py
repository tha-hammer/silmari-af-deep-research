"""B1 producer unit tests: ``build_research_completed`` envelope + small DTO.

Asserts the CloudEvents envelope shape, every DTO field's source, that
``subject == execution_id`` (C-Correlation), that the document body is ABSENT
from ``data`` (C-Notification), and that the event is emitted for ``succeeded``
runs only.
"""

from __future__ import annotations

import pytest

from research_completed_event import (
    EVENT_SOURCE,
    EVENT_TYPE_RESEARCH_COMPLETED,
    RESEARCH_PACKAGE_KEY,
    build_research_completed,
)

_EXPECTED_DTO_KEYS = {
    "run_id",
    "status",
    "title",
    "result_ref",
    "research_prompt",
    "research_document_id",
}


# ─────────────────────────── envelope shape ───────────────────────────


def test_envelope_type_and_source(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["type"] == EVENT_TYPE_RESEARCH_COMPLETED
    assert event["source"] == EVENT_SOURCE


def test_subject_is_execution_id(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["subject"] == terminal_execution["execution_id"]  # C-Correlation


def test_id_present_and_prefixed_when_not_injected(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["id"]  # non-empty, unique per emit
    assert event["id"].startswith("ce-")


def test_time_present_when_not_injected(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["time"]
    assert event["time"].endswith("Z")


def test_injected_id_and_time_are_used(terminal_execution):
    event = build_research_completed(
        terminal_execution, event_id="ce-fixed", time="2026-07-12T18:00:00Z"
    )
    assert event["id"] == "ce-fixed"
    assert event["time"] == "2026-07-12T18:00:00Z"


# ─────────────────────────── DTO field sources ───────────────────────────


def test_data_has_exactly_the_dto_keys(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert set(event["data"].keys()) == _EXPECTED_DTO_KEYS


def test_research_prompt_from_metadata_query(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert (
        event["data"]["research_prompt"]
        == terminal_execution["result"]["metadata"]["query"]
    )


def test_title_from_metadata_title(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["data"]["title"] == terminal_execution["result"]["metadata"]["title"]


def test_research_document_id_is_execution_id(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["data"]["research_document_id"] == terminal_execution["execution_id"]


def test_result_ref_is_execution_id_keyed(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert (
        event["data"]["result_ref"]
        == f"cp-execution://{terminal_execution['execution_id']}/result"
    )


def test_run_id_and_status_from_run(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert event["data"]["run_id"] == terminal_execution["run_id"]
    assert event["data"]["status"] == "succeeded"


# ─────────────────────────── C-Notification: no body ───────────────────────────


def test_research_package_body_absent_from_data(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert RESEARCH_PACKAGE_KEY not in event["data"]  # C-Notification


def test_owner_notes_never_copied_anywhere(terminal_execution):
    event = build_research_completed(terminal_execution)
    assert "notes" not in event
    assert "notes" not in event["data"]


# ─────────────────────────── succeeded-only emit ───────────────────────────


@pytest.mark.parametrize("bad_status", ["failed", "cancelled", "running", "", None])
def test_emitted_for_succeeded_only(terminal_execution, bad_status):
    terminal_execution["status"] = bad_status
    with pytest.raises(ValueError):
        build_research_completed(terminal_execution)


# ─────────────────────────── nullable snapshot fields ───────────────────────────


def test_title_none_when_metadata_title_absent(terminal_execution):
    terminal_execution["result"]["metadata"].pop("title")
    event = build_research_completed(terminal_execution)
    assert event["data"]["title"] is None  # snapshot convenience, not the key


def test_research_prompt_none_when_metadata_query_absent(terminal_execution):
    terminal_execution["result"]["metadata"].pop("query")
    event = build_research_completed(terminal_execution)
    assert event["data"]["research_prompt"] is None


def test_valid_when_result_missing_entirely(terminal_execution):
    # A succeeded run whose result snapshot is empty still builds a valid
    # envelope; title/prompt degrade to None, correlation keys are intact.
    terminal_execution.pop("result")
    event = build_research_completed(terminal_execution)
    assert event["subject"] == terminal_execution["execution_id"]
    assert event["data"]["title"] is None
    assert event["data"]["research_prompt"] is None
    assert RESEARCH_PACKAGE_KEY not in event["data"]
