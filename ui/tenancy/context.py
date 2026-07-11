"""Tenancy request context.

The ``RunContext`` value object plus the fail-closed ``current_run_context``
resolver (Behavior 0B). The ``IdentityPort`` protocol itself lives in
``identity.py``; this module only *consumes* it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping
from uuid import UUID

# Additive imports (B0B). ``Denied`` is defined in the workspace port layer,
# which imports nothing from this module at runtime (its back-reference is
# TYPE_CHECKING-only), so this creates no import cycle.
from ui.workspace.ports import Denied

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .identity import IdentityPort
    from .ports import SessionLike


@dataclass(frozen=True)
class RunContext:
    """The resolved, org-scoped identity for a single request.

    ``user_id`` and ``org_id`` are the internal ``deepresearch.user.id`` and
    ``deepresearch.organization.id`` UUIDs. ``supertokens_user_id`` is retained
    for diagnostics/logging only; it is never used as a scoping key.
    """

    user_id: UUID
    org_id: UUID
    supertokens_user_id: str


def current_run_context(
    session: "SessionLike | None",
    identity: "IdentityPort",
    config: Mapping[str, object],
    trusted_user_id: str | None = None,
) -> RunContext:
    """Resolve the active, org-scoped ``RunContext`` or fail closed.

    Raises ``Denied`` — performing NO app-data or control-plane calls — when:

    - ``trusted_user_id`` is not given, and the session is absent or exposes
      no user id; or
    - identity cannot resolve the SuperTokens id to an active app user with
      default-org membership.

    ``trusted_user_id`` (Behavior 5b, C4b): when given, identity is resolved
    from it directly and the SuperTokens session is NOT consulted — the
    gateway-trusted header wins, single source of truth, once the gateway's
    admission check (C4) has already validated the caller. This is the seam
    that closes the plan's headline gap: without it, a request proxied
    through the gateway would independently fail this app's own session
    check (the browser only ever holds a cookie scoped to the gateway's own
    host — this app's host never receives one).

    The only external call made here is ``identity.resolve_active_user`` — the
    identity lookup itself. On any failure the caller gets ``Denied`` before a
    single repo/CP call is issued.
    """
    if trusted_user_id is not None:
        supertokens_user_id = trusted_user_id
    else:
        if session is None:
            raise Denied("no session")
        supertokens_user_id = session.get_user_id()
        if not supertokens_user_id:
            raise Denied("session has no user id")
    resolved = identity.resolve_active_user(supertokens_user_id)
    if resolved is None:
        raise Denied("no active app user / default-org membership")
    user_id, org_id = resolved
    return RunContext(
        user_id=user_id,
        org_id=org_id,
        supertokens_user_id=supertokens_user_id,
    )
