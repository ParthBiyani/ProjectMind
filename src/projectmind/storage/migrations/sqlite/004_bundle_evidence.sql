-- Source excerpts are a third kind of thing a bundle can carry, beside profile
-- statements and episodic records. Record which were served and what they cost.
ALTER TABLE bundle_log ADD COLUMN evidence_ids    TEXT NOT NULL DEFAULT '[]';
ALTER TABLE bundle_log ADD COLUMN evidence_tokens INTEGER NOT NULL DEFAULT 0;
