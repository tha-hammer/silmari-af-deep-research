"""Behavior 1 — RunRepo port contract.

Parametrized over the ``run_repo`` fixture (fake only for now; the "fake"
param id leaves a seam so B6 can add "pg" and run this same suite against
Postgres).
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from tests.ui._helpers import CTX_ORG, OTHER_ORG, OTHER_USER, make_ctx, make_ref
from ui.workspace.ports import Conflict, NotFound


def test_ensure_ready_is_callable(run_repo) -> None:
    run_repo.ensure_ready()  # no raise


def test_add_get_roundtrip(run_repo, ctx) -> None:
    sample = make_ref(run_id="run_round_trip")
    run_repo.add(sample)
    assert run_repo.get_by_context(ctx, sample.run_id) == sample


def test_list_by_context_filters_org_and_user(run_repo, ctx) -> None:
    run_repo.add(make_ref(run_id="run_a", org_id=ctx.org_id, created_by=ctx.user_id))
    run_repo.add(make_ref(run_id="run_b", org_id=ctx.org_id, created_by=OTHER_USER))
    run_repo.add(make_ref(run_id="run_c", org_id=OTHER_ORG, created_by=ctx.user_id))
    assert [r.run_id for r in run_repo.list_by_context(ctx)] == ["run_a"]


def test_list_by_context_newest_first(run_repo, ctx) -> None:
    from datetime import datetime, timezone

    older = make_ref(
        run_id="run_old", created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    newer = make_ref(
        run_id="run_new", created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    run_repo.add(older)
    run_repo.add(newer)
    assert [r.run_id for r in run_repo.list_by_context(ctx)] == ["run_new", "run_old"]


def test_get_by_context_foreign_org_is_notfound(run_repo, ctx) -> None:
    # Same-org/other-user IS fetchable (then denied by assert_run_access);
    # foreign-org rows are indistinguishable from absent -> NotFound.
    run_repo.add(make_ref(run_id="run_foreign", org_id=OTHER_ORG))
    with pytest.raises(NotFound):
        run_repo.get_by_context(ctx, "run_foreign")


def test_get_by_context_same_org_other_user_is_fetchable(run_repo, ctx) -> None:
    run_repo.add(make_ref(run_id="run_sibling", org_id=ctx.org_id, created_by=OTHER_USER))
    fetched = run_repo.get_by_context(ctx, "run_sibling")
    assert fetched.run_id == "run_sibling"


def test_duplicate_public_run_id_raises_conflict(run_repo, ctx) -> None:
    sample = make_ref(run_id="run_dup")
    run_repo.add(sample)
    with pytest.raises(Conflict):
        run_repo.add(replace(sample, id=uuid4()))


def test_missing_get_and_update_raise_notfound(run_repo, ctx) -> None:
    with pytest.raises(NotFound):
        run_repo.get_by_context(ctx, "run_absent")
    with pytest.raises(NotFound):
        run_repo.update_status(ctx, "run_absent", "failed", None, None)


def test_update_status_returns_updated_ref(run_repo, ctx) -> None:
    from datetime import datetime, timezone

    run_repo.add(make_ref(run_id="run_upd", status="running"))
    done = datetime(2026, 7, 3, 10, 0, tzinfo=timezone.utc)
    updated = run_repo.update_status(ctx, "run_upd", "succeeded", done, 1500)
    assert updated.status == "succeeded"
    assert updated.completed_at == done
    assert updated.duration_ms == 1500
    # persisted
    assert run_repo.get_by_context(ctx, "run_upd").status == "succeeded"


def test_update_status_is_org_scoped(run_repo) -> None:
    other_ctx = make_ctx(org_id=OTHER_ORG)
    run_repo.add(make_ref(run_id="run_scope", org_id=CTX_ORG))
    with pytest.raises(NotFound):
        run_repo.update_status(other_ctx, "run_scope", "failed", None, None)
