-- ProjectMind schema, SQLite dialect.
--
-- Mirrors migrations/postgres/001_initial.sql. The two are kept deliberately
-- close: same table names, same column names, same semantics, so the parity
-- suite can run identical assertions against either backend.

CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    key           TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    root_path     TEXT,
    git_remote    TEXT,
    fingerprint   TEXT NOT NULL DEFAULT '{}',
    first_seen    TEXT NOT NULL,
    last_active   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_projects_remote ON projects (git_remote);

CREATE TABLE IF NOT EXISTS profile_statements (
    id                  TEXT PRIMARY KEY,
    statement           TEXT NOT NULL,
    category            TEXT NOT NULL,
    scope               TEXT,
    evidence_refs       TEXT NOT NULL DEFAULT '[]',
    confidence          REAL NOT NULL,
    status              TEXT NOT NULL,
    superseded_by       TEXT REFERENCES profile_statements (id) ON DELETE SET NULL,
    supersedes          TEXT REFERENCES profile_statements (id) ON DELETE SET NULL,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    contradiction_refs  TEXT NOT NULL DEFAULT '[]',
    entities            TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    last_confirmed_at   TEXT NOT NULL,
    ttl_days            INTEGER NOT NULL,
    origin              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_statements_status ON profile_statements (status);
CREATE INDEX IF NOT EXISTS idx_statements_category ON profile_statements (category);

CREATE TABLE IF NOT EXISTS episodic_records (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    type                  TEXT NOT NULL,
    what                  TEXT NOT NULL,
    why                   TEXT NOT NULL DEFAULT '',
    rejected_alternatives TEXT NOT NULL DEFAULT '[]',
    outcome               TEXT NOT NULL DEFAULT '',
    entities              TEXT NOT NULL DEFAULT '[]',
    source_type           TEXT NOT NULL,
    source_url            TEXT NOT NULL,
    occurred_at           TEXT NOT NULL,
    confidence            REAL NOT NULL,
    is_inference          INTEGER NOT NULL DEFAULT 0,
    superseded_by         TEXT REFERENCES episodic_records (id) ON DELETE SET NULL,
    content_hash          TEXT NOT NULL UNIQUE,
    ingested_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_project ON episodic_records (project_id);
CREATE INDEX IF NOT EXISTS idx_records_type ON episodic_records (type);
CREATE INDEX IF NOT EXISTS idx_records_occurred ON episodic_records (occurred_at DESC);

-- Entities are exploded into their own table rather than queried out of the
-- JSON column, because the prompt gate does an entity-overlap lookup on every
-- single call and that has to be an index hit.
CREATE TABLE IF NOT EXISTS record_entities (
    record_id TEXT NOT NULL REFERENCES episodic_records (id) ON DELETE CASCADE,
    entity    TEXT NOT NULL,
    PRIMARY KEY (record_id, entity)
);

CREATE INDEX IF NOT EXISTS idx_record_entities_entity ON record_entities (entity);

-- Embeddings live in a side table so that loading records for lexical search
-- does not drag several kilobytes of float per row through memory.
CREATE TABLE IF NOT EXISTS record_embeddings (
    record_id  TEXT PRIMARY KEY REFERENCES episodic_records (id) ON DELETE CASCADE,
    dimensions INTEGER NOT NULL,
    vector     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    proposal        TEXT NOT NULL,
    rationale       TEXT NOT NULL DEFAULT '',
    evidence_refs   TEXT NOT NULL DEFAULT '[]',
    impact          REAL NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution      TEXT,
    resolution_note TEXT NOT NULL DEFAULT '',
    batch_id        TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_open ON review_queue (resolution, impact DESC);

CREATE TABLE IF NOT EXISTS fingerprint_cache (
    project_key   TEXT PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    computed_at   TEXT NOT NULL
);

-- Every call to the front door, so the gate can be scored against real usage
-- rather than only against the offline eval set. The prompt is hashed, never
-- stored.
CREATE TABLE IF NOT EXISTS bundle_log (
    bundle_id         TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    project_key       TEXT,
    task_type         TEXT,
    prompt_hash       TEXT NOT NULL,
    prompt_words      INTEGER NOT NULL DEFAULT 0,
    profile_ids       TEXT NOT NULL DEFAULT '[]',
    episodic_ids      TEXT NOT NULL DEFAULT '[]',
    cross_project_ids TEXT NOT NULL DEFAULT '[]',
    profile_tokens    INTEGER NOT NULL DEFAULT 0,
    episodic_tokens   INTEGER NOT NULL DEFAULT 0,
    gate_reason       TEXT NOT NULL DEFAULT '',
    latency_ms        REAL NOT NULL DEFAULT 0,
    injected          INTEGER NOT NULL DEFAULT 0,
    feedback          INTEGER,
    feedback_note     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_bundle_created ON bundle_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bundle_feedback ON bundle_log (feedback);
