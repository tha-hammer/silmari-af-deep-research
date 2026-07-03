"""B0B — current_run_context fail-closed resolution."""

from __future__ import annotations

import pytest

from tests.ui._helpers import CTX_ORG, CTX_USER
from ui.tenancy.context import RunContext, current_run_context
from ui.tenancy.fakes import FakeIdentity, FakeSession
from ui.workspace.ports import Denied

_CONFIG = {"default_org_slug": "silmari-default"}


def test_valid_session_resolves_to_run_context():
    identity = FakeIdentity(by_supertokens={"st_u1": (CTX_USER, CTX_ORG)})
    ctx = current_run_context(FakeSession("st_u1"), identity, _CONFIG)
    assert isinstance(ctx, RunContext)
    assert ctx.user_id == CTX_USER
    assert ctx.org_id == CTX_ORG
    assert ctx.supertokens_user_id == "st_u1"


def test_missing_session_denies_without_identity_call():
    identity = FakeIdentity(by_supertokens={"st_u1": (CTX_USER, CTX_ORG)})
    with pytest.raises(Denied):
        current_run_context(None, identity, _CONFIG)
    assert identity.resolve_calls == []  # no lookup attempted


def test_session_with_no_user_id_denies_without_identity_call():
    identity = FakeIdentity(by_supertokens={"st_u1": (CTX_USER, CTX_ORG)})
    with pytest.raises(Denied):
        current_run_context(FakeSession(None), identity, _CONFIG)
    assert identity.resolve_calls == []


def test_unresolvable_user_denies():
    # Models: missing app user, inactive user, missing default org, OR missing
    # membership — the fake collapses all of these to a None resolution.
    identity = FakeIdentity(by_supertokens={})  # nothing resolves
    with pytest.raises(Denied):
        current_run_context(FakeSession("st_ghost"), identity, _CONFIG)
    assert identity.resolve_calls == ["st_ghost"]  # lookup attempted, then denied
