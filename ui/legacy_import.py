"""Legacy run-index importer (B8).

Reads the old ``ui/runs/run_*.json`` files and, for each valid one, records a
``ResearchRunRef`` in the default org owned by the configured
``legacy_import_owner_email``. Pure over injected ports: it validates through
``from_legacy_json`` (the single legacy serializer) and writes through the same
``RunRepo`` the routes use.

Contract:
- ``dry_run=True`` (default) reports outcomes and writes nothing.
- If the legacy owner email cannot be resolved to an active app user with
  default-org membership, the import aborts before ANY write (fail closed).
- Existing ``run_id`` is an idempotent *skip*, not a failure.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
from uuid import UUID, uuid4

from ui.tenancy.context import RunContext
from ui.tenancy.identity import IdentityPort
from ui.workspace.ports import NotFound, RunRepo
from ui.workspace.research_run import (
    LegacyRunData,
    MapperError,
    ResearchRunRef,
    from_legacy_json,
)

_SKIP_NAMES = {".pending-input.json"}


@dataclass
class ImportReport:
    """Outcome tallies for a legacy import pass."""

    unresolved_owner: bool = False
    imported: list[str] = field(default_factory=list)
    would_import: list[str] = field(default_factory=list)
    skipped_existing: list[str] = field(default_factory=list)
    invalid: list[tuple[str, str]] = field(default_factory=list)
    unresolved_owner_files: list[str] = field(default_factory=list)


def _iter_legacy_files(directory: str) -> list[str]:
    names = sorted(os.listdir(directory))
    out: list[str] = []
    for name in names:
        if name.startswith("."):  # dotfiles (and .pending-input.json)
            continue
        if name in _SKIP_NAMES:
            continue
        if not name.endswith(".json"):
            continue
        out.append(name)
    return out


def _ref_from_legacy(
    legacy: LegacyRunData,
    user_id: UUID,
    org_id: UUID,
    visibility: str,
    uuid_factory: Callable[[], UUID],
) -> ResearchRunRef:
    return ResearchRunRef(
        id=uuid_factory(),
        run_id=legacy.run_id,
        org_id=org_id,
        created_by=user_id,
        query=legacy.query,
        params=legacy.params,
        status=legacy.status,
        visibility=visibility,  # type: ignore[arg-type]
        result_ref=legacy.root_execution_id,
        execution_id=legacy.root_execution_id,
        created_at=legacy.created_at,
        started_at=legacy.created_at,
        completed_at=legacy.completed_at,
        duration_ms=legacy.duration_ms,
    )


def import_legacy_runs(
    directory: str,
    repo: RunRepo,
    identity: IdentityPort,
    config: Mapping[str, Any],
    dry_run: bool = True,
    uuid_factory: Callable[[], UUID] = uuid4,
) -> ImportReport:
    """Import legacy run JSON files into ``repo``. See module docstring."""
    report = ImportReport()

    # Resolve the owner FIRST — an unresolved owner means we never write.
    owner_email = str(config["legacy_import_owner_email"])
    owner = identity.resolve_owner_email(owner_email)
    report.unresolved_owner = owner is None
    visibility = str(config.get("default_run_visibility", "private"))

    for name in _iter_legacy_files(directory):
        path = os.path.join(directory, name)
        try:
            with open(path) as fh:
                payload = json.load(fh)
        except (ValueError, OSError):
            report.invalid.append((name, "malformed json"))
            continue
        try:
            legacy = from_legacy_json(path, payload)
        except MapperError as exc:
            report.invalid.append((name, str(exc)))
            continue

        if owner is None:
            # Valid file, but no owner to attribute it to — cannot import.
            report.unresolved_owner_files.append(legacy.run_id)
            continue

        user_id, org_id = owner
        ctx = RunContext(
            user_id=user_id, org_id=org_id, supertokens_user_id="legacy-import"
        )
        try:
            repo.get_by_context(ctx, legacy.run_id)
            report.skipped_existing.append(legacy.run_id)  # idempotent skip
            continue
        except NotFound:
            pass

        if dry_run:
            report.would_import.append(legacy.run_id)
            continue

        ref = _ref_from_legacy(legacy, user_id, org_id, visibility, uuid_factory)
        repo.add(ref)
        report.imported.append(legacy.run_id)

    return report
