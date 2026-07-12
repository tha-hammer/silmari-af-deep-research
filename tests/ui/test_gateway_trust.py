"""Behavior 5 — Backend enforces gateway-only trust (C4).

``enable_gateway_trust=True`` (the default) installs a ``before_request`` that
runs before ANY route — including routes with no ``@auth_decorator`` — so a
direct hit to this backend is refused regardless of which endpoint it targets.
"""

from __future__ import annotations

from tests.ui._helpers import CTX_ORG, CTX_USER, SessionHolder, build_fake_deps, identity_auth
from ui.app import create_app
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import FakeControlPlane, FakeLaunch, FakeRunRepo

SECRET = "s3cret-gateway-value"


def _app(*, gateway_secret_env: str | None):
    deps = build_fake_deps(
        run_repo=FakeRunRepo(),
        identity=FakeIdentity(by_supertokens={"st_u1": (CTX_USER, CTX_ORG)}),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_x", "exec_x", created_at=None),
    )
    holder = SessionHolder(FakeSession("st_u1"))
    return create_app(
        deps,
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=True,
    )


def test_missing_secret_header_is_403(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    resp = client.get("/defaults")
    assert resp.status_code == 403


def test_mismatched_secret_header_is_403(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    resp = client.get(
        "/defaults", headers={"X-Gateway-Secret": "wrong-value", "X-User-Id": "st_u1"}
    )
    assert resp.status_code == 403


def test_matching_secret_is_processed(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    resp = client.get("/defaults", headers={"X-Gateway-Secret": SECRET, "X-User-Id": "st_u1"})
    assert resp.status_code == 200


def test_secret_unset_in_env_fails_closed(monkeypatch):
    # Edge case: an empty/unset GATEWAY_SHARED_SECRET must NOT mean "allow"
    # (RedTeam admin-token lesson) — reject even a request that happens to
    # send an empty X-Gateway-Secret too.
    monkeypatch.delenv("GATEWAY_SHARED_SECRET", raising=False)
    client = _app(gateway_secret_env=None).test_client()
    resp = client.get("/defaults", headers={"X-Gateway-Secret": "", "X-User-Id": "st_u1"})
    assert resp.status_code == 403


def test_login_page_bypasses_gateway_trust(monkeypatch):
    # /login must stay reachable WITHOUT the gateway secret so direct/local
    # login still works when this app is fronted by the gateway (fixes the
    # 2026-07-11 rollback where an unscoped gate 403'd /login and locked users
    # out). It is served by this app's own route, not SuperTokens.
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    assert client.get("/login").status_code == 200


def test_reset_password_page_bypasses_gateway_trust(monkeypatch):
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    assert client.get("/login/reset-password").status_code == 200


def test_auth_api_bypasses_gateway_trust(monkeypatch):
    # The SuperTokens /auth/* API is called directly by login.html's JS and must
    # not be 403'd by gateway trust. With enable_supertokens=False the route is
    # unmounted (404) — the point is it is NOT 403 (the gate let it through).
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    assert client.get("/auth/session/refresh").status_code != 403


def test_protected_routes_still_gated_when_auth_surface_excluded(monkeypatch):
    # Excluding the auth surface must not weaken the gate on real routes.
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    assert client.get("/defaults").status_code == 403
    assert client.get("/api/runs").status_code == 403
    assert client.get("/").status_code == 403


def test_lookalike_login_path_is_not_bypassed(monkeypatch):
    # Bypass is exact-match on the login routes + /auth prefix — a lookalike
    # such as /x/login must remain fail-closed.
    monkeypatch.setenv("GATEWAY_SHARED_SECRET", SECRET)
    client = _app(gateway_secret_env=SECRET).test_client()
    assert client.get("/x/login").status_code == 403
