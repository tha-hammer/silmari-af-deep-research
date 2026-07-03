"""Fixtures for ui/ unit + contract tests.

Also puts the fork repo root on ``sys.path`` so ``import ui.*`` resolves in
the dev venv (the package is not pip-installed; the Docker image relies on
``PYTHONPATH=/app`` instead).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests.ui._helpers import make_ctx  # noqa: E402
from ui.tenancy.context import RunContext  # noqa: E402
from ui.workspace.fakes import FakeRunRepo  # noqa: E402


@pytest.fixture
def ctx() -> RunContext:
    return make_ctx()


def _pg_run_repo() -> Any:
    """A migrated, empty ``ResearchRunRepository`` against ``TEST_DATABASE_URL``.

    Skips (rather than fails) when the env var is unset so the ``pg`` contract
    variant only runs under an explicit ``-m integration`` selection with a
    reachable Postgres. Truncates the table so each test starts clean.
    """
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL not set")

    import psycopg

    from ui.workspace.postgres.migrate import apply
    from ui.workspace.postgres.repository import ResearchRunRepository

    apply(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute("TRUNCATE deepresearch.research_run")
        conn.commit()
    return ResearchRunRepository(dsn)


@pytest.fixture(
    params=[
        "fake",
        pytest.param("pg", marks=pytest.mark.integration),
    ]
)
def run_repo(request: pytest.FixtureRequest) -> Any:
    # "fake" runs everywhere; "pg" carries the integration marker so it is
    # deselected by ``-m "not integration"`` and runs the SAME contract suite
    # against Postgres under ``-m integration``.
    if request.param == "fake":
        return FakeRunRepo()
    if request.param == "pg":
        return _pg_run_repo()
    raise NotImplementedError(f"unknown run_repo backend: {request.param}")
