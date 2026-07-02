# Architecture

ProjectMind is a single-user, local-first memory service. It has one job: given
a prompt and a project directory, return the smallest bundle of context that
makes an agent's next action better, and return nothing when it cannot.

## The two layers

Memory is split by cost-of-being-wrong, not by content type.

| | Profile | Episodic |
|---|---|---|
| Answers | How does this person work? | What happened, where, and why? |
| Volume | 50–200 statements | thousands of records |
| Injection | always, fingerprint-filtered | conditionally, when the gate fires |
| Wrong record costs | every future task | one task |
| Confidence bar | ≥ 0.85 to activate | ≥ 0.6 to serve |

The split exists so the two layers can have different write paths. Profile
statements only ever become active through human approval. Episodic records are
ingested continuously and never touch the profile without passing through the
reflection loop.

## Request path

```
              ┌─────────────────────────────────────────────────┐
  prompt ────►│                  get_context                    │
project_path  │                (the front door)                 │
              └───────┬─────────────────────────────┬───────────┘
                      │                             │
                      ▼                             ▼
             ┌─────────────────┐          ┌───────────────────┐
             │   fingerprint   │          │  prompt analysis  │
             │  (cached, no    │          │  task type,       │
             │   LLM, cheap)   │          │  entities,novelty │
             └────────┬────────┘          └─────────┬─────────┘
                      └──────────┬──────────────────┘
                                 ▼
                        ┌─────────────────┐
                        │ relevance gate  │  decides what may be served
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │   retrieval     │  BM25 + vector, fused
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │ rank and budget │  hard token caps, cross-project boost
                        └────────┬────────┘
                                 ▼
                          ContextBundle
```

Every stage is allowed to return nothing. An empty bundle is a correct answer
and is recorded as such, because "did not inject" is a measurable outcome.

## Layout

```
src/projectmind/
  config.py        settings, resolved from env then defaults
  models.py        domain types shared by every layer
  storage/         store protocol, sqlite and postgres backends, embeddings
  profile/         statement repository and the lifecycle state machine
  fingerprint/     manifest parsing, directory scan, cache, similarity
  gate/            prompt analysis, gating rules, ranking, token budget
  retrieval/       lexical, vector and fused search over episodic memory
  ingestion/       local git and GitHub sources, decision/failure extractors
  reflection/      contradiction detection, TTL scan, review queue
  telemetry/       bundle log and the metrics that score all of the above
  mcp/             the MCP server surface
  cli/             operator commands
```

## Design rules

1. **One front door.** Agents call `get_context` and nothing else on the hot
   path. Gating, ranking and budgeting are server-side decisions so that
   retrieval quality does not vary by which agent is driving.
2. **Degrade to nothing, never to wrong.** A missing API key, an unreachable
   database or an unparsable manifest reduces what is served. It never changes
   a correct answer into an incorrect one.
3. **Provenance on every record.** Anything served can be traced to the commit,
   pull request or statement it came from.
4. **Inference is labelled.** An extracted "why" is a hypothesis about an
   artifact, and is marked as one.
5. **Nothing interrupts a working session.** All human review is batched.

## Storage

Storage sits behind a single protocol with two implementations. SQLite is the
default so the server is available without a daemon; Postgres with pgvector is
selected by setting a `postgresql://` URL and is exercised by the same parity
test suite. No query is written against a backend directly.
