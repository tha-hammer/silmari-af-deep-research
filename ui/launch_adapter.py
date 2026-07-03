"""Production launch adapter (B7).

Dispatches a run to the AgentField control plane by reusing the *pure* helpers
in ``server.py`` (``coerce_params``, ``build_input_payload``, ``valid_run_id``)
and ``cp_post``. It deliberately does NOT touch ``save_cfg``, ``cfg_path``,
``RUNS_DIR``, or ``launch_run`` — persistence is now the ``RunRepo``'s job, and
the local JSON index is gone.

The adapter is a callable matching the launch seam the routes depend on:
``launch(params) -> LaunchResult``. Errors are raised as typed exceptions so
the route can map them to the plan's status codes (400 / 502).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from ui.workspace.dto import JSONValue, LaunchResult
from ui.workspace.research_run import normalize_cp_status

# server.py exposes the pure control-plane helpers. Import it whichever way the
# runtime layout allows (top-level under Docker/PYTHONPATH=ui; ``ui.server``
# under the repo-root test/import layout).
try:  # pragma: no cover - import shim
    import server as _srv  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - import shim
    from ui import server as _srv  # type: ignore[no-redef]


class LaunchError(Exception):
    """The control plane rejected the launch (semantic 400)."""


class ControlPlaneUnreachable(Exception):
    """The control plane could not be reached (transport 502)."""


class LaunchResponseInvalid(Exception):
    """The control plane replied without a usable run_id (502)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ControlPlaneLaunch:
    """Callable launch adapter over the control plane.

    ``srv`` is injected for testability (defaults to the imported ``server``
    module). ``clock`` stamps ``created_at`` deterministically in tests.
    """

    def __init__(
        self,
        srv: Any = _srv,
        node: str | None = None,
        reasoner: str | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._srv = srv
        self._node = node or srv.NODE
        self._reasoner = reasoner or srv.REASONER
        self._clock = clock

    def __call__(self, params: Mapping[str, JSONValue]) -> LaunchResult:
        coerced = self._srv.coerce_params(dict(params))
        if not coerced.get("query"):
            raise LaunchError("query is required")
        payload = self._srv.build_input_payload(coerced)
        resp = self._srv.cp_post(
            f"/execute/async/{self._node}.{self._reasoner}", {"input": payload}
        )
        if resp is None:
            raise ControlPlaneUnreachable("control plane unreachable")
        if resp.get("_error"):
            raise LaunchError(f"launch rejected: {resp['_error']}")
        run_id = resp.get("run_id") or resp.get("runId")
        if not self._srv.valid_run_id(run_id):
            raise LaunchResponseInvalid("launch response missing a valid run_id")
        root_execution_id = resp.get("execution_id") or resp.get("executionId") or ""
        return LaunchResult(
            run_id=run_id,
            root_execution_id=root_execution_id,
            created_at=self._clock(),
            status=normalize_cp_status(resp.get("status") or "running"),
            node=self._node,
            reasoner=self._reasoner,
            params=coerced,
        )
