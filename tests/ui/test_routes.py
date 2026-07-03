"""B7 — Flask routes, error mapping, and the BLOCKING workflow-closure test.

All e2e via ``create_app(fake_deps, auth_decorator=identity_auth(...),
enable_supertokens=False)``. No SuperTokens, no network, no DB.
"""

from __future__ import annotations

import pytest

from tests.ui._helpers import (
    CTX_ORG,
    CTX_USER,
    FIXED_NOW,
    OTHER_USER,
    SessionHolder,
    build_fake_deps,
    identity_auth,
    make_ref,
)
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import FakeControlPlane, FakeLaunch, FakeRunRepo

# u1 and u2 share the default org; u1 is the caller in most tests.
U1, ORG, U2 = CTX_USER, CTX_ORG, OTHER_USER
IDENTITY = {"st_u1": (U1, ORG), "st_u2": (U2, ORG)}


def _build(*, repo=None, control_plane=None, launch=None, identity=None):
    repo = repo if repo is not None else FakeRunRepo()
    cp = control_plane if control_plane is not None else FakeControlPlane()
    ln = launch if launch is not None else FakeLaunch(
        "run_alpha_0001", "exec_alpha", created_at=FIXED_NOW
    )
    idn = identity if identity is not None else FakeIdentity(by_supertokens=IDENTITY)
    holder = SessionHolder(FakeSession("st_u1"))
    deps = build_fake_deps(run_repo=repo, identity=idn, control_plane=cp, launch=ln)

    from ui.app import create_app

    app = create_app(deps, auth_decorator=identity_auth(holder), enable_supertokens=False)
    return app, app.test_client(), repo, cp, ln, holder


# --------------------------------------------------------------------------- #
# /defaults regression
# --------------------------------------------------------------------------- #
def test_defaults_regression():
    import ui.app as appmod

    _, client, *_ = _build()
    resp = client.get("/defaults")
    assert resp.status_code == 200
    assert resp.get_json() == appmod.srv.DEFAULTS


# --------------------------------------------------------------------------- #
# Error mapping (result/cancel precedence)
# --------------------------------------------------------------------------- #
def test_result_401_when_no_session():
    repo = FakeRunRepo()
    holder = SessionHolder(None)  # missing session -> outside auth 401
    deps = build_fake_deps(
        run_repo=repo,
        identity=FakeIdentity(by_supertokens=IDENTITY),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_a_0001", "e", created_at=FIXED_NOW),
    )
    from ui.app import create_app

    client = create_app(
        deps, auth_decorator=identity_auth(holder), enable_supertokens=False
    ).test_client()
    assert client.get("/api/result?run=run_a_0001").status_code == 401


def test_result_403_when_context_unresolvable():
    # Session present but identity can't resolve -> Denied -> 403, no CP call.
    idn = FakeIdentity(by_supertokens={})  # st_u1 not mapped
    _, client, _, cp, *_ = _build(identity=idn)
    resp = client.get("/api/result?run=run_alpha_0001")
    assert resp.status_code == 403
    assert cp.get_calls == []


def test_result_400_on_bad_run_id():
    _, client, _, cp, *_ = _build()
    resp = client.get("/api/result?run=not-a-run")
    assert resp.status_code == 400
    assert cp.get_calls == []


def test_result_404_when_run_absent_in_org():
    _, client, _, cp, *_ = _build()
    resp = client.get("/api/result?run=run_absent_0001")
    assert resp.status_code == 404
    assert cp.get_calls == []


def test_result_403_same_org_other_user_makes_no_cp_call():
    repo = FakeRunRepo()
    repo.add(make_ref("run_u2_0001", org_id=ORG, created_by=U2))  # owned by u2
    _, client, _, cp, *_ = _build(repo=repo)
    resp = client.get("/api/result?run=run_u2_0001")  # caller is u1
    assert resp.status_code == 403
    assert cp.get_calls == []  # denied BEFORE any control-plane call


def test_cancel_403_same_org_other_user_makes_no_cp_call():
    repo = FakeRunRepo()
    repo.add(make_ref("run_u2_0002", org_id=ORG, created_by=U2))
    _, client, _, cp, *_ = _build(repo=repo)
    resp = client.post("/api/cancel", json={"run": "run_u2_0002"})
    assert resp.status_code == 403
    assert cp.cancel_calls == []  # no subprocess / no control-plane cancel


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #
def test_result_happy_path_renders_dto():
    repo = FakeRunRepo()
    repo.add(make_ref("run_ok_0001", org_id=ORG, created_by=U1, status="running"))
    cp = FakeControlPlane(
        {
            "exec_sample_0001": {
                "status": "succeeded",
                "duration_ms": 4200,
                "result": {
                    "research_package": {
                        "document_title": "T",
                        "sections": [{"title": "S", "content": "c"}],
                        "source_notes": [{"citation_id": "1", "title": "x", "url": "u"}],
                    }
                },
            }
        }
    )
    _, client, *_ = _build(repo=repo, control_plane=cp)
    resp = client.get("/api/result?run=run_ok_0001")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["run_id"] == "run_ok_0001"
    assert body["status"] == "succeeded"
    assert body["source_count"] == 1
    assert body["section_count"] == 1
    assert "markdown" in body


def test_cancel_happy_path_updates_status():
    repo = FakeRunRepo()
    repo.add(make_ref("run_cancel_0001", org_id=ORG, created_by=U1, status="running"))
    _, client, repo_out, cp, *_ = _build(repo=repo)
    resp = client.post("/api/cancel", json={"run": "run_cancel_0001"})
    assert resp.status_code == 200
    assert resp.get_json()["cancelled"] == "exec_sample_0001"
    assert cp.cancel_calls == [("exec_sample_0001", "cancelled from UI")]
    # Stored status flipped to cancelled.
    from tests.ui._helpers import make_ctx

    assert repo_out.get_by_context(make_ctx(), "run_cancel_0001").status == "cancelled"


def test_run_happy_path_returns_launch_dto():
    _, client, repo, *_ = _build()
    resp = client.post("/api/run", json={"query": "grounded monte carlo"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["run_id"] == "run_alpha_0001"
    assert body["root_execution_id"] == "exec_alpha"
    assert body["params"]["query"] == "grounded monte carlo"
    assert body["status"] == "running"


# --------------------------------------------------------------------------- #
# /api/runs never calls the unscoped srv.list_runs()
# --------------------------------------------------------------------------- #
def test_api_runs_never_calls_srv_list_runs(monkeypatch):
    import ui.app as appmod

    def _boom():
        raise AssertionError("srv.list_runs() must never be called by /api/runs")

    monkeypatch.setattr(appmod.srv, "list_runs", _boom)
    repo = FakeRunRepo()
    repo.add(make_ref("run_mine_0001", org_id=ORG, created_by=U1))
    _, client, *_ = _build(repo=repo)
    resp = client.get("/api/runs")
    assert resp.status_code == 200
    assert [r["run_id"] for r in resp.get_json()["runs"]] == ["run_mine_0001"]


# --------------------------------------------------------------------------- #
# BLOCKING Workflow Closure test
# --------------------------------------------------------------------------- #
def test_workflow_closure_launch_then_list_is_per_user():
    """POST /api/run as u1, then GET /api/runs shows it for u1 but NOT u2.

    RED-AT-SEAM: the test never seeds the repo — the ONLY way the run reaches
    ``/api/runs`` is if ``POST /api/run`` calls ``record_run_ownership`` to
    persist it. If that wiring is removed from the route, the repo stays empty,
    u1's ``/api/runs`` returns ``[]``, and the ``run_ids_u1`` assertion fails.
    """
    repo = FakeRunRepo()  # empty; NOT seeded directly
    holder = SessionHolder(FakeSession("st_u1"))
    deps = build_fake_deps(
        run_repo=repo,
        identity=FakeIdentity(by_supertokens=IDENTITY),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_closure_0001", "exec_closure", created_at=FIXED_NOW),
    )
    from ui.app import create_app

    app = create_app(deps, auth_decorator=identity_auth(holder), enable_supertokens=False)
    client = app.test_client()

    # u1 launches — observed only through the HTTP response body.
    launched = client.post("/api/run", json={"query": "closes the loop"})
    assert launched.status_code == 200
    assert launched.get_json()["run_id"] == "run_closure_0001"

    # u1 sees the run.
    holder.session = FakeSession("st_u1")
    run_ids_u1 = [r["run_id"] for r in client.get("/api/runs").get_json()["runs"]]
    assert run_ids_u1 == ["run_closure_0001"]

    # u2 (same org, different user) does NOT.
    holder.session = FakeSession("st_u2")
    run_ids_u2 = [r["run_id"] for r in client.get("/api/runs").get_json()["runs"]]
    assert run_ids_u2 == []
