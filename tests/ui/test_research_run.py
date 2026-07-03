"""Behaviors 2-5 — aggregate invariants, status model, use cases, serializers.

Pure unit tests plus Hypothesis property tests. No IO, DB, wall-clock, or
randomness (clock/uuid are injected).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.ui._helpers import (
    CTX_ORG,
    CTX_USER,
    FIXED_NOW,
    OTHER_ORG,
    OTHER_USER,
    make_ctx,
    make_ref,
)
from ui.workspace.dto import LaunchResult
from ui.workspace.fakes import (
    FakeControlPlane,
    FakeLaunch,
    FakeRunRepo,
    fixed_clock,
)
from ui.workspace.ports import Denied
from ui.workspace.research_run import (
    CP_STATUS_TO_RUN_STATUS,
    LegacyRunData,
    MapperError,
    ResearchRunRef,
    assert_run_access,
    from_legacy_json,
    from_row,
    is_valid_run_id,
    list_user_runs,
    normalize_cp_status,
    record_run_ownership,
    refresh_run_status,
    to_run_json,
)

uuids = st.uuids(version=4)


# --------------------------------------------------------------------------- #
# Aggregate invariants
# --------------------------------------------------------------------------- #
def test_valid_ref_constructs() -> None:
    ref = make_ref()
    assert ref.status == "running"
    assert ref.visibility == "private"


@pytest.mark.parametrize("bad_run_id", ["", "nope", "RUN_123", "run-123", "xrun_1"])
def test_invalid_run_id_rejected(bad_run_id: str) -> None:
    with pytest.raises(ValueError):
        make_ref(run_id=bad_run_id)


def test_empty_query_rejected() -> None:
    with pytest.raises(ValueError):
        make_ref(query="")


def test_bad_status_rejected() -> None:
    with pytest.raises(ValueError):
        make_ref(status="bogus")  # type: ignore[arg-type]


def test_bad_visibility_rejected() -> None:
    with pytest.raises(ValueError):
        make_ref(visibility="public")  # type: ignore[arg-type]


def test_negative_duration_rejected() -> None:
    with pytest.raises(ValueError):
        make_ref(duration_ms=-1)


def test_zero_duration_preserved_on_construct() -> None:
    assert make_ref(duration_ms=0).duration_ms == 0


@given(
    id=uuids,
    org_id=uuids,
    created_by=uuids,
    query=st.text(min_size=1),
)
def test_property_required_ids_present_or_raises(
    id: UUID, org_id: UUID, created_by: UUID, query: str
) -> None:
    # Valid required IDs -> constructs with those exact IDs.
    ref = make_ref(id=id, org_id=org_id, created_by=created_by, query=query)
    assert ref.id == id and ref.org_id == org_id and ref.created_by == created_by
    assert ref.query == query


@given(query=st.one_of(st.just(""), st.none()))
def test_property_missing_query_always_raises(query) -> None:
    with pytest.raises(ValueError):
        make_ref(query=query)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# with_status + duration_ms=0 regression
# --------------------------------------------------------------------------- #
def test_with_status_transitions() -> None:
    ref = make_ref(status="running")
    done = datetime(2026, 7, 3, 11, tzinfo=timezone.utc)
    updated = ref.with_status("succeeded", completed_at=done, duration_ms=42)
    assert updated.status == "succeeded"
    assert updated.completed_at == done
    assert updated.duration_ms == 42
    assert ref.status == "running"  # original untouched (frozen)


def test_with_status_keeps_existing_when_none() -> None:
    ref = make_ref(status="running", completed_at=None, duration_ms=7)
    updated = ref.with_status("failed")
    assert updated.duration_ms == 7  # kept


def test_duration_ms_zero_survives_with_status_and_json() -> None:
    ref = make_ref(status="running", duration_ms=0)
    updated = ref.with_status("succeeded", duration_ms=0)
    assert updated.duration_ms == 0
    dto = to_run_json(updated)
    assert dto["duration_ms"] == 0
    assert json.loads(json.dumps(dto))["duration_ms"] == 0
    # And keeping duration through a no-arg-ish transition preserves 0.
    kept = ref.with_status("failed")
    assert kept.duration_ms == 0


# --------------------------------------------------------------------------- #
# Status normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw,expected", list(CP_STATUS_TO_RUN_STATUS.items()))
def test_normalize_known_statuses(raw: str, expected: str) -> None:
    assert normalize_cp_status(raw) == expected


@pytest.mark.parametrize("raw", ["", None])
def test_normalize_empty_is_running(raw) -> None:
    assert normalize_cp_status(raw) == "running"


@pytest.mark.parametrize("raw", ["weird", "timeout", "unknown-thing"])
def test_normalize_unknown_nonempty_is_failed(raw: str) -> None:
    assert normalize_cp_status(raw) == "failed"


def test_normalize_case_insensitive() -> None:
    assert normalize_cp_status("SUCCEEDED") == "succeeded"
    assert normalize_cp_status("  Running  ") == "running"


def test_normalize_unknown_logs_when_logger_supplied() -> None:
    class _Rec:
        def __init__(self) -> None:
            self.calls: list = []

        def warning(self, msg, *a, **k) -> None:
            self.calls.append((msg, k))

    log = _Rec()
    assert normalize_cp_status("mystery", logger=log) == "failed"
    assert log.calls and log.calls[0][0] == "unknown_cp_status"


# --------------------------------------------------------------------------- #
# record_run_ownership
# --------------------------------------------------------------------------- #
def _launch(run_id: str = "run_launch_1", query: str = "q?") -> LaunchResult:
    return LaunchResult(
        run_id=run_id,
        root_execution_id="exec_launch_1",
        created_at=FIXED_NOW,
        status="running",
        node="meta_deep_research",
        reasoner="execute_deep_research",
        params={"query": query, "depth": 3},
    )


def test_record_run_ownership_persists_scoped_ref() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    fixed_id = UUID("99999999-9999-9999-9999-999999999999")
    ref = record_run_ownership(
        ctx, _launch(query="grounded monte carlo"), repo,
        clock=fixed_clock(FIXED_NOW), uuid_factory=lambda: fixed_id,
    )
    assert ref.id == fixed_id
    assert ref.org_id == ctx.org_id
    assert ref.created_by == ctx.user_id
    assert ref.query == "grounded monte carlo"
    assert ref.visibility == "private"
    assert ref.result_ref == "exec_launch_1"
    assert ref.execution_id == "exec_launch_1"
    assert ref.status == "running"
    # persisted and retrievable
    assert repo.get_by_context(ctx, ref.run_id) == ref


def test_record_run_ownership_normalizes_status() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    launch = replace(_launch(), status="queued")  # type: ignore[arg-type]
    ref = record_run_ownership(
        ctx, launch, repo, clock=fixed_clock(FIXED_NOW), uuid_factory=uuid4
    )
    assert ref.status == "running"


# --------------------------------------------------------------------------- #
# list_user_runs (Behavior 3)
# --------------------------------------------------------------------------- #
def test_list_user_runs_scopes_and_orders() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    repo.add(make_ref(run_id="run_mine_old",
                      created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                      started_at=datetime(2026, 1, 1, tzinfo=timezone.utc)))
    repo.add(make_ref(run_id="run_mine_new",
                      created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                      started_at=datetime(2026, 6, 1, tzinfo=timezone.utc)))
    repo.add(make_ref(run_id="run_other_user", created_by=OTHER_USER))
    repo.add(make_ref(run_id="run_other_org", org_id=OTHER_ORG))
    got = [r.run_id for r in list_user_runs(ctx, repo)]
    assert got == ["run_mine_new", "run_mine_old"]


@given(
    n_mine=st.integers(min_value=0, max_value=6),
    n_other_user=st.integers(min_value=0, max_value=6),
    n_other_org=st.integers(min_value=0, max_value=6),
)
def test_property_list_scope_safety(
    n_mine: int, n_other_user: int, n_other_org: int
) -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    for i in range(n_mine):
        repo.add(make_ref(run_id=f"run_mine_{i}"))
    for i in range(n_other_user):
        repo.add(make_ref(run_id=f"run_ou_{i}", created_by=OTHER_USER))
    for i in range(n_other_org):
        repo.add(make_ref(run_id=f"run_oo_{i}", org_id=OTHER_ORG))
    result = list_user_runs(ctx, repo)
    assert len(result) == n_mine
    for ref in result:
        assert ref.org_id == ctx.org_id
        assert ref.created_by == ctx.user_id


# --------------------------------------------------------------------------- #
# refresh_run_status (Behavior 4)
# --------------------------------------------------------------------------- #
def test_refresh_updates_from_control_plane() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    ref = make_ref(run_id="run_refresh", status="running", execution_id="exec_r")
    repo.add(ref)
    done = "2026-07-03T12:00:00Z"
    cp = FakeControlPlane({"exec_r": {"status": "completed", "completed_at": done,
                                      "duration_ms": 5000}})
    updated = refresh_run_status(ref, ctx, repo, cp)
    assert updated.status == "succeeded"
    assert updated.duration_ms == 5000
    assert updated.completed_at == datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)
    assert cp.get_calls == ["exec_r"]
    assert repo.get_by_context(ctx, "run_refresh").status == "succeeded"


@pytest.mark.parametrize("raw", ["pending", "queued", "registered", "submitted", ""])
def test_refresh_nonterminal_maps_to_running(raw: str) -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    ref = make_ref(run_id="run_np", status="running", execution_id="exec_np")
    repo.add(ref)
    cp = FakeControlPlane({"exec_np": {"status": raw}})
    updated = refresh_run_status(ref, ctx, repo, cp)
    assert updated.status == "running"


def test_refresh_preserves_duration_zero() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    ref = make_ref(run_id="run_z", status="running", execution_id="exec_z")
    repo.add(ref)
    cp = FakeControlPlane({"exec_z": {"status": "succeeded", "duration_ms": 0}})
    updated = refresh_run_status(ref, ctx, repo, cp)
    assert updated.duration_ms == 0


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_refresh_terminal_is_noop(terminal: str) -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    ref = make_ref(run_id="run_term", status=terminal, execution_id="exec_t")  # type: ignore[arg-type]
    repo.add(ref)
    cp = FakeControlPlane({"exec_t": {"status": "running"}})
    result = refresh_run_status(ref, ctx, repo, cp)
    assert result is ref
    assert cp.get_calls == []  # no CP call
    assert cp.cancel_calls == []


def test_refresh_no_payload_is_noop() -> None:
    ctx = make_ctx()
    repo = FakeRunRepo()
    ref = make_ref(run_id="run_missing_cp", status="running", execution_id="exec_x")
    repo.add(ref)
    cp = FakeControlPlane({})  # returns None
    result = refresh_run_status(ref, ctx, repo, cp)
    assert result == ref
    assert cp.get_calls == ["exec_x"]  # called, but no write
    assert repo.get_by_context(ctx, "run_missing_cp").status == "running"


# --------------------------------------------------------------------------- #
# assert_run_access (Behavior 5)
# --------------------------------------------------------------------------- #
def test_access_granted_when_both_match() -> None:
    ctx = make_ctx()
    assert assert_run_access(make_ref(org_id=ctx.org_id, created_by=ctx.user_id), ctx) is None


def test_access_denied_foreign_org() -> None:
    ctx = make_ctx()
    with pytest.raises(Denied):
        assert_run_access(make_ref(org_id=OTHER_ORG, created_by=ctx.user_id), ctx)


def test_access_denied_foreign_creator() -> None:
    ctx = make_ctx()
    with pytest.raises(Denied):
        assert_run_access(make_ref(org_id=ctx.org_id, created_by=OTHER_USER), ctx)


def test_access_denied_when_none() -> None:
    with pytest.raises(Denied):
        assert_run_access(None, make_ctx())


@given(org_id=uuids, created_by=uuids)
def test_property_guard_safety(org_id: UUID, created_by: UUID) -> None:
    ctx = make_ctx(user_id=CTX_USER, org_id=CTX_ORG)
    ref = make_ref(org_id=org_id, created_by=created_by)
    if org_id == CTX_ORG and created_by == CTX_USER:
        assert assert_run_access(ref, ctx) is None
    else:
        with pytest.raises(Denied):
            assert_run_access(ref, ctx)


# --------------------------------------------------------------------------- #
# Serializers: to_run_json / from_row / from_legacy_json
# --------------------------------------------------------------------------- #
def test_to_run_json_preserves_fields() -> None:
    done = datetime(2026, 7, 3, 13, tzinfo=timezone.utc)
    ref = make_ref(status="succeeded", completed_at=done, duration_ms=1234,
                   result_ref="exec_abc")
    dto = to_run_json(ref)
    assert dto["run_id"] == ref.run_id
    assert dto["root_execution_id"] == "exec_abc"
    assert dto["status"] == "succeeded"
    assert dto["completed_at"] == done.isoformat()
    assert dto["duration_ms"] == 1234
    assert dto["params"] == dict(ref.params)


def test_from_row_roundtrips_to_run_json() -> None:
    ref = make_ref(run_id="run_row_1")
    row = {
        "id": ref.id,
        "run_id": ref.run_id,
        "org_id": ref.org_id,
        "created_by": ref.created_by,
        "query": ref.query,
        "params": dict(ref.params),
        "status": ref.status,
        "visibility": ref.visibility,
        "result_ref": ref.result_ref,
        "execution_id": ref.execution_id,
        "created_at": ref.created_at,
        "started_at": ref.started_at,
        "completed_at": None,
        "duration_ms": None,
    }
    assert from_row(row) == ref


def test_from_row_accepts_string_uuids_and_iso_timestamps() -> None:
    row = {
        "id": str(CTX_USER),
        "run_id": "run_row_2",
        "org_id": str(CTX_ORG),
        "created_by": str(CTX_USER),
        "query": "q",
        "params": {"query": "q"},
        "status": "running",
        "visibility": "private",
        "result_ref": "exec_1",
        "execution_id": "exec_1",
        "created_at": "2026-07-03T09:40:00+00:00",
        "started_at": "2026-07-03T09:40:00Z",
        "completed_at": None,
        "duration_ms": 0,
    }
    ref = from_row(row)
    assert ref.duration_ms == 0
    assert ref.created_at == FIXED_NOW


@pytest.mark.parametrize(
    "mutate",
    [
        {"id": "not-a-uuid"},
        {"status": "bogus"},
        {"visibility": "public"},
        {"created_at": "not-a-date"},
        {"params": [1, 2, 3]},
        {"duration_ms": -5},
        {"run_id": "bad id"},
    ],
)
def test_from_row_rejects_bad_rows(mutate: dict) -> None:
    row = {
        "id": str(CTX_USER),
        "run_id": "run_row_bad",
        "org_id": str(CTX_ORG),
        "created_by": str(CTX_USER),
        "query": "q",
        "params": {"query": "q"},
        "status": "running",
        "visibility": "private",
        "result_ref": None,
        "execution_id": None,
        "created_at": "2026-07-03T09:40:00+00:00",
        "started_at": "2026-07-03T09:40:00+00:00",
        "completed_at": None,
        "duration_ms": None,
    }
    row.update(mutate)
    with pytest.raises(MapperError):
        from_row(row)


def test_from_legacy_json_valid() -> None:
    payload = {
        "run_id": "run_20260702_002007_fx389r62",
        "root_execution_id": "exec_20260702_002007_tqvvjd1p",
        "created_at": "2026-07-02T00:20:07+00:00",
        "status": "succeeded",
        "node": "meta_deep_research",
        "reasoner": "execute_deep_research",
        "params": {"query": "Grounded Monte Carlo research.", "research_focus": 5},
        "completed_at": "2026-07-02T00:59:31Z",
        "duration_ms": 2364720,
    }
    data = from_legacy_json("ui/runs/run_20260702_002007_fx389r62.json", payload)
    assert isinstance(data, LegacyRunData)
    assert data.run_id == "run_20260702_002007_fx389r62"
    assert data.query == "Grounded Monte Carlo research."
    assert data.status == "succeeded"
    assert data.duration_ms == 2364720
    assert data.root_execution_id == "exec_20260702_002007_tqvvjd1p"


def test_from_legacy_json_filename_mismatch_rejected() -> None:
    payload = {"run_id": "run_aaa", "created_at": "2026-07-02T00:20:07Z",
               "params": {"query": "q"}, "status": "succeeded"}
    with pytest.raises(MapperError):
        from_legacy_json("ui/runs/run_bbb.json", payload)


def test_from_legacy_json_missing_query_rejected() -> None:
    payload = {"run_id": "run_noq", "created_at": "2026-07-02T00:20:07Z",
               "params": {"research_focus": 5}, "status": "succeeded"}
    with pytest.raises(MapperError):
        from_legacy_json("run_noq.json", payload)


def test_from_legacy_json_bad_run_id_rejected() -> None:
    payload = {"run_id": "nope", "created_at": "2026-07-02T00:20:07Z",
               "params": {"query": "q"}, "status": "succeeded"}
    with pytest.raises(MapperError):
        from_legacy_json("nope.json", payload)


def test_from_legacy_json_unknown_status_normalized_to_failed() -> None:
    payload = {"run_id": "run_weird", "created_at": "2026-07-02T00:20:07Z",
               "params": {"query": "q"}, "status": "explode"}
    data = from_legacy_json("run_weird.json", payload)
    assert data.status == "failed"


def test_is_valid_run_id_matches_server_regex() -> None:
    assert is_valid_run_id("run_20260702_002007_fx389r62")
    assert not is_valid_run_id("")
    assert not is_valid_run_id("exec_1")
    assert not is_valid_run_id(None)
