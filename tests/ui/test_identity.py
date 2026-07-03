"""B0B — bootstrap_default_org contract + fake identity resolution."""

from __future__ import annotations

import pytest

from tests.ui._helpers import CTX_ORG, CTX_USER
from ui.tenancy.fakes import FakeIdentity, FakeIdentityStore
from ui.tenancy.identity import BootstrapError, bootstrap_default_org

_CONFIG = {
    "default_org_slug": "silmari-default",
    "default_org_name": "Silmari Default",
    "bootstrap_owner_emails": ["owner@example.com"],
}


def test_bootstrap_upserts_org_user_and_membership():
    store = FakeIdentityStore(known_emails={"owner@example.com": "st_owner"})
    result = bootstrap_default_org(_CONFIG, store)

    assert store.orgs == {"silmari-default": result.org_id}
    assert "st_owner" in store.users
    assert (result.org_id, store.users["st_owner"]) in store.memberships
    assert result.owner_user_ids == [store.users["st_owner"]]


def test_bootstrap_is_idempotent():
    store = FakeIdentityStore(known_emails={"owner@example.com": "st_owner"})
    r1 = bootstrap_default_org(_CONFIG, store)
    r2 = bootstrap_default_org(_CONFIG, store)

    assert r1.org_id == r2.org_id
    assert len(store.orgs) == 1
    assert len(store.users) == 1
    assert len(store.memberships) == 1


def test_bootstrap_aborts_before_writing_on_unresolved_email():
    store = FakeIdentityStore(known_emails={})  # owner email unresolvable
    with pytest.raises(BootstrapError):
        bootstrap_default_org(_CONFIG, store)
    # Resolve-before-write: nothing was upserted.
    assert store.orgs == {}
    assert store.users == {}
    assert store.memberships == set()


def test_fake_identity_resolution_paths():
    identity = FakeIdentity(
        by_supertokens={"st_u1": (CTX_USER, CTX_ORG)},
        by_email={"owner@example.com": (CTX_USER, CTX_ORG)},
    )
    assert identity.resolve_active_user("st_u1") == (CTX_USER, CTX_ORG)
    assert identity.resolve_active_user("st_ghost") is None
    assert identity.resolve_owner_email("owner@example.com") == (CTX_USER, CTX_ORG)
    assert identity.resolve_owner_email("nobody@example.com") is None
