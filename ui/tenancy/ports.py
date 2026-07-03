"""Tenancy boundary protocols: the request session and the bootstrap store.

``SessionLike`` is the minimal shape ``current_run_context`` needs from a
SuperTokens session (production) or a fake (tests) — only ``get_user_id``.

``IdentityStore`` is the injectable, side-effect-carrying seam that
``bootstrap_default_org`` writes through. It is a *contract*, not SQL: the
real psycopg-backed implementation lands in B6. Unit tests drive it with a
fake so the bootstrap intent (idempotent upsert; resolve-before-write) is
provable with no DB.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class SessionLike(Protocol):
    """The slice of a verified session the tenancy layer reads."""

    def get_user_id(self) -> str: ...


class IdentityStore(Protocol):
    """Idempotent upsert seam for the default-org bootstrap command (B0B).

    Every method expresses *intent* over the identity tables; the concrete
    SQL is deferred to B6. Implementations must be idempotent.
    """

    def resolve_supertokens_id(self, email: str) -> str | None:
        """Return the SuperTokens user id for ``email`` or ``None`` if unknown."""
        ...

    def upsert_organization(self, slug: str, name: str) -> UUID:
        """Create-or-return the org with ``slug``; return its internal id."""
        ...

    def upsert_user(self, supertokens_user_id: str, email: str) -> UUID:
        """Create-or-return the app user; return its internal id."""
        ...

    def upsert_membership(self, org_id: UUID, user_id: UUID) -> None:
        """Ensure ``user_id`` is an active member of ``org_id`` (idempotent)."""
        ...
