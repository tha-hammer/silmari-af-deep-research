-- Deep Research — Postgres persistence for the research_run aggregate (B6, thin slice).
-- Single table; no dbmate, no user/org/membership tables, no foreign keys.
-- Idempotent: every object uses IF NOT EXISTS so re-applying is a no-op.

CREATE SCHEMA IF NOT EXISTS deepresearch;

CREATE TABLE IF NOT EXISTS deepresearch.research_run (
    id            uuid PRIMARY KEY,
    run_id        text NOT NULL UNIQUE,
    org_id        uuid NOT NULL,
    created_by    uuid NOT NULL,
    title         text,
    query         text NOT NULL,
    params        jsonb NOT NULL DEFAULT '{}',
    status        text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),
    visibility    text NOT NULL CHECK (visibility IN ('org', 'private')),
    result_ref    text,
    execution_id  text,
    started_at    timestamptz,
    completed_at  timestamptz,
    created_at    timestamptz,
    duration_ms   bigint CHECK (duration_ms IS NULL OR duration_ms >= 0)
);

CREATE INDEX IF NOT EXISTS ix_research_run_org_user_created
    ON deepresearch.research_run (org_id, created_by, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_research_run_org_run_id
    ON deepresearch.research_run (org_id, run_id);
