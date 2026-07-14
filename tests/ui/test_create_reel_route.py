"""B2 (BLOCKING) — POST /api/create-reel, C7.

Closure tests: org isolation proven, empty-selection fail-closed, dispatch-failure
502 fail-BEFORE-write, server-injected principal. All e2e via ``create_app`` with
fakes — no SuperTokens, no network, no DB.
"""

from __future__ import annotations

from tests.ui._helpers import (
    CTX_ORG,
    CTX_USER,
    FIXED_NOW,
    OTHER_USER,
    SessionHolder,
    build_fake_deps,
    identity_auth,
    make_ctx,
    make_ref,
)
from ui.launch_adapter import ControlPlaneUnreachable, LaunchError
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import (
    FakeControlPlane,
    FakeLaunch,
    FakeReelDispatch,
    FakeReelJobRepo,
    FakeRunRepo,
)

U1, ORG, U2 = CTX_USER, CTX_ORG, OTHER_USER
IDENTITY = {"st_u1": (U1, ORG), "st_u2": (U2, ORG)}
SELECTION = [{"paragraphId": "2-0", "text": "alpha", "position": 4}]


def _build(*, repo=None, reel_repo=None, dispatch=None, identity=None, session="st_u1"):
    repo = repo if repo is not None else FakeRunRepo()
    reel_repo = reel_repo if reel_repo is not None else FakeReelJobRepo()
    dispatch = dispatch if dispatch is not None else FakeReelDispatch()
    idn = identity if identity is not None else FakeIdentity(by_supertokens=IDENTITY)
    holder = SessionHolder(FakeSession(session) if session else None)
    deps = build_fake_deps(
        run_repo=repo,
        identity=idn,
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_x_0001", "exec_x", created_at=FIXED_NOW),
        reel_job_repo=reel_repo,
        reel_dispatch=dispatch,
    )
    from ui.app import create_app

    app = create_app(
        deps,
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    return app.test_client(), repo, reel_repo, dispatch, holder


def _seed_run(repo, run_id="run_src_0001", *, org_id=ORG, created_by=U1):
    repo.add(make_ref(run_id, org_id=org_id, created_by=created_by,
                      execution_id="exec_src_0001", result_ref="exec_src_0001"))


# --------------------------------------------------------------------------- #
# Happy path — 202 + row written on accept
# --------------------------------------------------------------------------- #
def test_create_reel_202_dispatches_and_writes_row():
    client, repo, reel_repo, dispatch, _ = _build()
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["status"] == "queued"
    assert body["execution_id"] == "exec_reel_0001"
    assert body["job_id"]
    # dispatch happened through the control plane exactly once
    assert len(dispatch.calls) == 1
    # a reel_job row exists, status=queued, execution_id set (OBSERVABLE)
    job = reel_repo.get_by_context(make_ctx(), body["job_id"])
    assert job.status == "queued"
    assert job.execution_id == "exec_reel_0001"


def test_create_reel_defaults_client_request_id_when_omitted():
    # Regression: reel_job.client_request_id is NOT NULL in Postgres. When the
    # Create Reel UI omits clientRequestId, the server must default it, else the
    # INSERT raises NotNullViolation → unhandled 500 (observed in prod 2026-07-14).
    client, repo, reel_repo, _dispatch, _ = _build()
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 202
    job = reel_repo.get_by_context(make_ctx(), resp.get_json()["job_id"])
    assert job.client_request_id, "client_request_id must be non-null when omitted by client"


def test_create_reel_injects_server_principal_not_client_body():
    client, repo, _, dispatch, _ = _build()
    _seed_run(repo)
    # client tries to spoof identity in the body — must be ignored.
    client.post(
        "/api/create-reel",
        json={
            "selectedParagraphs": SELECTION,
            "sourceRunId": "run_src_0001",
            "userId": "spoof-user",
            "orgId": "spoof-org",
        },
    )
    payload = dispatch.calls[0]
    assert payload["userId"] == str(U1)          # server-injected
    assert payload["orgId"] == str(ORG)
    # Dispatch payload must use the reel-af reasoner's snake_case contract; the
    # REQUIRED source_execution_id is what it fetch_body()'s (its absence 422s the agent).
    assert payload["source_execution_id"] == "exec_src_0001"
    assert payload["source_package_ref"] == "exec_src_0001"  # source run's execution id
    assert payload["source_run_id"] == "run_src_0001"


# --------------------------------------------------------------------------- #
# Fail-closed validation
# --------------------------------------------------------------------------- #
def test_empty_selection_is_400_no_dispatch_no_row():
    client, repo, reel_repo, dispatch, _ = _build()
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": [], "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "empty_selection"
    assert dispatch.calls == []                    # never dispatched
    import pytest
    from ui.workspace.ports import NotFound
    with pytest.raises(NotFound):                  # no row written anywhere
        reel_repo.get_by_context(make_ctx(), "00000000-0000-0000-0000-000000000001")


def test_missing_selection_key_is_400():
    client, repo, _, dispatch, _ = _build()
    _seed_run(repo)
    resp = client.post("/api/create-reel", json={"sourceRunId": "run_src_0001"})
    assert resp.status_code == 400
    assert dispatch.calls == []


def test_bad_run_id_is_400():
    client, *_ = _build()
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "not-a-run"},
    )
    assert resp.status_code == 400


def test_absent_source_run_is_400():
    client, *_ = _build()  # run_repo empty
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_absent_0001"},
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# Org isolation (C7)
# --------------------------------------------------------------------------- #
def test_org_isolation_same_org_other_user_is_403_no_dispatch():
    client, repo, reel_repo, dispatch, _ = _build()
    _seed_run(repo, "run_u2_0001", org_id=ORG, created_by=U2)  # owned by u2
    resp = client.post(  # caller is u1
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_u2_0001"},
    )
    assert resp.status_code == 403
    assert dispatch.calls == []                    # denied BEFORE dispatch


def test_no_session_is_401():
    client, repo, *_ = _build(session=None)
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# RED-AT-SEAM (S2): dispatch failure → 502 dispatch_failed, NO row (fail-before-write)
# --------------------------------------------------------------------------- #
def test_dispatch_unreachable_is_502_and_writes_no_row():
    dispatch = FakeReelDispatch(error=ControlPlaneUnreachable("control plane unreachable"))
    client, repo, reel_repo, _, _ = _build(dispatch=dispatch)
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "dispatch_failed"
    # fail-BEFORE-write: no reel_job row was created.
    assert reel_repo._rows == {}


def test_dispatch_rejected_is_502():
    dispatch = FakeReelDispatch(error=LaunchError("rejected"))
    client, repo, reel_repo, _, _ = _build(dispatch=dispatch)
    _seed_run(repo)
    resp = client.post(
        "/api/create-reel",
        json={"selectedParagraphs": SELECTION, "sourceRunId": "run_src_0001"},
    )
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "dispatch_failed"
    assert reel_repo._rows == {}
