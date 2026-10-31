# Scaling to a million users

ProjectMind today is single-user and local-first on purpose. This document describes how the
same design would serve a million developers, what carries over, what changes, and what must
not change.

## What already scales

- **The `Store` protocol.** Every query goes through one interface with two backends. The
  Postgres + pgvector backend exists and passes the same parity suite as SQLite. Multi-tenancy
  is a `user_id` on every table plus row-level security.
- **One front door.** `get_context` is transport-independent: the hook, the MCP server and the
  web UI already call the same service. A hosted HTTPS API is one more transport.
- **The gate is a pre-filter.** Mechanical edits and prompts that name nothing cost no history
  search. At scale this is the largest cost saver.
- **Coverage scoring is per user.** It needs only one user's term frequencies, which shard
  naturally.
- **Client-side filtering already exists.** The walker, the secret scanner and the identifier
  rules run where the files are.

## Sizing

| Quantity | Heavy user | 1M users (heavy-tail planning) |
|---|---|---|
| Passages | ~30,000 | up to 30 billion |
| Text and full-text index | ~60 MB | ~60 PB |
| Vectors (384 dims, float16) | ~23 MB | ~23 PB |
| Prompts per day | ~100 | ~100M (≈1,200/s average, ~5,000/s peak) |
| Prompts that reach retrieval | ~40% | ~2,000/s peak |

Most users will have far less material than a heavy user; plan capacity for the tail and
cost for the median.

## Target architecture

```
 hook / MCP / web / extension
        │  HTTPS, per-user OAuth token
        ▼
 API gateway ── auth, rate limits, routing by user shard
        │
        ├─► context service (stateless, autoscaled)
        │      gate (in memory) ─► retrieval ─► budget ─► bundle
        │                │
        │        per-shard Postgres + pgvector (HNSW), or a search tier per shard
        │        caches: fingerprints, term statistics, recent bundles
        │
        ├─► ingestion API ─► queue ─► workers: chunk, scan, embed (GPU pool), index
        │
        └─► history and feedback ─► warehouse ─► evaluation, calibration, A/B analysis
```

## The changes, and why

1. **Shard by user.** No query ever crosses users, so sharding by user id scales
   horizontally with no cross-shard joins. Large users get dedicated shards.
2. **Filter on the client, index on the server.** The walker and secret scanner keep running
   on the user's machine, so raw files never leave it; only accepted passages are uploaded.
   Server workers embed and index from a queue with back-pressure and retries.
3. **Approximate nearest neighbours.** Brute force over 30,000 vectors is milliseconds locally;
   at scale use HNSW per user partition (pgvector supports it) or a vector store. The semantic
   threshold stays per model (the `chunk_vectors.model` column already keys it) and is
   re-measured when the model changes.
4. **Shared embedding pool.** Batched GPU workers, versioned models, background re-embedding.
5. **Caching.** Fingerprints, term frequencies and the bundle for repeated prompts.
6. **Privacy and compliance.** Per-user envelope encryption through a key management service,
   encryption at rest, tenant-isolation tests, deletion that removes passages, vectors and
   backups within a stated window, regional data residency (GDPR, India's DPDP Act), and a
   local-only mode for users who want no cloud.
7. **Observability.** The per-request trace already exists; add latency SLOs (p95 under 300 ms
   end to end), per-shard dashboards, and the false-injection rate from feedback as a product
   health metric.
8. **Evaluation at scale.** The interaction history becomes the evaluation set: consented,
   sampled, labelled questions per cohort; every retrieval change replayed offline before
   rollout; A/B tests with false injection as the guardrail; per-user calibration of the
   coverage floor and semantic threshold from feedback.
9. **Cost.** Dominated by storage and one-time embedding, not by queries. A free tier with a
   passage cap plus the local-only mode keep it affordable.

## What breaks first

**Calibration.** Floors tuned on one corpus will not fit everyone. Per-user calibration from
feedback, and the split between a strict automatic bar and a generous, labelled explicit search,
matter more as the user base grows.

## What must not change

The contract: verbatim, cited, relevant or silent, private by construction. At a million users a
false injection is a million silently worse sessions, so restraint matters more, not less.
