"""B0A — create_app factory + DI seam.

Proves: fake deps/auth are used, SuperTokens is skipped when disabled,
``/defaults`` is testable, and the production module-level ``app`` imports
with NO database.
"""

from __future__ import annotations

from tests.ui._helpers import (
    CTX_ORG,
    CTX_USER,
    SessionHolder,
    build_fake_deps,
    identity_auth,
)
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.fakes import FakeControlPlane, FakeLaunch, FakeRunRepo


def _deps():
    return build_fake_deps(
        run_repo=FakeRunRepo(),
        identity=FakeIdentity(by_supertokens={"st_u1": (CTX_USER, CTX_ORG)}),
        control_plane=FakeControlPlane(),
        launch=FakeLaunch("run_x", "exec_x", created_at=None),  # unused here
    )


def test_create_app_returns_flask_without_supertokens():
    from flask import Flask

    from ui.app import create_app

    holder = SessionHolder(FakeSession("st_u1"))
    app = create_app(
        _deps(),
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    assert isinstance(app, Flask)


def test_defaults_uses_injected_auth_and_serves_defaults():
    from ui.app import create_app
    import ui.app as appmod

    holder = SessionHolder(FakeSession("st_u1"))
    app = create_app(
        _deps(),
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    client = app.test_client()

    resp = client.get("/defaults")
    assert resp.status_code == 200
    payload = resp.get_json()
    # Schema passthrough plus the reel-af deep-link base (DR "Send to reels").
    assert {k: payload[k] for k in appmod.srv.DEFAULTS} == appmod.srv.DEFAULTS
    assert payload["reels_base"] == appmod.REELS_BASE_URL


def test_missing_session_is_401_from_injected_auth():
    from ui.app import create_app

    holder = SessionHolder(None)  # no session
    app = create_app(
        _deps(),
        auth_decorator=identity_auth(holder),
        enable_supertokens=False,
        enable_gateway_trust=False,
    )
    client = app.test_client()

    assert client.get("/defaults").status_code == 401


def test_production_app_imports_with_no_db():
    # Importing the module builds `app = create_app(default_deps())` with the
    # lazy psycopg repo + default-org identity — no DB connection is opened.
    import ui.app as appmod

    assert appmod.app is not None
    # The lazy repo defers a missing DSN to first use (no env var -> unavailable).
    from ui.workspace.ports import RepositoryUnavailable

    import pytest

    with pytest.raises(RepositoryUnavailable):
        appmod.make_default_run_repo().ensure_ready()
    # Identity resolves every session into the fixed default org (no DB).
    from uuid import UUID

    user_id, org_id = appmod.make_default_identity().resolve_active_user("st_u1")
    assert isinstance(user_id, UUID)
    assert isinstance(org_id, UUID)
