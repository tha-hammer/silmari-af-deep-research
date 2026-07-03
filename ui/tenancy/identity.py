"""Identity resolution port and the default-org bootstrap contract (B0B).

``IdentityPort`` is the fail-closed lookup the request path depends on: it
maps a SuperTokens user id (or a bootstrap owner email) to the internal
``(user_id, org_id)`` pair for the default org, returning ``None`` on ANY
failure (missing/inactive app user, missing default org, missing membership).
Routes never see the individual failure reasons — a ``None`` becomes a
``Denied`` at the ``current_run_context`` boundary.

``bootstrap_default_org`` is a pure, injectable command: it resolves every
configured owner email to a SuperTokens id BEFORE any write, so a single
unresolved email aborts the whole command with no partial writes. The real
SQL store is B6; here the store is an injected ``IdentityStore``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Protocol, cast
from uuid import UUID

from .ports import IdentityStore


class IdentityPort(Protocol):
    """Fail-closed identity resolution used by the request path and imports."""

    def resolve_active_user(
        self, supertokens_user_id: str
    ) -> tuple[UUID, UUID] | None:
        """Resolve a live session's user to ``(user_id, org_id)`` or ``None``."""
        ...

    def resolve_owner_email(self, email: str) -> tuple[UUID, UUID] | None:
        """Resolve a configured owner ``email`` to ``(user_id, org_id)`` or ``None``.

        Used by the legacy importer (B8) to attribute imported rows.
        """
        ...


class BootstrapError(RuntimeError):
    """A bootstrap owner email could not be resolved before writing."""


@dataclass(frozen=True)
class BootstrapResult:
    """Outcome of ``bootstrap_default_org`` — the org and each owner user id."""

    org_id: UUID
    owner_user_ids: list[UUID] = field(default_factory=list)


def bootstrap_default_org(
    config: Mapping[str, object], store: IdentityStore
) -> BootstrapResult:
    """Idempotently upsert the default org + configured owners + memberships.

    Resolve-before-write: every ``bootstrap_owner_emails`` entry is resolved to
    a SuperTokens id first; if any is unresolved, raise ``BootstrapError``
    before touching the store, guaranteeing no partial writes.
    """
    slug = str(config["default_org_slug"])
    name = str(config["default_org_name"])
    raw_emails = config.get("bootstrap_owner_emails") or []
    emails = list(cast("Iterable[object]", raw_emails))

    # Phase 1 — resolve ALL owners before any mutation.
    resolved: list[tuple[str, str]] = []
    for email in emails:
        st_id = store.resolve_supertokens_id(str(email))
        if st_id is None:
            raise BootstrapError(f"cannot resolve bootstrap owner email: {email!r}")
        resolved.append((str(email), st_id))

    # Phase 2 — mutate (idempotent upserts).
    org_id = store.upsert_organization(slug, name)
    owner_user_ids: list[UUID] = []
    for email, st_id in resolved:
        user_id = store.upsert_user(st_id, email)
        store.upsert_membership(org_id, user_id)
        owner_user_ids.append(user_id)

    return BootstrapResult(org_id=org_id, owner_user_ids=owner_user_ids)
