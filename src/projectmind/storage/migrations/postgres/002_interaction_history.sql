-- Interaction history: everything asked of ProjectMind and everything it
-- answered, so retrieval can be audited, replayed and improved from real use.
-- Extends bundle_log rather than adding a second table, because a bundle *is*
-- one interaction and the usage report already reads from it.
--
-- prompt_text, rendered_text and trace are NULL/empty when history is set to
-- 'minimal' (hashed metadata only, as before) or when the project is excluded.
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS source        TEXT NOT NULL DEFAULT 'cli';
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS session_id    TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS cwd           TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS prompt_text   TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS rendered_text TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS trace         JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS embedder      TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS version       TEXT;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS delivered     BOOLEAN;
CREATE INDEX IF NOT EXISTS idx_bundle_source ON bundle_log (source);
CREATE INDEX IF NOT EXISTS idx_bundle_project ON bundle_log (project_key);
