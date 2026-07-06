-- ProjectMind schema, Postgres dialect.
--
-- Mirrors migrations/sqlite/001_initial.sql table for table and column for
-- column. Where the dialects differ the semantics do not: TEXT timestamps
-- become timestamptz, TEXT JSON becomes jsonb, and the float32 blob becomes a
-- pgvector column so nearest-neighbour search runs in the database instead of
-- in Python.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS projects (
    id            UUID PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    root_path     TEXT,
    git_remote    TEXT,
    fingerprint   JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen    TIMESTAMPTZ NOT NULL,
    last_active   TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_remote ON projects (git_remote);

CREATE TABLE IF NOT EXISTS profile_statements (
    id                  UUID PRIMARY KEY,
    statement           TEXT NOT NULL,
    category            TEXT NOT NULL,
    scope               JSONB,
    evidence_refs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence          DOUBLE PRECISION NOT NULL,
    status              TEXT NOT NULL,
    superseded_by       UUID REFERENCES profile_statements (id) ON DELETE SET NULL,
    supersedes          UUID REFERENCES profile_statements (id) ON DELETE SET NULL,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    contradiction_refs  JSONB NOT NULL DEFAULT '[]'::jsonb,
    entities            JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL,
    last_confirmed_at   TIMESTAMPTZ NOT NULL,
    ttl_days            INTEGER NOT NULL,
    origin              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_statements_status ON profile_statements (status);
CREATE INDEX IF NOT EXISTS idx_statements_category ON profile_statements (category);

CREATE TABLE IF NOT EXISTS episodic_records (
    id                    UUID PRIMARY KEY,
    project_id            UUID NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    type                  TEXT NOT NULL,
    what                  TEXT NOT NULL,
    why                   TEXT NOT NULL DEFAULT '',
    rejected_alternatives JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome               TEXT NOT NULL DEFAULT '',
    entities              JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_type           TEXT NOT NULL,
    source_url            TEXT NOT NULL,
    occurred_at           TIMESTAMPTZ NOT NULL,
    confidence            DOUBLE PRECISION NOT NULL,
    is_inference          BOOLEAN NOT NULL DEFAULT FALSE,
    superseded_by         UUID REFERENCES episodic_records (id) ON DELETE SET NULL,
    content_hash          TEXT NOT NULL UNIQUE,
    ingested_at           TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_project ON episodic_records (project_id);
CREATE INDEX IF NOT EXISTS idx_records_type ON episodic_records (type);
CREATE INDEX IF NOT EXISTS idx_records_occurred ON episodic_records (occurred_at DESC);

CREATE TABLE IF NOT EXISTS record_entities (
    record_id UUID NOT NULL REFERENCES episodic_records (id) ON DELETE CASCADE,
    entity    TEXT NOT NULL,
    PRIMARY KEY (record_id, entity)
);

CREATE INDEX IF NOT EXISTS idx_record_entities_entity ON record_entities (entity);

-- 384 dimensions matches the default embedding provider. Changing the provider
-- to one with a different width needs a migration, which is the honest trade
-- for being able to put an index on the column at all.
CREATE TABLE IF NOT EXISTS record_embeddings (
    record_id  UUID PRIMARY KEY REFERENCES episodic_records (id) ON DELETE CASCADE,
    dimensions INTEGER NOT NULL,
    vector     VECTOR(384) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_record_embeddings_cosine
    ON record_embeddings USING hnsw (vector vector_cosine_ops);

CREATE TABLE IF NOT EXISTS review_queue (
    id              UUID PRIMARY KEY,
    kind            TEXT NOT NULL,
    target_id       UUID NOT NULL,
    proposal        TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    evidence_refs   JSONB NOT NULL DEFAULT '[]'::jsonb,
    impact          DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ,
    resolution      TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    batch_id        UUID
);

CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue (resolution, impact DESC);

CREATE TABLE IF NOT EXISTS fingerprint_cache (
    project_key   TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    fingerprint   JSONB NOT NULL,
    computed_at   TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS bundle_log (
    bundle_id         UUID PRIMARY KEY,
    created_at        TIMESTAMPTZ NOT NULL,
    project_key       TEXT,
    task_type         TEXT,
    prompt_hash       TEXT NOT NULL,
    prompt_words      INTEGER NOT NULL DEFAULT 0,
    profile_ids       JSONB NOT NULL DEFAULT '[]'::jsonb,
    episodic_ids      JSONB NOT NULL DEFAULT '[]'::jsonb,
    cross_project_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    profile_tokens    INTEGER NOT NULL DEFAULT 0,
    episodic_tokens   INTEGER NOT NULL DEFAULT 0,
    gate_reason       TEXT NOT NULL DEFAULT '',
    latency_ms        DOUBLE PRECISION NOT NULL DEFAULT 0,
    injected          BOOLEAN NOT NULL DEFAULT FALSE,
    feedback          BOOLEAN,
    feedback_note     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_bundle_created ON bundle_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bundle_feedback ON bundle_log (feedback);
