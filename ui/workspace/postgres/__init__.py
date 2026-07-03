"""Postgres persistence adapters for the workspace domain (Behavior 6).

Thin slice: one ``deepresearch.research_run`` table, applied by ``migrate.apply``,
read/written through ``ResearchRunRepository`` (a ``RunRepo`` implementation).
Importing this package opens NO database connection.
"""

from __future__ import annotations

from .migrate import apply
from .readiness import check_ready
from .repository import ResearchRunRepository

__all__ = ["apply", "check_ready", "ResearchRunRepository"]
