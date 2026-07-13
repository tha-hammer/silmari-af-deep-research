"""B2/B6 — ReelJobPort contract.

Parametrized over the ``reel_job_repo`` fixture (``fake`` everywhere; ``pg``
carries the integration marker and runs the SAME suite against Postgres when a
provisioned ``deepresearch.reel_job`` is reachable).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from tests.ui._helpers import OTHER_ORG, OTHER_USER, make_ctx, make_reel_job_ref
from ui.workspace.ports import Conflict, NotFound

JOB_A = UUID("aaaa0000-0000-0000-0000-000000000001")


def test_ensure_ready_is_callable(reel_job_repo) -> None:
    reel_job_repo.ensure_ready()  # no raise


def test_create_get_roundtrip(reel_job_repo, ctx) -> None:
    job = make_reel_job_ref(id=JOB_A, org_id=ctx.org_id, created_by=ctx.user_id)
    reel_job_repo.create(job)
    fetched = reel_job_repo.get_by_context(ctx, str(JOB_A))
    assert fetched.id == JOB_A
    assert fetched.status == "queued"
    assert fetched.execution_id == "exec_reel_0001"


def test_get_by_context_foreign_org_is_notfound(reel_job_repo, ctx) -> None:
    reel_job_repo.create(make_reel_job_ref(id=JOB_A, org_id=OTHER_ORG))
    with pytest.raises(NotFound):
        reel_job_repo.get_by_context(ctx, str(JOB_A))


def test_get_by_context_same_org_other_user_is_fetchable(reel_job_repo, ctx) -> None:
    reel_job_repo.create(
        make_reel_job_ref(id=JOB_A, org_id=ctx.org_id, created_by=OTHER_USER)
    )
    # fetchable (then denied by assert_reel_job_access) — mirrors run repo doctrine.
    assert reel_job_repo.get_by_context(ctx, str(JOB_A)).id == JOB_A


def test_duplicate_client_request_id_raises_conflict(reel_job_repo, ctx) -> None:
    reel_job_repo.create(
        make_reel_job_ref(id=JOB_A, org_id=ctx.org_id, created_by=ctx.user_id,
                          client_request_id="crid-1")
    )
    with pytest.raises(Conflict):
        reel_job_repo.create(
            make_reel_job_ref(id=uuid4(), org_id=ctx.org_id, created_by=ctx.user_id,
                              client_request_id="crid-1")
        )


def test_missing_get_and_update_raise_notfound(reel_job_repo, ctx) -> None:
    absent = str(uuid4())
    with pytest.raises(NotFound):
        reel_job_repo.get_by_context(ctx, absent)
    with pytest.raises(NotFound):
        reel_job_repo.update_status(ctx, absent, "failed", None, None)


def test_update_status_persists_and_is_org_scoped(reel_job_repo, ctx) -> None:
    reel_job_repo.create(make_reel_job_ref(id=JOB_A, org_id=ctx.org_id, created_by=ctx.user_id))
    done = datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc)
    updated = reel_job_repo.update_status(ctx, str(JOB_A), "succeeded", "reel://v/1", done)
    assert updated.status == "succeeded"
    assert updated.result_ref == "reel://v/1"
    assert reel_job_repo.get_by_context(ctx, str(JOB_A)).status == "succeeded"
    # foreign org cannot update
    other = make_ctx(org_id=OTHER_ORG)
    with pytest.raises(NotFound):
        reel_job_repo.update_status(other, str(JOB_A), "failed", None, None)
