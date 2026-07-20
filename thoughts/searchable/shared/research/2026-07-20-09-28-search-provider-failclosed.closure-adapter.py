"""Closure adapter (STAGED PROPOSAL — not wired into the repo).
Derived from the ClosureMap for: "A web-search failure surfaces on the run
record as a failed execution instead of a succeeded, source-less document."
Pin: 7c2baa66b7343c8626e399c1aeb35f76a0c3be04.
Promote into /home/maceo/ntm_Dev/silmari-agentfield-system/silmari-af-deep-research
and complete each TODO(promote) before use.
Speaks the 7-op contract apps/closure-oracle already talks to (mock_adapter.py).

NOTE (faithfulness): the SOURCE of this behavior is an HTTP boundary (the search
provider response), not a DB store. `/seed` therefore installs a fake provider
response rather than seeding a table. The OBSERVABLE is the control-plane
execution record read back through the UI's production read path.
"""
import http.server, json, sys
ASYNC_EDGES = []                                   # this chain is fully synchronous
CONNECTOR = {e: True for e in ASYNC_EDGES}
SINK = []                                          # Phase-0 /seed_sink target

def handle(op, p):
    if op == "/reset":        SINK.clear(); CONNECTOR.update({e: True for e in ASYNC_EDGES}); return {"ok": True}
    if op == "/set_connector": CONNECTOR[p["edge"]] = p["enabled"]; return {"ok": True}
    if op == "/seed_sink":     SINK.append(p["value"]); return {"ok": True}
    if op == "/seed":
        # TODO(promote): install fake search-provider response = p["data"] so
        #   skills.search.search() returns it (or raises it).            (skills/search/__init__.py:48)
        #   Seeding an EXCEPTION here is the red-at-seam case for this belt.
        return {"ok": True}
    if op == "/trigger":
        # TODO(promote): call execute_deep_research(**p["args"])         (main.py:3038)
        #   or the narrower reasoner execute_intelligence_stream_comprehensive (main.py:1341)
        return {"ok": True}
    if op == "/drive":
        if not CONNECTOR.get(p["edge"], True): return {"ok": True}      # oracle disabled = red-at-seam
        # No async edges in this chain — nothing to drain.
        return {"ok": True}
    if op == "/observe":
        # TODO(promote): return json.dumps(result_for(p["run_id"]))     (ui/server.py:251)
        #   i.e. the run's execution status + DocumentResponse as the UI reads it.
        return {"ok": True, "value": json.dumps(SINK)}
    return {"ok": False, "error": "unknown op"}

class Hn(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        out = json.dumps(handle(self.path, json.loads(self.rfile.read(n) or "{}"))).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Hn).serve_forever()
