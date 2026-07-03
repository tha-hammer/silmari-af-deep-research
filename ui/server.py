#!/usr/bin/env python3
"""
Deep Research UI — a thin control panel over the AgentField `meta_deep_research`
reasoner. Every run's full parameter set is persisted to runs/<run_id>.json so a
result is always traceable back to exactly the config that produced it.

Run with the app's venv so `markdown` is available:
    ./.venv/bin/python ui/server.py            # serves http://localhost:8899
Env overrides: DR_UI_PORT, AGENTFIELD_SERVER, AGENTFIELD_API_KEY

The UI talks to the AgentField control plane purely over HTTP (cp_get/cp_post);
it has no dependency on the `af` CLI binary, so it builds/deploys from ui/ alone.
"""
import base64
import hmac
import json
import os
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE, "runs")
os.makedirs(RUNS_DIR, exist_ok=True)

PORT = int(os.environ.get("DR_UI_PORT", "8899"))

# --- Access control -------------------------------------------------------
# HTTP Basic Auth gate. Set DR_UI_PASSWORD to require a login on every request;
# each run costs ~10k paid agent invocations, so a public host MUST set it.
# DR_UI_USER defaults to "maceo". If DR_UI_PASSWORD is unset the UI stays open
# (local dev) and prints a loud warning at startup.
AUTH_USER = os.environ.get("DR_UI_USER", "maceo")
AUTH_PASS = os.environ.get("DR_UI_PASSWORD", "")
REALM = "Deep Research"

CONTROL_PLANE = os.environ.get("AGENTFIELD_SERVER", "http://localhost:8080").rstrip("/")
API = CONTROL_PLANE + "/api/v1"
# Control planes with AGENTFIELD_API_KEY set require this header on every API call.
AF_API_KEY = os.environ.get("AGENTFIELD_API_KEY", "")

DEFAULTS = json.load(open(os.path.join(BASE, "defaults.json")))
NODE = DEFAULTS["node"]
REASONER = DEFAULTS["reasoner"]
FIELDS = DEFAULTS["fields"]

RUNNING_STATES = {"running", "pending", "queued", "registered", "submitted", ""}
INT_KEYS = {"research_focus", "research_scope", "max_research_loops", "num_parallel_streams"}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def cfg_path(run_id):
    # run_id is validated before this is ever called
    return os.path.join(RUNS_DIR, run_id + ".json")


def save_cfg(cfg):
    with open(cfg_path(cfg["run_id"]), "w") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def valid_run_id(run_id):
    return bool(run_id) and re.fullmatch(r"run_[A-Za-z0-9_]+", run_id) is not None


def cp_get(path):
    """GET the control plane API and return parsed JSON (or None)."""
    try:
        req = urllib.request.Request(API + path)
        if AF_API_KEY:
            req.add_header("X-API-Key", AF_API_KEY)
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def cp_post(path, body):
    """POST JSON to the control plane API (/api/v1 + path) → parsed JSON (or None).

    On an HTTP error status the parsed error message is returned under an
    `_error` key so callers can surface it; transport failures return None.
    """
    try:
        req = urllib.request.Request(
            API + path, data=json.dumps(body).encode(), method="POST"
        )
        req.add_header("Content-Type", "application/json")
        if AF_API_KEY:
            req.add_header("X-API-Key", AF_API_KEY)
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode() or "{}")
        except ValueError:
            detail = {}
        return {"_error": detail.get("error") or detail.get("message") or f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def coerce_params(raw):
    """Build a clean, defaulted, type-correct param dict from user input."""
    params = {}
    for field in FIELDS:
        key = field["key"]
        val = raw.get(key, field.get("default"))
        if key in INT_KEYS:
            try:
                val = int(val)
            except (TypeError, ValueError):
                val = field["default"]
            lo, hi = field.get("min"), field.get("max")
            if lo is not None:
                val = max(lo, val)
            if hi is not None:
                val = min(hi, val)
        else:
            val = "" if val is None else str(val).strip()
        params[key] = val
    return params


def build_input_payload(params):
    """The reasoner input object. Omit blank optional strings."""
    payload = dict(params)
    for opt in ("model", "evidence_style", "mode"):
        if opt in payload and payload[opt] == "":
            payload.pop(opt)
    return payload


def cp_get_agent_run(run_id):
    """Fetch a run's executions over HTTP (replaces the old CLI run lookup)."""
    try:
        req = urllib.request.Request(
            CONTROL_PLANE + f"/api/ui/v2/workflow-runs/{run_id}"
        )
        if AF_API_KEY:
            req.add_header("X-API-Key", AF_API_KEY)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def root_execution_id(run_id):
    data = cp_get_agent_run(run_id) or {}
    execs = data.get("executions") or data.get("data", {}).get("executions", [])
    roots = [e for e in execs if not e.get("parent_execution_id")]
    chosen = roots[0] if roots else (execs[0] if execs else None)
    return chosen.get("execution_id") if chosen else None


def resolve_execution_id(run_id):
    """Cached execution id from the run's cfg, else an HTTP lookup."""
    p = cfg_path(run_id)
    if os.path.exists(p):
        try:
            eid = json.load(open(p)).get("root_execution_id")
            if eid:
                return eid
        except (ValueError, OSError):
            pass
    return root_execution_id(run_id)


def launch_run(raw_params):
    params = coerce_params(raw_params)
    if not params.get("query"):
        return {"error": "query is required"}
    payload = build_input_payload(params)
    resp = cp_post(f"/execute/async/{NODE}.{REASONER}", {"input": payload})
    if not resp:
        return {"error": "control plane unreachable"}
    if resp.get("_error"):
        return {"error": f"launch rejected: {resp['_error']}"}
    run_id = resp.get("run_id") or resp.get("runId")
    if not valid_run_id(run_id):
        return {"error": "launch response missing a valid run_id", "response": resp}
    cfg = {
        "run_id": run_id,
        "root_execution_id": resp.get("execution_id") or resp.get("executionId"),
        "created_at": now_iso(),
        "status": resp.get("status") or "running",
        "node": NODE,
        "reasoner": REASONER,
        "params": params,
    }
    return save_cfg(cfg)


def refresh_status(cfg):
    """Update a config's status from the control plane if still running."""
    if cfg.get("status") not in RUNNING_STATES:
        return cfg
    eid = cfg.get("root_execution_id")
    if not eid:
        eid = root_execution_id(cfg["run_id"])
        cfg["root_execution_id"] = eid
    if not eid:
        return cfg
    payload = cp_get(f"/executions/{eid}")
    if payload and payload.get("status"):
        cfg["status"] = payload["status"]
        if payload.get("completed_at"):
            cfg["completed_at"] = payload["completed_at"]
        if payload.get("duration_ms"):
            cfg["duration_ms"] = payload["duration_ms"]
        save_cfg(cfg)
    return cfg


def list_runs():
    runs = []
    for name in os.listdir(RUNS_DIR):
        if not name.endswith(".json") or name.startswith("."):
            continue
        try:
            cfg = json.load(open(os.path.join(RUNS_DIR, name)))
        except (ValueError, OSError):
            continue
        runs.append(refresh_status(cfg))
    runs.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return runs


def render_markdown(rp, meta):
    lines = [f"# {rp.get('document_title', 'Research Report')}\n"]
    if rp.get("executive_summary"):
        lines += ["## Executive Summary\n", rp["executive_summary"].strip(), ""]
    for i, s in enumerate(rp.get("sections", []), 1):
        lines += [f"\n## {i}. {s.get('title', 'Untitled')}\n", (s.get("content") or "").strip(), ""]
    sn = rp.get("source_notes", [])
    if sn:
        lines.append("\n## Sources\n")
        for s in sn:
            lines.append(f"{s.get('citation_id', '?')}. [{s.get('title', 'source')}]({s.get('url', '')}) — {s.get('domain', '')}")
    return "\n".join(lines)


def result_for(run_id):
    cfg = None
    p = cfg_path(run_id)
    if os.path.exists(p):
        cfg = refresh_status(json.load(open(p)))
    eid = (cfg or {}).get("root_execution_id") or root_execution_id(run_id)
    payload = cp_get(f"/executions/{eid}") if eid else None
    if not payload:
        return {"status": "unknown", "error": "no execution payload"}
    status = payload.get("status", "unknown")
    result = payload.get("result") or {}
    rp = result.get("research_package")
    out = {
        "status": status,
        "run_id": run_id,
        "params": (cfg or {}).get("params"),
        "duration_ms": payload.get("duration_ms"),
        "source_count": len((rp or {}).get("source_notes", [])) if rp else 0,
        "section_count": len((rp or {}).get("sections", [])) if rp else 0,
    }
    if rp:
        md = render_markdown(rp, result.get("metadata", {}))
        out["markdown"] = md
        try:
            import markdown as _md
            out["html"] = _md.markdown(md, extensions=["tables", "fenced_code", "sane_lists"])
        except Exception:
            out["html"] = "<pre>" + md.replace("<", "&lt;") + "</pre>"
        out["sources"] = rp.get("source_notes", [])
    elif status not in RUNNING_STATES:
        out["error"] = payload.get("error_message") or payload.get("error") or "no research_package in result"
    return out


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def _authorized(self):
        """True when no password is configured or the Basic Auth header matches."""
        if not AUTH_PASS:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(pw, AUTH_PASS)

    def _require_auth(self):
        """Send a 401 challenge. Returns True so callers can `return self._require_auth()`."""
        body = b'{"error": "authentication required"}'
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    # The run APIs below are DISABLED (B7). The standalone, unscoped
    # `Handler` must not serve `list_runs`/`result_for`/`launch_run`/cancel —
    # every run path now goes through the Flask factory (`ui/app.py`) with a
    # per-user `RunContext`, `RunRepo`, and `assert_run_access` guard. These
    # endpoints return 410 Gone with NO side effects (no CP call, no repo/FS
    # read or write). The pure helpers above remain intact and are reused.
    _GONE = b'{"error": "gone: run API moved behind the authenticated Flask app"}'

    def do_GET(self):
        if not self._authorized():
            return self._require_auth()
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._send(200, open(os.path.join(BASE, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path == "/defaults":
            return self._send(200, json.dumps(DEFAULTS))
        if u.path in ("/api/runs", "/api/result"):
            return self._send(410, self._GONE)
        return self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if not self._authorized():
            return self._require_auth()
        u = urlparse(self.path)
        if u.path in ("/api/run", "/api/cancel"):
            # 410 BEFORE reading the body — no launch, no cancel, no side effects.
            return self._send(410, self._GONE)
        return self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    print(f"Deep Research UI → http://localhost:{PORT}   (control plane: {CONTROL_PLANE})")
    if AUTH_PASS:
        print(f"🔒 Basic Auth ENABLED (user: {AUTH_USER})")
    else:
        print("⚠️  UNPROTECTED — DR_UI_PASSWORD is unset. Do NOT expose this UI "
              "publicly; each run spends real API credits.")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
