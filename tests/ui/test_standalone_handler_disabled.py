"""B7 — the standalone server.Handler must not serve unscoped run APIs.

Every run path now goes through the authenticated Flask app. The legacy
``Handler`` returns 410 for the four run endpoints with NO side effects
(no ``list_runs``/``result_for``/``launch_run``/cancel, no CP call). The pure
helper functions remain intact (they are reused by the new adapters).
"""

from __future__ import annotations

import pytest

from ui import server as srv


class _Handler(srv.Handler):
    """Drive do_GET/do_POST without a socket."""

    def __init__(self) -> None:  # noqa: D401 - deliberately skip socket setup
        self.headers = {}  # no Authorization; AUTH_PASS unset -> authorized
        self.path = ""
        self.sent: list[tuple[int, object]] = []

    def _send(self, code, body, ctype="application/json"):  # type: ignore[override]
        self.sent.append((code, body))


@pytest.fixture(autouse=True)
def _explode_on_side_effects(monkeypatch):
    for name in ("list_runs", "result_for", "launch_run", "cp_post", "cp_get"):
        monkeypatch.setattr(
            srv,
            name,
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("disabled handler must have no side effects")
            ),
        )


@pytest.mark.parametrize("path", ["/api/runs", "/api/result"])
def test_get_run_apis_return_410(path):
    h = _Handler()
    h.path = path
    h.do_GET()
    assert h.sent and h.sent[0][0] == 410


@pytest.mark.parametrize("path", ["/api/run", "/api/cancel"])
def test_post_run_apis_return_410(path):
    h = _Handler()
    h.path = path
    h.do_POST()
    assert h.sent and h.sent[0][0] == 410


def test_pure_helpers_still_present():
    assert callable(srv.valid_run_id)
    assert callable(srv.coerce_params)
    assert callable(srv.build_input_payload)
    assert callable(srv.render_markdown)
    assert srv.valid_run_id("run_abc") is True
