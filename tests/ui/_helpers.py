"""Shared, IO-free builders and constants for the ui/ unit + contract tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable
from uuid import UUID

from ui.tenancy.context import RunContext
from ui.tenancy.ports import SessionLike
from ui.workspace.research_run import ResearchRunRef, RunStatus, Visibility

# Deterministic identity fixtures.
CTX_USER = UUID("11111111-1111-1111-1111-111111111111")
CTX_ORG = UUID("22222222-2222-2222-2222-222222222222")
OTHER_USER = UUID("33333333-3333-3333-3333-333333333333")
OTHER_ORG = UUID("44444444-4444-4444-4444-444444444444")

FIXED_NOW = datetime(2026, 7, 3, 9, 40, 0, tzinfo=timezone.utc)


def make_ctx(
    user_id: UUID = CTX_USER,
    org_id: UUID = CTX_ORG,
    supertokens_user_id: str = "st_u1",
) -> RunContext:
    return RunContext(
        user_id=user_id, org_id=org_id, supertokens_user_id=supertokens_user_id
    )


def make_ref(
    run_id: str = "run_sample_0001",
    *,
    id: UUID | None = None,
    org_id: UUID = CTX_ORG,
    created_by: UUID = CTX_USER,
    query: str = "what is grounded monte carlo?",
    params: dict | None = None,
    status: RunStatus = "running",
    visibility: Visibility = "private",
    result_ref: str | None = "exec_sample_0001",
    execution_id: str | None = "exec_sample_0001",
    created_at: datetime = FIXED_NOW,
    started_at: datetime = FIXED_NOW,
    completed_at: datetime | None = None,
    duration_ms: int | None = None,
) -> ResearchRunRef:
    return ResearchRunRef(
        id=id if id is not None else UUID(int=abs(hash(run_id)) % (1 << 128)),
        run_id=run_id,
        org_id=org_id,
        created_by=created_by,
        query=query,
        params=params if params is not None else {"query": query},
        status=status,
        visibility=visibility,
        result_ref=result_ref,
        execution_id=execution_id,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
    )


class SessionHolder:
    """Mutable holder so a single app can be driven as different users.

    ``identity_auth(holder)`` reads ``holder.session`` per request, letting a
    test POST as u1 then GET as u1/u2 against the SAME ``create_app`` instance.
    """

    def __init__(self, session: SessionLike | None = None) -> None:
        self.session = session

    def __call__(self) -> SessionLike | None:
        return self.session


def identity_auth(get_session: Any) -> Callable[..., Any]:
    """A ``verify_session``-compatible fake factory for e2e route tests.

    ``get_session`` is either a session object or a zero-arg callable returning
    the current session. The returned factory mimics SuperTokens: it puts the
    session on ``g.supertokens`` and, when ``session_required`` and there is no
    session, short-circuits with 401 (the "outside" auth gate the routes rely
    on). No SuperTokens, no network.
    """

    def factory(session_required: bool = True, **_kw: Any) -> Callable[..., Any]:
        from functools import wraps

        from flask import g, jsonify

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(fn)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                session = get_session() if callable(get_session) else get_session
                g.supertokens = session
                if session_required and session is None:
                    return jsonify({"error": "unauthorized"}), 401
                return fn(*args, **kwargs)

            return wrapper

        return decorator

    return factory


def build_fake_deps(
    *,
    run_repo: Any,
    identity: Any,
    control_plane: Any,
    launch: Any,
    config: dict[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    uuid_factory: Callable[[], UUID] | None = None,
) -> Any:
    """Assemble ``AppDeps`` for e2e tests using ``g.supertokens`` as the session.

    ``session_provider`` reads ``g.supertokens`` — exactly what the injected
    ``identity_auth`` decorator sets — so production and test share one seam.
    """
    import itertools
    import logging

    from ui.app import AppDeps, default_session_provider

    cfg = config or {
        "default_org_slug": "silmari-default",
        "default_org_name": "Silmari Default",
        "bootstrap_owner_emails": ["owner@example.com"],
        "legacy_import_owner_email": "owner@example.com",
        "default_run_visibility": "private",
    }
    if uuid_factory is None:
        _counter = itertools.count(1)
        uuid_factory = lambda: UUID(int=next(_counter))  # noqa: E731
    return AppDeps(
        run_repo=run_repo,
        identity=identity,
        control_plane=control_plane,
        launch=launch,
        config=cfg,
        session_provider=default_session_provider,
        clock=clock or (lambda: FIXED_NOW),
        uuid_factory=uuid_factory,
        logger=logging.getLogger("test.ui"),
    )
