#!/usr/bin/env python3
"""
Deep Research UI — Flask + SuperTokens front end (dependency-injected factory).

``create_app(deps, auth_decorator, enable_supertokens)`` registers every route
against injected dependencies so tests can drive the whole HTTP surface with
in-memory fakes, no SuperTokens, and no database. Production keeps a
module-level ``app = create_app(default_deps())``.

CRITICAL: importing this module must NOT open a DB connection or require the
schema. ``default_deps()`` wires the real control-plane + launch adapters, but
the run repository and identity resolver are *unprovisioned placeholders*
(the psycopg adapter is Behavior 6) that raise ``RepositoryUnavailable`` only
when a method is actually called.

Env:
  SUPERTOKENS_CONNECTION_URI / CONNECTION_URI   SuperTokens Core URL (private)
  SUPERTOKENS_API_KEY        / API_KEY          SuperTokens Core API key
  UI_WEBSITE_DOMAIN                              public URL of THIS UI (for cookies)
  DR_UI_PORT (default 8899), UI_BIND_HOST (default :: for Railway private net)
  AGENTFIELD_SERVER, AGENTFIELD_API_KEY         (read by server.py)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from flask import Flask, Response, g, jsonify, redirect, request, send_from_directory
from supertokens_python.recipe.session.framework.flask import verify_session

# server.py holds the pure control-plane helpers. Import whichever way the
# runtime layout allows (top-level under Docker/PYTHONPATH=ui; ``ui.server``
# under the repo-root test/import layout).
try:  # pragma: no cover - import shim
    import server as srv  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - import shim
    from ui import server as srv  # type: ignore[no-redef]

from ui.launch_adapter import (
    ControlPlaneLaunch,
    ControlPlaneUnreachable,
    LaunchError,
    LaunchResponseInvalid,
)
from ui.tenancy.context import RunContext, current_run_context
from ui.tenancy.identity import IdentityPort
from ui.tenancy.ports import SessionLike
from ui.workspace.dto import JSONValue, LaunchRunDTO, ResearchResultDTO
from ui.workspace.ports import (
    CancelResult,
    ControlPlanePort,
    Conflict,
    Denied,
    ExecutionPayload,
    NotFound,
    RepositoryUnavailable,
    RunRepo,
)
from ui.workspace.postgres.repository import ResearchRunRepository
from ui.workspace.research_run import (
    ResearchRunRef,
    assert_run_access,
    list_user_runs,
    record_run_ownership,
    refresh_run_status,
    to_run_json,
)

# --------------------------------------------------------------------------- #
# Env / static config (no SuperTokens, no DB — safe at import)
# --------------------------------------------------------------------------- #
BASE = srv.BASE
PORT = int(os.environ.get("DR_UI_PORT", "8899"))
HOST = os.environ.get("UI_BIND_HOST", "::")  # dual-stack for Railway private net
WEBSITE_DOMAIN = os.environ.get(
    "UI_WEBSITE_DOMAIN", f"http://localhost:{PORT}"
).rstrip("/")
ST_CONN = os.environ.get(
    "SUPERTOKENS_CONNECTION_URI", os.environ.get("CONNECTION_URI", "http://localhost:3567")
).rstrip("/")
ST_KEY = os.environ.get("SUPERTOKENS_API_KEY", os.environ.get("API_KEY", "")) or None

CONFIG_PATH = os.path.join(BASE, "config.json")
with open(CONFIG_PATH) as _cf:
    CONFIG = json.load(_cf)

# Only these emails may register. Empty = open registration (NOT recommended).
ALLOWED_EMAILS = {
    e.strip().lower() for e in CONFIG.get("allowed_emails", []) if e.strip()
}

TENANCY_CONFIG_PATH = os.path.join(BASE, "config", "tenancy.json")


def _load_tenancy_config() -> Mapping[str, Any]:
    with open(TENANCY_CONFIG_PATH) as fh:
        data: dict[str, Any] = json.load(fh)
    return data


# --------------------------------------------------------------------------- #
# Dependency bundle
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AppDeps:
    """Everything ``create_app`` needs, injectable end-to-end."""

    run_repo: RunRepo
    identity: IdentityPort
    control_plane: ControlPlanePort
    launch: Callable[[Mapping[str, JSONValue]], Any]
    config: Mapping[str, Any]
    session_provider: Callable[[], SessionLike | None]
    clock: Callable[[], datetime]
    uuid_factory: Callable[[], UUID]
    logger: logging.Logger


def default_session_provider() -> SessionLike | None:
    """Production session accessor: the session SuperTokens put on ``g``."""
    return getattr(g, "supertokens", None)


# --------------------------------------------------------------------------- #
# Real persistence + identity wiring (Behavior 6, thin slice)
# --------------------------------------------------------------------------- #
class DefaultOrgIdentity:
    """Trivial ``IdentityPort``: every session maps into the fixed default org.

    SuperTokens ids are UUIDs, so ``created_by`` is ``UUID(st_user_id)`` when
    parseable; a non-UUID id is deterministically hashed via ``uuid5`` so the
    mapping is stable. ``org_id`` is always the configured default org. No DB.
    """

    def __init__(self, default_org_id: UUID) -> None:
        self._org_id = default_org_id

    def resolve_active_user(
        self, supertokens_user_id: str
    ) -> tuple[UUID, UUID] | None:
        try:
            user_id = UUID(supertokens_user_id)
        except (ValueError, AttributeError, TypeError):
            user_id = uuid5(NAMESPACE_URL, supertokens_user_id)
        return (user_id, self._org_id)

    def resolve_owner_email(self, email: str) -> tuple[UUID, UUID] | None:
        # Legacy import attribution is out of scope for the thin slice.
        return None


def make_default_run_repo() -> RunRepo:
    """Lazy psycopg repo. No connect at import; a missing env var defers to use."""

    def _dsn() -> str:
        dsn = os.environ.get("DEEPRESEARCH_DATABASE_URL")
        if not dsn:
            raise RepositoryUnavailable("DEEPRESEARCH_DATABASE_URL is not set")
        return dsn

    return ResearchRunRepository(_dsn)


def make_default_identity(config: Mapping[str, Any] | None = None) -> IdentityPort:
    """Build the default-org identity resolver from tenancy config (no DB)."""
    cfg = config if config is not None else _load_tenancy_config()
    return DefaultOrgIdentity(UUID(str(cfg["default_org_id"])))


# --------------------------------------------------------------------------- #
# Real control-plane adapter (reuses server.py HTTP helpers)
# --------------------------------------------------------------------------- #
class ControlPlaneHTTP:
    """``ControlPlanePort`` over the AgentField control plane via ``server``."""

    def __init__(self, srv_module: Any = srv) -> None:
        self._srv = srv_module

    def get_execution(self, execution_id: str) -> ExecutionPayload | None:
        payload = self._srv.cp_get(f"/executions/{execution_id}")
        return payload  # type: ignore[return-value]

    def cancel_execution(
        self, execution_id: str, reason: str
    ) -> CancelResult | None:
        res = self._srv.cp_post(
            f"/executions/{execution_id}/cancel", {"reason": reason}
        )
        if res is None:
            return None
        if res.get("_error"):
            return CancelResult(
                cancelled=False, execution_id=execution_id, error=res["_error"]
            )
        return CancelResult(cancelled=True, execution_id=execution_id)


def default_deps() -> AppDeps:
    """Wire production dependencies. No DB connection is opened here."""
    config = _load_tenancy_config()
    return AppDeps(
        run_repo=make_default_run_repo(),
        identity=make_default_identity(config),
        control_plane=ControlPlaneHTTP(srv),
        launch=ControlPlaneLaunch(srv),
        config=config,
        session_provider=default_session_provider,
        clock=lambda: datetime.now(timezone.utc),
        uuid_factory=uuid4,
        logger=logging.getLogger("ui.app"),
    )


# --------------------------------------------------------------------------- #
# Result serialization (workspace DTOs are frozen; assembled here)
# --------------------------------------------------------------------------- #
def _build_result_dto(
    ref: ResearchRunRef, payload: ExecutionPayload | None
) -> ResearchResultDTO:
    """Assemble the ``/api/result`` DTO from a ref + control-plane payload."""
    if not payload:
        return ResearchResultDTO(
            status="unknown",
            run_id=ref.run_id,
            params=dict(ref.params),
            duration_ms=ref.duration_ms,
            source_count=0,
            section_count=0,
            error="no execution payload",
        )
    status = payload.get("status", "unknown")
    result = payload.get("result") or {}
    rp = result.get("research_package") if isinstance(result, Mapping) else None
    out: ResearchResultDTO = ResearchResultDTO(
        status=status,
        run_id=ref.run_id,
        params=dict(ref.params),
        duration_ms=payload.get("duration_ms"),
        source_count=len((rp or {}).get("source_notes", [])) if rp else 0,
        section_count=len((rp or {}).get("sections", [])) if rp else 0,
    )
    if rp:
        md = srv.render_markdown(rp, result.get("metadata", {}))
        out["markdown"] = md
        try:
            import markdown as _md

            out["html"] = _md.markdown(
                md, extensions=["tables", "fenced_code", "sane_lists"]
            )
        except Exception:  # noqa: BLE001 - html is best-effort
            out["html"] = "<pre>" + md.replace("<", "&lt;") + "</pre>"
        out["sources"] = rp.get("source_notes", [])
    elif status not in srv.RUNNING_STATES:
        out["error"] = (
            payload.get("error_message")
            or payload.get("error")
            or "no research_package in result"
        )
    return out


def _safe_refresh(
    ref: ResearchRunRef,
    ctx: RunContext,
    repo: RunRepo,
    control_plane: ControlPlanePort,
    logger: logging.Logger,
) -> ResearchRunRef:
    """Best-effort status refresh; never fails a read if the CP/DB hiccups."""
    try:
        return refresh_run_status(ref, ctx, repo, control_plane)
    except (RepositoryUnavailable, NotFound, Exception) as exc:  # noqa: BLE001
        logger.warning("refresh_failed", extra={"run_id": ref.run_id, "err": str(exc)})
        return ref


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(
    deps: AppDeps,
    auth_decorator: Any = verify_session,
    enable_supertokens: bool = True,
) -> Flask:
    """Build a Flask app with all routes bound to ``deps``.

    ``auth_decorator`` is a ``verify_session``-compatible *factory*: routes
    apply ``@auth_decorator()`` (session required) or
    ``@auth_decorator(session_required=False)``. Tests inject a fake factory
    and set ``enable_supertokens=False`` to skip SuperTokens ``init``/Middleware
    /CORS entirely.
    """
    app = Flask(__name__)

    if enable_supertokens:
        _configure_supertokens(app)

    log = deps.logger

    def _resolve_ctx() -> RunContext:
        return current_run_context(
            deps.session_provider(), deps.identity, deps.config
        )

    # ---- static / auth pages (preserved exactly) ------------------------- #
    @app.route("/", methods=["GET"])
    @auth_decorator(session_required=False)
    def index() -> Response:
        if getattr(g, "supertokens", None) is None:
            return redirect("/login")
        return send_from_directory(BASE, "index.html")

    @app.route("/login", methods=["GET"])
    def login_page() -> Response:
        return send_from_directory(BASE, "login.html")

    @app.route("/login/reset-password", methods=["GET"])
    def reset_password_page() -> Response:
        return send_from_directory(BASE, "login.html")

    @app.route("/defaults", methods=["GET"])
    @auth_decorator()
    def defaults() -> Response:
        return jsonify(srv.DEFAULTS)

    # ---- run APIs (org-scoped, per-user) --------------------------------- #
    @app.route("/api/runs", methods=["GET"])
    @auth_decorator()
    def api_runs() -> Any:
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        try:
            refs = list_user_runs(ctx, deps.run_repo)
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        runs = [
            to_run_json(_safe_refresh(r, ctx, deps.run_repo, deps.control_plane, log))
            for r in refs
        ]
        return jsonify({"runs": runs, "control_plane": srv.CONTROL_PLANE})

    @app.route("/api/run", methods=["POST"])
    @auth_decorator()
    def api_run() -> Any:
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        try:
            deps.run_repo.ensure_ready()
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        try:
            launch = deps.launch(body)
        except LaunchError as exc:
            return jsonify({"error": str(exc)}), 400
        except (ControlPlaneUnreachable, LaunchResponseInvalid) as exc:
            return jsonify({"error": str(exc)}), 502
        try:
            ref = record_run_ownership(
                ctx, launch, deps.run_repo, deps.clock, deps.uuid_factory
            )
        except Conflict:
            log.error(
                "orphaned_launch",
                extra={
                    "run_id": launch.run_id,
                    "root_execution_id": launch.root_execution_id,
                    "org_id": str(ctx.org_id),
                    "created_by": str(ctx.user_id),
                    "reason": "duplicate_run_id",
                },
            )
            return jsonify({"error": "duplicate run"}), 409
        except RepositoryUnavailable:
            log.error(
                "orphaned_launch",
                extra={
                    "run_id": launch.run_id,
                    "root_execution_id": launch.root_execution_id,
                    "org_id": str(ctx.org_id),
                    "created_by": str(ctx.user_id),
                    "reason": "db_write_failed_after_dispatch",
                },
            )
            return jsonify({"error": "failed to record run"}), 500
        dto = LaunchRunDTO(
            run_id=launch.run_id,
            root_execution_id=launch.root_execution_id,
            created_at=launch.created_at.isoformat(),
            status=ref.status,
            node=launch.node,
            reasoner=launch.reasoner,
            params=dict(launch.params),
        )
        return jsonify(dto), 200

    @app.route("/api/result", methods=["GET"])
    @auth_decorator()
    def api_result() -> Any:
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        run_id = request.args.get("run", "")
        if not srv.valid_run_id(run_id):
            return jsonify({"error": "bad run id"}), 400
        try:
            ref = deps.run_repo.get_by_context(ctx, run_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        try:
            assert_run_access(ref, ctx)
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        ref = _safe_refresh(ref, ctx, deps.run_repo, deps.control_plane, log)
        payload = (
            deps.control_plane.get_execution(ref.result_ref)
            if ref.result_ref
            else None
        )
        return jsonify(_build_result_dto(ref, payload))

    @app.route("/api/cancel", methods=["POST"])
    @auth_decorator()
    def api_cancel() -> Any:
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        run_id = body.get("run", "")
        if not srv.valid_run_id(run_id):
            return jsonify({"error": "bad run id"}), 400
        try:
            ref = deps.run_repo.get_by_context(ctx, run_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        try:
            assert_run_access(ref, ctx)
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        execution_id = ref.execution_id or ref.result_ref
        res = (
            deps.control_plane.cancel_execution(execution_id, "cancelled from UI")
            if execution_id
            else None
        )
        if res and res.get("cancelled"):
            try:
                deps.run_repo.update_status(ctx, ref.run_id, "cancelled", None, None)
            except (RepositoryUnavailable, NotFound):
                pass
        return jsonify({"cancelled": execution_id})

    return app


def _configure_supertokens(app: Flask) -> None:
    """Initialize SuperTokens, Flask middleware, and CORS (production only)."""
    from supertokens_python import (
        InputAppInfo,
        SupertokensConfig,
        get_all_cors_headers,
        init,
    )
    from supertokens_python.recipe import emailpassword, session
    from supertokens_python.recipe.emailpassword.interfaces import (
        APIInterface,
        GeneralErrorResponse,
    )
    from supertokens_python.framework.flask import Middleware
    from flask_cors import CORS

    def _restrict_signups(original: APIInterface) -> APIInterface:
        orig_sign_up_post = original.sign_up_post

        async def sign_up_post(
            form_fields,
            tenant_id,
            session,  # noqa: A002 - matches SuperTokens signature
            should_try_linking_with_session_user,
            api_options,
            user_context,
        ):
            email = ""
            for f in form_fields:
                if f.id == "email":
                    email = (f.value or "").strip().lower()
            if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
                return GeneralErrorResponse(
                    message="Sign-up is restricted. Contact the owner for access."
                )
            return await orig_sign_up_post(
                form_fields,
                tenant_id,
                session,
                should_try_linking_with_session_user,
                api_options,
                user_context,
            )

        original.sign_up_post = sign_up_post
        return original

    init(
        app_info=InputAppInfo(
            app_name="Deep Research",
            api_domain=WEBSITE_DOMAIN,
            website_domain=WEBSITE_DOMAIN,
            api_base_path="/auth",
            website_base_path="/login",
        ),
        supertokens_config=SupertokensConfig(connection_uri=ST_CONN, api_key=ST_KEY),
        framework="flask",
        recipe_list=[
            session.init(),
            emailpassword.init(
                override=emailpassword.InputOverrideConfig(apis=_restrict_signups)
            ),
        ],
        mode="wsgi",
    )
    Middleware(app)
    CORS(
        app,
        supports_credentials=True,
        origins=[WEBSITE_DOMAIN],
        allow_headers=["Content-Type"] + get_all_cors_headers(),
    )


# Production app (SuperTokens on; DB-free at import via unprovisioned placeholders).
app = create_app(default_deps())


if __name__ == "__main__":
    print(
        f"Deep Research UI (SuperTokens) → :{PORT}  core={ST_CONN}  site={WEBSITE_DOMAIN}"
    )
    app.run(host=HOST, port=PORT, threaded=True)
