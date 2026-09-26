-- ADR-003 (prompt versioning): pin storage and per-source summary tracking.
--
-- Prompt VERSIONS themselves are no longer bumped in code near this table;
-- they live in the append-only registry adapters/llm_api/prompts.py
-- (operation -> {version: PromptVersion}). This supersedes 005's "Bump
-- PROMPT_VERSION in llm_adapter.py to invalidate" note -- 005 is applied
-- and is never edited.
--
-- `prompt_pins` holds the admin-controlled pin per operation: 'deployment'
-- scope for the whole deployment, or a source_id for a chunk_summary
-- per-source override. `hyde` has no per-source override (CHECK below), so
-- it only ever has a 'deployment' row. This migration inserts no pin rows;
-- pins are seeded into the in-memory resolver at startup, not here.
--
-- `source_records.summary_prompt_version` records the chunk_summary version
-- a source was last summarized at; the backfill sets existing rows to v3
-- (today's summary prompt, per ADR-003 §1) so nothing looks stale on
-- upgrade. `summary_refresh_target` marks an in-progress, not-yet-finished
-- summary-only refresh (`resummarize`) triggered by moving the chunk_summary
-- pin: non-NULL means "refresh to this version is pending/running" (ADR-003
-- §2).
--
-- Every statement below is idempotent: the migration runner holds no lock
-- while applying files (issue #28), so a migration must tolerate being
-- re-run (e.g. concurrent workers racing to apply it).
CREATE TABLE IF NOT EXISTS prompt_pins (
    operation  TEXT NOT NULL,
    scope      TEXT NOT NULL,          -- 'deployment' or a source_id
    version    INTEGER NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT,
    PRIMARY KEY (operation, scope),
    CHECK (operation <> 'hyde' OR scope = 'deployment')
);

ALTER TABLE source_records
    ADD COLUMN IF NOT EXISTS summary_prompt_version INTEGER,
    ADD COLUMN IF NOT EXISTS summary_refresh_target INTEGER;

UPDATE source_records SET summary_prompt_version = 3 WHERE summary_prompt_version IS NULL;
