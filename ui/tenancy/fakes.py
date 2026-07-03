"""In-memory, IO-free fakes for the tenancy ports.

Used by unit tests and the Flask e2e factory. No IO, network, DB, wall-clock,
or randomness. They enforce the same fail-closed contract as the (B6) real
adapters: an unknown session/email resolves to ``None``.
"""

from __future__ import annotations

from uuid import UUID


class FakeSession:
    """A verified-session stand-in exposing only ``get_user_id``.

    ``user_id=None`` models a session with no resolvable user (still a
    "present" session object, but one that must fail closed downstream).
    """

    def __init__(self, user_id: str | None) -> None:
        self._user_id = user_id

    def get_user_id(self) -> str:
        return self._user_id or ""


class FakeIdentity:
    """Fake ``IdentityPort`` backed by explicit mappings.

    ``by_supertokens`` maps a SuperTokens id to ``(user_id, org_id)``;
    ``by_email`` maps an owner email the same way. Anything absent resolves to
    ``None`` (fail closed). Every resolution is recorded for call-count asserts.
    """

    def __init__(
        self,
        by_supertokens: dict[str, tuple[UUID, UUID]] | None = None,
        by_email: dict[str, tuple[UUID, UUID]] | None = None,
    ) -> None:
        self._by_st = dict(by_supertokens or {})
        self._by_email = dict(by_email or {})
        self.resolve_calls: list[str] = []
        self.email_calls: list[str] = []

    def resolve_active_user(
        self, supertokens_user_id: str
    ) -> tuple[UUID, UUID] | None:
        self.resolve_calls.append(supertokens_user_id)
        return self._by_st.get(supertokens_user_id)

    def resolve_owner_email(self, email: str) -> tuple[UUID, UUID] | None:
        self.email_calls.append(email)
        return self._by_email.get(email)


class FakeIdentityStore:
    """Fake ``IdentityStore`` for ``bootstrap_default_org`` tests.

    Tracks idempotent upserts so tests can assert no duplicate orgs/users are
    created on a second bootstrap run. Emails not present in ``known_emails``
    resolve to ``None`` (drives the resolve-before-write abort path).
    """

    def __init__(self, known_emails: dict[str, str] | None = None) -> None:
        # email -> supertokens id
        self._known_emails = dict(known_emails or {})
        self.orgs: dict[str, UUID] = {}
        self.users: dict[str, UUID] = {}  # supertokens id -> user id
        self.memberships: set[tuple[UUID, UUID]] = set()
        self._next = 0

    def _mint(self) -> UUID:
        self._next += 1
        return UUID(int=self._next)

    def resolve_supertokens_id(self, email: str) -> str | None:
        return self._known_emails.get(email)

    def upsert_organization(self, slug: str, name: str) -> UUID:
        if slug not in self.orgs:
            self.orgs[slug] = self._mint()
        return self.orgs[slug]

    def upsert_user(self, supertokens_user_id: str, email: str) -> UUID:
        if supertokens_user_id not in self.users:
            self.users[supertokens_user_id] = self._mint()
        return self.users[supertokens_user_id]

    def upsert_membership(self, org_id: UUID, user_id: UUID) -> None:
        self.memberships.add((org_id, user_id))
