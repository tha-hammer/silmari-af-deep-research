#!/usr/bin/env python3
"""
Deep Research UI — Flask + SuperTokens front end.

Reuses all the business logic in `server.py` (launch/list/result/cancel against
the AgentField control plane) but puts a real SuperTokens EmailPassword login in
front of it instead of the legacy HTTP Basic Auth gate.

Env:
  SUPERTOKENS_CONNECTION_URI / CONNECTION_URI   SuperTokens Core URL (private)
  SUPERTOKENS_API_KEY        / API_KEY          SuperTokens Core API key
  UI_WEBSITE_DOMAIN                              public URL of THIS UI (for cookies)
  DR_UI_PORT (default 8899), UI_BIND_HOST (default :: for Railway private net)
  AGENTFIELD_SERVER, AGENTFIELD_API_KEY         (read by server.py)
"""
import json
import os

from flask import Flask, g, jsonify, request, redirect, send_from_directory

import server as srv  # existing helpers; importing does NOT start the old server

from supertokens_python import (
    init,
    InputAppInfo,
    SupertokensConfig,
    get_all_cors_headers,
)
from supertokens_python.recipe import session, emailpassword
from supertokens_python.recipe.emailpassword.interfaces import (
    APIInterface,
    GeneralErrorResponse,
)
from supertokens_python.recipe.session.framework.flask import verify_session
from supertokens_python.framework.flask import Middleware
from flask_cors import CORS

# Server-side config: all tunables live in ui/config.json (ARCHITECTURE.md config
# doctrine — no literals in code, one-jump lookup). Loaded once at startup.
CONFIG_PATH = os.path.join(srv.BASE, "config.json")
with open(CONFIG_PATH) as _cf:
    CONFIG = json.load(_cf)

# Only these emails may register. Empty = open registration (NOT recommended).
ALLOWED_EMAILS = {e.strip().lower() for e in CONFIG.get("allowed_emails", []) if e.strip()}


def _restrict_signups(original: APIInterface) -> APIInterface:
    """Reject public sign-ups whose email is not on the allowlist."""
    orig_sign_up_post = original.sign_up_post

    async def sign_up_post(
        form_fields, tenant_id, session, should_try_linking_with_session_user,
        api_options, user_context,
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
            form_fields, tenant_id, session,
            should_try_linking_with_session_user, api_options, user_context,
        )

    original.sign_up_post = sign_up_post
    return original

BASE = srv.BASE
PORT = int(os.environ.get("DR_UI_PORT", "8899"))
HOST = os.environ.get("UI_BIND_HOST", "::")  # dual-stack for Railway private net
WEBSITE_DOMAIN = os.environ.get("UI_WEBSITE_DOMAIN", f"http://localhost:{PORT}").rstrip("/")
ST_CONN = os.environ.get(
    "SUPERTOKENS_CONNECTION_URI", os.environ.get("CONNECTION_URI", "http://localhost:3567")
).rstrip("/")
ST_KEY = os.environ.get("SUPERTOKENS_API_KEY", os.environ.get("API_KEY", "")) or None

init(
    app_info=InputAppInfo(
        app_name="Deep Research",
        api_domain=WEBSITE_DOMAIN,      # auth API is served from this same app
        website_domain=WEBSITE_DOMAIN,  # single-origin: login page + API + tool
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

app = Flask(__name__)
Middleware(app)  # exposes /auth/* and refreshes sessions
CORS(
    app,
    supports_credentials=True,
    origins=[WEBSITE_DOMAIN],
    allow_headers=["Content-Type"] + get_all_cors_headers(),
)


@app.route("/", methods=["GET"])
@verify_session(session_required=False)
def index():
    # Logged out -> send them to the login page; logged in -> serve the tool.
    if g.supertokens is None:
        return redirect("/login")
    return send_from_directory(BASE, "index.html")


@app.route("/login", methods=["GET"])
def login_page():
    return send_from_directory(BASE, "login.html")


@app.route("/login/reset-password", methods=["GET"])
def reset_password_page():
    # Target of the password-reset email link (?token=...). Same page; JS detects
    # the token and shows the "set new password" form.
    return send_from_directory(BASE, "login.html")


@app.route("/defaults", methods=["GET"])
@verify_session()
def defaults():
    return jsonify(srv.DEFAULTS)


@app.route("/api/runs", methods=["GET"])
@verify_session()
def api_runs():
    return jsonify({"runs": srv.list_runs(), "control_plane": srv.CONTROL_PLANE})


@app.route("/api/result", methods=["GET"])
@verify_session()
def api_result():
    run_id = request.args.get("run", "")
    if not srv.valid_run_id(run_id):
        return jsonify({"error": "bad run id"}), 400
    return jsonify(srv.result_for(run_id))


@app.route("/api/run", methods=["POST"])
@verify_session()
def api_run():
    body = request.get_json(silent=True) or {}
    res = srv.launch_run(body)
    return jsonify(res), (400 if res.get("error") else 200)


@app.route("/api/cancel", methods=["POST"])
@verify_session()
def api_cancel():
    body = request.get_json(silent=True) or {}
    run_id = body.get("run", "")
    if not srv.valid_run_id(run_id):
        return jsonify({"error": "bad run id"}), 400
    eid = srv.resolve_execution_id(run_id)
    if eid:
        srv.cp_post(f"/executions/{eid}/cancel", {"reason": "cancelled from UI"})
    return jsonify({"cancelled": eid})


if __name__ == "__main__":
    print(f"Deep Research UI (SuperTokens) → :{PORT}  core={ST_CONN}  site={WEBSITE_DOMAIN}")
    app.run(host=HOST, port=PORT, threaded=True)
