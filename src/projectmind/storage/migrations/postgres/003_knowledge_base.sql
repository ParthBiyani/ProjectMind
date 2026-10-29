-- The knowledge base: documents read from disk (and later the browser) and the
-- chunks they are split into. Every chunk keeps the line span or locator it came
-- from, so anything served can be traced back to its exact place in a source.
--
-- Chunks are indexed lexically (tsvector, English stemming so that
-- "returns" finds "return"). Exact technical terms are the most
-- reliable signal in this material, and an index of exact terms is also what
-- makes "this evidence really contains what you asked about" checkable.
CREATE TABLE IF NOT EXISTS documents (
    id           UUID PRIMARY KEY,
    uri          TEXT NOT NULL UNIQUE,
    source_type  TEXT NOT NULL,
    root         TEXT NOT NULL,
    project_key  TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    mtime        TIMESTAMPTZ NOT NULL,
    indexed_at   TIMESTAMPTZ NOT NULL,
    chunk_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_documents_root ON documents (root);
CREATE INDEX IF NOT EXISTS idx_documents_project ON documents (project_key);
CREATE INDEX IF NOT EXISTS idx_documents_type ON documents (source_type);

CREATE TABLE IF NOT EXISTS chunks (
    id          UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    ordinal     INTEGER NOT NULL,
    heading     TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    start_line  INTEGER NOT NULL DEFAULT 0,
    end_line    INTEGER NOT NULL DEFAULT 0,
    locator     TEXT NOT NULL DEFAULT '',
    tsv         TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(heading, '') || ' ' || text)
    ) STORED
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks (document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_tsv ON chunks USING GIN (tsv);
