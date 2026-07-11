"""Behavior 5b — Deep-research resolves identity from the gateway-trusted
header, not its own session (C4b).

BLOCKING closure test (Workflow Closure, B5b): a request admitted through the
gateway trust check resolves deep-research's own identity from the trusted
header, not its own session cookie. Exercises the REAL ``create_app`` /
``current_run_context`` path — no stub stands in for identity resolution;
only the run repo is a fake (mirrors the existing workflow-closure test in
``test_routes.py``).
"""

from __future__ import annotations

from tests.ui._helpers import CTX_ORG, CTX_USER, SessionHolder, build_fake_deps, identity_auth, make_ref
from ui.app import create_app
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import FakeControlPlane, FakeLaunch, FakeRunRepo

SECRET = "s3cret-gateway-value"
IDENTITY = {"st_u1": (CTX_USER, CTX_ORG)}


def _app(*, session, repo=None):
    repo = repo if repo is not None else FakeRunRepo()
    deps = build_fake_deps(
        run_repo=repo,
        identity=FakeIdentity(by_supertokens=IDENTITY),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_x", "exec_x", created_at=None),
    )
    holder = SessionHolder(session)
    app = create_app(
        deps,
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=True,
    )
    return app, repo


def test_workflow_closure_trusted_header_resolves_identity_without_session(monkeypatch):
    """SOURCE: an inbound request carrying a valid X-Gateway-Secret and
    X-User-Id: st_u1, and — critically — NO SuperTokens session cookie of
    deep-research's own (``SessionHolder(None)``). TRIGGER: the real WSGI
    entrypoint via `create_app(...).test_client()` hitting a real route.
    FORBIDDEN SPAN: `current_run_context` runs for real; only the run repo
    (unrelated to identity) is faked.
    """
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    repo = FakeRunRepo()
    repo.add(make_ref("run_mine_0001", org_id=CTX_ORG, created_by=CTX_USER))
    app, repo = _app(session=None, repo=repo)  # no deep-research session cookie
    client = app.test_client()

    resp = client.get(
        "/api/runs", headers={"X-Gateway-Secret": SECRET, "X-User-Id": "st_u1"}
    )

    assert resp.status_code == 200
    run_ids = [r["run_id"] for r in resp.get_json()["runs"]]
    assert run_ids == ["run_mine_0001"]  # identity resolved to st_u1 -> CTX_USER/CTX_ORG


def test_trusted_secret_but_missing_user_id_is_401(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    app, _repo = _app(session=None)
    client = app.test_client()

    resp = client.get("/api/runs", headers={"X-Gateway-Secret": SECRET})

    assert resp.status_code == 401


def test_gateway_header_wins_over_a_present_session(monkeypatch):
    """Both a deep-research session AND a valid gateway header are present —
    the gateway header wins (single source of truth once C4 is satisfied)."""
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    repo = FakeRunRepo()
    repo.add(make_ref("run_mine_0001", org_id=CTX_ORG, created_by=CTX_USER))
    # A session for a DIFFERENT (unmapped) user would 403 if ever consulted —
    # proving the header, not the session, drove resolution.
    app, repo = _app(session=FakeSession("st_unmapped"), repo=repo)
    client = app.test_client()

    resp = client.get(
        "/api/runs", headers={"X-Gateway-Secret": SECRET, "X-User-Id": "st_u1"}
    )

    assert resp.status_code == 200
    run_ids = [r["run_id"] for r in resp.get_json()["runs"]]
    assert run_ids == ["run_mine_0001"]


def test_gateway_trust_disabled_falls_back_to_session_path(monkeypatch):
    """``enable_gateway_trust=False`` (dev/local/direct) — the existing
    session-based path is unchanged; a trusted header is NOT consulted."""
    monkeypatch.delenv("GATEWAY_SHARED_SECRET", raising=False)
    deps = build_fake_deps(
        run_repo=FakeRunRepo(),
        identity=FakeIdentity(by_supertokens=IDENTITY),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_x", "exec_x", created_at=None),
    )
    holder = SessionHolder(FakeSession("st_u1"))
    app = create_app(
        deps,
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    resp = app.test_client().get("/api/runs")
    assert resp.status_code == 200
