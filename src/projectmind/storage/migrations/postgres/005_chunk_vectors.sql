-- Semantic vectors for passages, so a question can be matched by meaning as well as
-- by shared words. Optional: an index with no vectors works exactly as before.
--
-- Stored as 16-bit floats (half the size, no measurable loss for retrieval). The
-- model name is part of the key space: vectors from different models are never mixed.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id UUID NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vector   BYTEA NOT NULL,
    PRIMARY KEY (chunk_id, model)
);

CREATE INDEX IF NOT EXISTS idx_chunk_vectors_model ON chunk_vectors (model);
