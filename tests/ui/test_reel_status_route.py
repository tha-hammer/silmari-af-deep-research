"""B6 (BLOCKING) — GET /api/reel-status?job=<id>.

Poll-through: read reel_job → get_execution → map + persist. Closure/red-at-seam:
an unreachable CP leaves the last-known status (never fabricates ``succeeded``);
org isolation denies a non-owner. All e2e via fakes.
"""

from __future__ import annotations

from uuid import UUID

from tests.ui._helpers import (
    CTX_ORG,
    CTX_USER,
    FIXED_NOW,
    OTHER_USER,
    SessionHolder,
    build_fake_deps,
    identity_auth,
    make_ctx,
    make_reel_job_ref,
)
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import FakeControlPlane, FakeLaunch, FakeReelJobRepo, FakeRunRepo

U1, ORG, U2 = CTX_USER, CTX_ORG, OTHER_USER
IDENTITY = {"st_u1": (U1, ORG), "st_u2": (U2, ORG)}
JOB = UUID("bbbb0000-0000-0000-0000-000000000001")


def _build(*, reel_repo=None, cp=None, session="st_u1"):
    reel_repo = reel_repo if reel_repo is not None else FakeReelJobRepo()
    cp = cp if cp is not None else FakeControlPlane()
    holder = SessionHolder(FakeSession(session) if session else None)
    deps = build_fake_deps(
        run_repo=FakeRunRepo(),
        identity=FakeIdentity(by_supertokens=IDENTITY),
        control_plane=cp,
        launch=FakeLaunch("run_x_0001", "exec_x", created_at=FIXED_NOW),
        reel_job_repo=reel_repo,
    )
    from ui.app import create_app

    app = create_app(
        deps,
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    return app.test_client(), reel_repo, cp, holder


def _seed_job(reel_repo, *, org_id=ORG, created_by=U1, status="queued",
              execution_id="exec_reel_0001"):
    reel_repo.create(
        make_reel_job_ref(id=JOB, org_id=org_id, created_by=created_by,
                          status=status, execution_id=execution_id)
    )


# --------------------------------------------------------------------------- #
# Poll-through mapping + persistence
# --------------------------------------------------------------------------- #
def test_status_maps_running_to_producing_and_persists():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo)
    cp = FakeControlPlane({"exec_reel_0001": {"status": "running"}})
    client, _, _, _ = _build(reel_repo=reel_repo, cp=cp)
    resp = client.get(f"/api/reel-status?job={JOB}")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "producing"
    # persisted transition
    assert reel_repo.get_by_context(make_ctx(), str(JOB)).status == "producing"


def test_status_succeeded_sets_reel_ref():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo)
    cp = FakeControlPlane(
        {"exec_reel_0001": {"status": "succeeded",
                            "result": {"video_path": "reel://vids/7.mp4"}}}
    )
    client, _, _, _ = _build(reel_repo=reel_repo, cp=cp)
    body = client.get(f"/api/reel-status?job={JOB}").get_json()
    assert body["status"] == "succeeded"
    assert body["reel_ref"] == "reel://vids/7.mp4"
    assert reel_repo.get_by_context(make_ctx(), str(JOB)).result_ref == "reel://vids/7.mp4"


# --------------------------------------------------------------------------- #
# RED-AT-SEAM (S5): CP unreachable → last-known, never fabricated succeeded
# --------------------------------------------------------------------------- #
def test_cp_unreachable_keeps_last_known_status():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo, status="queued")
    cp = FakeControlPlane({})  # get_execution returns None for this exec id
    client, _, _, _ = _build(reel_repo=reel_repo, cp=cp)
    body = client.get(f"/api/reel-status?job={JOB}").get_json()
    assert body["status"] == "queued"             # NOT fabricated to succeeded
    assert reel_repo.get_by_context(make_ctx(), str(JOB)).status == "queued"


def test_terminal_job_makes_no_cp_call():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo, status="succeeded")
    cp = FakeControlPlane({"exec_reel_0001": {"status": "running"}})
    client, _, _, _ = _build(reel_repo=reel_repo, cp=cp)
    body = client.get(f"/api/reel-status?job={JOB}").get_json()
    assert body["status"] == "succeeded"          # terminal is a strict no-op
    assert cp.get_calls == []


# --------------------------------------------------------------------------- #
# Org isolation + not-found + auth
# --------------------------------------------------------------------------- #
def test_same_org_other_user_is_403():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo, org_id=ORG, created_by=U2)  # owned by u2
    cp = FakeControlPlane({"exec_reel_0001": {"status": "running"}})
    client, _, _, _ = _build(reel_repo=reel_repo, cp=cp)  # caller u1
    resp = client.get(f"/api/reel-status?job={JOB}")
    assert resp.status_code == 403
    assert cp.get_calls == []                      # denied before any CP call


def test_foreign_org_is_404():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo, org_id=UUID("44444444-4444-4444-4444-444444444444"))
    client, _, _, _ = _build(reel_repo=reel_repo)
    assert client.get(f"/api/reel-status?job={JOB}").status_code == 404


def test_unknown_job_is_404():
    client, *_ = _build()
    assert client.get(f"/api/reel-status?job={JOB}").status_code == 404


def test_missing_job_arg_is_400():
    client, *_ = _build()
    assert client.get("/api/reel-status").status_code == 400


def test_no_session_is_401():
    reel_repo = FakeReelJobRepo()
    _seed_job(reel_repo)
    client, _, _, _ = _build(reel_repo=reel_repo, session=None)
    assert client.get(f"/api/reel-status?job={JOB}").status_code == 401
