"""B8 — import_legacy_runs validation, idempotency, and fail-closed owner."""

from __future__ import annotations

import json
from pathlib import Path

from tests.ui._helpers import CTX_ORG, CTX_USER, make_ref
from ui.legacy_import import import_legacy_runs
from ui.tenancy.fakes import FakeIdentity
from ui.workspace.fakes import FakeRunRepo

OWNER = {"maceo@example.com": (CTX_USER, CTX_ORG)}
CONFIG = {
    "legacy_import_owner_email": "maceo@example.com",
    "default_run_visibility": "private",
}


def _write(dir_: Path, name: str, payload: dict) -> None:
    (dir_ / name).write_text(json.dumps(payload))


def _valid_payload(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "created_at": "2026-07-03T09:40:00+00:00",
        "status": "succeeded",
        "root_execution_id": "exec_" + run_id,
        "params": {"query": "what is X?"},
        "duration_ms": 0,  # preserved, not dropped
    }


def _identity() -> FakeIdentity:
    return FakeIdentity(by_email=OWNER)


def test_dry_run_reports_without_writing(tmp_path):
    _write(tmp_path, "run_a1.json", _valid_payload("run_a1"))
    _write(tmp_path, "run_bad.json", {"run_id": "run_bad"})  # missing query/created_at
    _write(tmp_path, "notjson.txt", {})  # skipped (not .json)
    _write(tmp_path, ".pending-input.json", {"x": 1})  # skipped dotfile
    (tmp_path / "malformed.json").write_text("{not json")

    repo = FakeRunRepo()
    report = import_legacy_runs(str(tmp_path), repo, _identity(), CONFIG, dry_run=True)

    assert report.would_import == ["run_a1"]
    assert report.imported == []
    invalid_names = {n for n, _ in report.invalid}
    assert "run_bad.json" in invalid_names
    assert "malformed.json" in invalid_names
    # Nothing was written.
    from tests.ui._helpers import make_ctx

    assert repo.list_by_context(make_ctx()) == []


def test_real_import_writes_and_preserves_zero_duration(tmp_path):
    _write(tmp_path, "run_a1.json", _valid_payload("run_a1"))
    repo = FakeRunRepo()
    report = import_legacy_runs(str(tmp_path), repo, _identity(), CONFIG, dry_run=False)

    assert report.imported == ["run_a1"]
    from tests.ui._helpers import make_ctx

    rows = repo.list_by_context(make_ctx())
    assert [r.run_id for r in rows] == ["run_a1"]
    assert rows[0].created_by == CTX_USER and rows[0].org_id == CTX_ORG
    assert rows[0].visibility == "private"
    assert rows[0].duration_ms == 0  # preserved


def test_filename_run_id_mismatch_is_invalid(tmp_path):
    payload = _valid_payload("run_real")
    _write(tmp_path, "run_other.json", payload)  # stem != payload run_id
    repo = FakeRunRepo()
    report = import_legacy_runs(str(tmp_path), repo, _identity(), CONFIG, dry_run=False)
    assert report.imported == []
    assert any(n == "run_other.json" for n, _ in report.invalid)


def test_existing_run_id_is_idempotent_skip(tmp_path):
    _write(tmp_path, "run_dup.json", _valid_payload("run_dup"))
    repo = FakeRunRepo()
    repo.add(make_ref("run_dup", org_id=CTX_ORG, created_by=CTX_USER))
    report = import_legacy_runs(str(tmp_path), repo, _identity(), CONFIG, dry_run=False)
    assert report.skipped_existing == ["run_dup"]
    assert report.imported == []


def test_unresolved_owner_aborts_before_writing(tmp_path):
    _write(tmp_path, "run_a1.json", _valid_payload("run_a1"))
    repo = FakeRunRepo()
    identity = FakeIdentity(by_email={})  # owner email cannot resolve
    report = import_legacy_runs(str(tmp_path), repo, identity, CONFIG, dry_run=False)
    assert report.unresolved_owner is True
    assert report.imported == []
    assert report.unresolved_owner_files == ["run_a1"]
    from tests.ui._helpers import make_ctx

    assert repo.list_by_context(make_ctx()) == []
