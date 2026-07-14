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

import hmac
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
    ReelDispatch,
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
from ui.workspace.postgres.reel_job_repository import ReelJobRepository
from ui.workspace.postgres.repository import ResearchRunRepository
from ui.workspace.reel_job import (
    ReelJobPort,
    ReelJobRef,
    assert_reel_job_access,
    refresh_reel_job_status,
    to_reel_status_json,
)
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
# Unified login: set to a shared parent domain (e.g. ".silmari.app") so the session
# cookie is shared across sibling services (research.*, reels.*). Unset (None) keeps
# the current host-scoped cookie — required until both services sit under one custom
# parent domain (a shared cookie is impossible across *.up.railway.app, a public suffix).
COOKIE_DOMAIN = os.environ.get("SESSION_COOKIE_DOMAIN") or None

CONFIG_PATH = os.path.join(BASE, "config.json")
with open(CONFIG_PATH) as _cf:
    CONFIG = json.load(_cf)

# Only these emails may register. Empty = open registration (NOT recommended).
ALLOWED_EMAILS = {
    e.strip().lower() for e in CONFIG.get("allowed_emails", []) if e.strip()
}

# Base URL of the reel-af app, for the DR "Send to reels" deep-link. Env wins so
# each deploy points at its sibling; falls back to the config.json tunable.
REELS_BASE_URL = (
    os.environ.get("REELS_BASE_URL") or CONFIG.get("reels_base_url", "")
).rstrip("/")

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
    reel_job_repo: ReelJobPort
    reel_dispatch: Callable[[Mapping[str, JSONValue]], Any]
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


def make_default_reel_job_repo() -> ReelJobPort:
    """Lazy psycopg reel-job repo. No connect at import; missing env defers to use."""

    def _dsn() -> str:
        dsn = os.environ.get("DEEPRESEARCH_DATABASE_URL")
        if not dsn:
            raise RepositoryUnavailable("DEEPRESEARCH_DATABASE_URL is not set")
        return dsn

    return ReelJobRepository(_dsn)


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
        reel_job_repo=make_default_reel_job_repo(),
        reel_dispatch=ReelDispatch(srv),
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


def _safe_refresh_reel(
    job: ReelJobRef,
    ctx: RunContext,
    repo: ReelJobPort,
    control_plane: ControlPlanePort,
    logger: logging.Logger,
) -> ReelJobRef:
    """Best-effort reel-job status refresh; never fails a poll if the CP/DB hiccups
    (a last-known status is returned, never a fabricated ``succeeded``)."""
    try:
        return refresh_reel_job_status(job, ctx, repo, control_plane)
    except (RepositoryUnavailable, NotFound, Exception) as exc:  # noqa: BLE001
        logger.warning("reel_refresh_failed", extra={"job_id": str(job.id), "err": str(exc)})
        return job


# --------------------------------------------------------------------------- #
# Gateway-trust public path allowlist
# --------------------------------------------------------------------------- #
# Paths that must stay reachable WITHOUT the gateway secret even when this app is
# fronted by the unified-login gateway: the login pages (served by this app) and
# the SuperTokens /auth API (called directly by login.html's JS). An unscoped
# gateway-trust gate 403'd these and locked users out of direct login -- the
# 2026-07-11 emergency rollback. Exact-match the login routes; prefix-match /auth.
_PUBLIC_AUTH_PATHS = frozenset({"/login", "/login/reset-password", "/auth"})


def _is_public_auth_path(path: str) -> bool:
    return path in _PUBLIC_AUTH_PATHS or path.startswith("/auth/")


def _is_browser_navigation(sec_fetch_dest: str, accept: str) -> bool:
    """True for a top-level browser navigation (document load), False for a
    ``fetch()``/XHR. Decides whether an expired/absent session sends the browser
    to /login (clean redirect) or returns the default 401 JSON (so the SPA's own
    fetch guard handles it). Prefers the ``Sec-Fetch-Dest`` fetch-metadata
    header; falls back to Accept content negotiation for older clients."""
    dest = (sec_fetch_dest or "").lower()
    if dest:
        return dest == "document"
    a = (accept or "").lower()
    return "text/html" in a and "application/json" not in a


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(
    deps: AppDeps,
    auth_decorator: Any = verify_session,
    enable_supertokens: bool = True,
    enable_gateway_trust: bool = True,
) -> Flask:
    """Build a Flask app with all routes bound to ``deps``.

    ``auth_decorator`` is a ``verify_session``-compatible *factory*: routes
    apply ``@auth_decorator()`` (session required) or
    ``@auth_decorator(session_required=False)``. Tests inject a fake factory
    and set ``enable_supertokens=False`` to skip SuperTokens ``init``/Middleware
    /CORS entirely.

    ``enable_gateway_trust`` (Behaviors 5 & 5b, C4/C4b) — defaults to ``True``
    (secure/fail-closed by default, mirroring ``enable_supertokens``):

    - every request must carry ``X-Gateway-Secret`` matching
      ``GATEWAY_SHARED_SECRET`` (constant-time compare); missing/mismatched/
      **empty-env** all fail closed with 403 (RedTeam admin-token lesson —
      an unset secret must never mean "allow");
    - once admitted, request identity is resolved from the trusted
      ``X-User-Id`` header instead of this app's own SuperTokens session
      (this app's host never receives the browser's session cookie once
      fronted by the gateway — see plan REVIEW). A trusted request with no
      ``X-User-Id`` is a malformed/incomplete trusted request -> 401, never a
      silent fall-through to session-based resolution.

    Tests that don't exercise B5/B5b pass ``enable_gateway_trust=False`` to
    keep exercising this app's own SuperTokens-session path directly
    (dev/local/non-gateway callers).
    """
    app = Flask(__name__)

    if enable_supertokens:
        _configure_supertokens(app)

    log = deps.logger

    if enable_gateway_trust:

        @app.before_request
        def _enforce_gateway_trust():  # pragma: no branch - simple guard
            # Keep the auth surface (login pages + SuperTokens /auth API) open so
            # direct/local login still works when this app is fronted by the
            # gateway. Everything else remains fail-closed.
            if _is_public_auth_path(request.path):
                return None
            expected = os.environ.get("GATEWAY_SHARED_SECRET", "")
            provided = request.headers.get("X-Gateway-Secret", "")
            if not expected or not hmac.compare_digest(provided, expected):
                return jsonify({"error": "forbidden"}), 403
            if not request.headers.get("X-User-Id", "").strip():
                return jsonify({"error": "unauthorized"}), 401
            return None

    def _resolve_ctx() -> RunContext:
        trusted_user_id = request.headers.get("X-User-Id") if enable_gateway_trust else None
        return current_run_context(
            deps.session_provider(), deps.identity, deps.config, trusted_user_id
        )

    # Once gateway trust is active, THIS app's own SuperTokens session is no
    # longer the authentication authority — the gateway is (C4 admits the
    # request; C4b/`_resolve_ctx` resolves identity from the trusted header,
    # fail-closed via `Denied`). Requiring a session at the outer
    # `@auth_decorator()` gate too would reject every gateway-proxied request
    # outright, since this app's host never receives the browser's session
    # cookie once fronted (plan REVIEW). So the outer gate's
    # `session_required` tracks the INVERSE of gateway-trust mode; it is
    # unaffected when gateway trust is off (dev/local/direct — unchanged
    # behavior for every pre-existing route/test).
    _auth_required = auth_decorator(session_required=not enable_gateway_trust)

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
    @_auth_required
    def defaults() -> Response:
        payload = dict(srv.DEFAULTS)
        payload["reels_base"] = REELS_BASE_URL
        return jsonify(payload)

    # ---- run APIs (org-scoped, per-user) --------------------------------- #
    @app.route("/api/runs", methods=["GET"])
    @_auth_required
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
    @_auth_required
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
    @_auth_required
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
    @_auth_required
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

    # ---- Create Reel dispatch + poll (MW Phase 3, C7 / spec §5-6) --------- #
    @app.route("/api/create-reel", methods=["POST"])
    @_auth_required
    def api_create_reel() -> Any:
        # C7: verified session -> org isolation -> non-empty selection (fail-closed)
        # -> server-injected principal -> CP dispatch FIRST -> write row on accept.
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(silent=True) or {}
        selected = body.get("selectedParagraphs")
        if not isinstance(selected, list) or len(selected) == 0:
            return jsonify({"error": "empty_selection"}), 400  # fail-closed (C7)
        source_run_id = body.get("sourceRunId", "")
        if not srv.valid_run_id(source_run_id):
            return jsonify({"error": "bad run id"}), 400
        try:
            ref = deps.run_repo.get_by_context(ctx, source_run_id)
        except NotFound:
            return jsonify({"error": "run not found"}), 400
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        try:
            assert_run_access(ref, ctx)  # research_run.org_id == session.activeOrgId
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        # Server-injected principal (never client-supplied); re-sort by position.
        ordered = sorted(
            selected, key=lambda p: p.get("position", 0) if isinstance(p, Mapping) else 0
        )
        # Dispatch payload MUST match the reel-af `reel_research_to_reel` reasoner's
        # input contract (agentfield maps keys -> params by exact name). Keys are
        # snake_case, and `source_execution_id` is REQUIRED — it is the ref the
        # reasoner fetch_body()'s from the control plane. Omitting it 422s the agent
        # ("Missing required field: source_execution_id"). userId/orgId are the
        # server-injected principal (ignored by the reasoner; asserted by tests).
        payload = {
            "source_execution_id": ref.execution_id or ref.result_ref,
            "selected_paragraphs": ordered,
            "source_run_id": ref.run_id,
            "source_package_ref": ref.execution_id or ref.result_ref,
            "citations": body.get("citations", []),
            "userId": str(ctx.user_id),
            "orgId": str(ctx.org_id),
        }
        # Dispatch FIRST — fail BEFORE writing the reel_job row (P-1 ordering rule).
        try:
            dispatch = deps.reel_dispatch(payload)
        except (ControlPlaneUnreachable, LaunchError, LaunchResponseInvalid) as exc:
            return jsonify({"error": "dispatch_failed", "detail": str(exc)}), 502
        job_id = deps.uuid_factory()
        job = ReelJobRef(
            id=job_id,
            org_id=ctx.org_id,
            created_by=ctx.user_id,
            status="queued",
            source_research_run_id=ref.id,
            execution_id=dispatch.execution_id,
            result_ref=None,
            # Idempotency key: default to the job's own id when the client omits
            # one, so the NOT NULL + unique(org,user,client_request_id) constraint
            # is always satisfied (a client-supplied id still enables replay dedup).
            client_request_id=body.get("clientRequestId") or str(job_id),
            created_at=deps.clock(),
        )
        try:
            deps.reel_job_repo.create(job)
        except Conflict:
            return jsonify({"error": "duplicate reel job"}), 409
        except RepositoryUnavailable:
            log.error(
                "orphaned_dispatch",
                extra={
                    "execution_id": dispatch.execution_id,
                    "org_id": str(ctx.org_id),
                    "created_by": str(ctx.user_id),
                    "reason": "db_write_failed_after_dispatch",
                },
            )
            return jsonify({"error": "failed to record reel job"}), 500
        return (
            jsonify(
                {
                    "job_id": str(job_id),
                    "execution_id": dispatch.execution_id,
                    "status": "queued",
                }
            ),
            202,
        )

    @app.route("/api/reel-status", methods=["GET"])
    @_auth_required
    def api_reel_status() -> Any:
        try:
            ctx = _resolve_ctx()
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        job_id = request.args.get("job", "").strip()
        if not job_id:
            return jsonify({"error": "bad job id"}), 400
        try:
            job = deps.reel_job_repo.get_by_context(ctx, job_id)
        except NotFound:
            return jsonify({"error": "not found"}), 404
        except RepositoryUnavailable:
            return jsonify({"error": "storage unavailable"}), 503
        try:
            assert_reel_job_access(job, ctx)  # only creating org_id/created_by may poll
        except Denied:
            return jsonify({"error": "forbidden"}), 403
        job = _safe_refresh_reel(job, ctx, deps.reel_job_repo, deps.control_plane, log)
        return jsonify(to_reel_status_json(job))

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
    from supertokens_python.recipe.session import InputErrorHandlers
    from supertokens_python.recipe.session.utils import (
        default_try_refresh_token_callback,
        default_unauthorised_callback,
    )
    from supertokens_python.framework.flask import Middleware
    from flask_cors import CORS

    # On an expired/absent session, send a top-level browser navigation to the
    # login page (clean redirect) instead of surfacing SuperTokens' raw
    # ``{"message":"try refresh token"}`` JSON. XHR/fetch callers keep the
    # default 401 so the SPA's own fetch guard (index.html) can react.
    async def _on_try_refresh_token(req, message, response):
        if _is_browser_navigation(
            req.get_header("Sec-Fetch-Dest") or "", req.get_header("Accept") or ""
        ):
            return response.redirect("/login")
        return await default_try_refresh_token_callback(req, message, response)

    async def _on_unauthorised(req, message, response):
        if _is_browser_navigation(
            req.get_header("Sec-Fetch-Dest") or "", req.get_header("Accept") or ""
        ):
            return response.redirect("/login")
        return await default_unauthorised_callback(req, message, response)

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
            session.init(
                cookie_domain=COOKIE_DOMAIN,
                error_handlers=InputErrorHandlers(
                    on_try_refresh_token=_on_try_refresh_token,
                    on_unauthorised=_on_unauthorised,
                ),
            ),
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
# EMERGENCY ROLLBACK (2026-07-11): enable_gateway_trust=True was deployed
# without verifying the actual end-to-end browser login flow first, and it
# locked users out of the direct login they were relying on
# (deep-research-ui-production.up.railway.app/login -> 403, since the
# before_request gate isn't scoped to exclude /login/auth). Disabling it here
# restores direct access immediately. The code/tests for Behaviors 5/5b are
# unchanged and still pass -- re-enable only after the full flow is verified
# against a real browser session, not curl.
app = create_app(default_deps(), enable_gateway_trust=False)


if __name__ == "__main__":
    print(
        f"Deep Research UI (SuperTokens) → :{PORT}  core={ST_CONN}  site={WEBSITE_DOMAIN}"
    )
    app.run(host=HOST, port=PORT, threaded=True)
