"""Fixtures for the producer-side (deep-research) INT Phase 2 tests.

Puts the fork repo root on ``sys.path`` so ``import research_completed_event``
resolves in the dev venv (the root modules are not pip-installed; the Docker
image relies on ``PYTHONPATH=/app`` instead — mirrors ``tests/ui/conftest.py``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> Any:
    with open(_FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def terminal_execution() -> dict:
    """A terminal, succeeded execution snapshot (the builder's input)."""
    return _load("terminal_execution.json")


@pytest.fixture
def golden_cloudevent() -> dict:
    """The shared golden ``research.completed`` CloudEvent (parsed)."""
    return _load("research_completed.cloudevent.json")


@pytest.fixture
def fixtures_dir() -> Path:
    return _FIXTURES
