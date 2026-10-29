-- Source excerpts are a third kind of thing a bundle can carry, beside profile
-- statements and episodic records. Record which were served and what they cost.
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS evidence_ids    JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE bundle_log ADD COLUMN IF NOT EXISTS evidence_tokens INTEGER NOT NULL DEFAULT 0;
