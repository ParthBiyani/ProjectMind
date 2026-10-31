# Architecture

ProjectMind is a single-user, local-first memory service. It has one job: given
a prompt and a project directory, return the smallest bundle of context that
makes an agent's next action better, and return nothing when it cannot.

## Three kinds of memory

Memory is split by cost-of-being-wrong, not by content type.

| | Profile | History records | Source passages |
|---|---|---|---|
| Answers | How does this person work? | What was decided or broke, and when? | What do my own files, notes, sessions and reading say? |
| Volume | 50-200 statements | hundreds to thousands | tens of thousands |
| Written by | a human, always | extracted from git history | read verbatim from sources |
| Served | every task, fingerprint-filtered | when the gate fires | when a passage covers the question |
| A wrong item costs | every future task | one task | one task |
| Bar | confidence >= 0.85 | confidence >= 0.6 | covers the question's distinctive terms (>= 60%) |

The split exists so the layers can have different write paths. Profile statements
only become active through human approval. History records and source passages are
ingested continuously and never touch the profile without passing through review.
Commit-derived records are also mirrored into the passage index, so a question about
your own project is found by what it says rather than by a keyword list.

## Request path

```
   hook (thin client) --localhost--> daemon (warm database, scheduler)
                                         |
                                         v
              +------------------------ get_context ----------------------+
  prompt ---->|  fingerprint + prompt analysis    (trivial prompts stop)  |
 project_path +---------+------------------------------------+------------+
                        |                                    |
                        v                                    v
              profile slice (<= 800 tok)       history records (gate + hybrid retrieval)
                        |                                    |
                        |                                    v
                        |                      source passages (open vocabulary, cited)
                        |                         rarity-weighted coverage, most
                        |                         specific term required, deduplicated
                        |                                    |
                        |                      optional meaning matches (daemon only):
                        |                         cosine >= 0.78, near the best, shares a
                        |                         distinctive word
                        v                                    v
                        +----------------- budget ----------+
                                 hard caps: 800 / 1500 / 1200, 2300 total
                                          |
                                          v
                              ContextBundle  --> interaction history
                                                 (prompt, trace, answer, delivery)
```

Every stage may return nothing. An empty bundle is a correct answer and is recorded
as one, with the reason, because "did not inject" is a measurable outcome.

## How knowledge gets in

```
  folders -----> walker (whitelist, data-dump guard, identity-document guard)
  git history -> extractors (no invented reasons)           \
  sessions ----> turns (what was asked, the closing reply)   >-> chunks (exact line
  browser -----> allow-listed pages, queries that led to one /    spans / locators)
  extension ---> page text, allow-listed or explicitly remembered      |
                                                                       v
                                       credential + identifier scan (drop, never redact)
                                                                       |
                                                                       v
                                                    SQLite FTS5 / Postgres tsvector
```

`projectmind sync` (and the daemon, on a timer) runs the whole of it incrementally.

## Layout

```
src/projectmind/
  config.py        settings: environment, then ~/.projectmind/.env, never a project's .env
  models.py        domain types shared by every layer
  service.py       the front door: get_context, search, search_sources, history logging
  semantic.py      optional meaning search: stored vectors, in-memory matrix, nearest passages
  storage/         store protocol, sqlite and postgres backends, embeddings, migrations
  profile/         statement repository and the lifecycle state machine
  fingerprint/     manifest parsing, directory scan, cache, similarity
  gate/            prompt analysis, gating rules, token budget
  retrieval/       history-record retrieval (hybrid) and evidence retrieval (cited passages)
  ingestion/       local git and GitHub sources, decision/failure extractors
  sources/         walker, chunker, office readers, transcripts, browser history, audit
  reflection/      contradiction detection, TTL scan, review queue
  evaluation/      the synthetic set and the real-material scorer
  capture.py       what the browser extension may send, and what is kept
  daemon.py        local server: warm hook answers, scheduler, capture endpoint, web UI
  web/             the dashboard (static HTML/CSS/JS) and its JSON API
  hook_entry.py    standard-library-only hook client with an in-process fallback
  sync.py          one incremental pass over every source
  demo.py          the guided tour on a fictional workspace
  mcp/             the MCP server surface
  cli/             operator commands
extension/         the Edge and Chrome extension (Manifest V3, plain JavaScript)
```

## Surfaces

| Surface | For | Notes |
|---|---|---|
| `UserPromptSubmit` hook | automatic injection | standard-library thin client; asks the daemon, falls back in process |
| MCP server | deliberate asks | `get_context`, `search_sources`, `search_memory`, `memory_feedback`, `memory_stats` |
| CLI | operating it | `projectmind ...`; see the README |
| Web UI | seeing and demoing it | `projectmind ui`, `projectmind ui --demo`; see [web-ui.md](web-ui.md) |
| Browser extension | capturing reading | Manifest V3, allow-listed hosts only |

Every surface calls the same `MemoryService`, so behaviour does not depend on which one is
used. Semantic search runs only where its vectors are loaded (the daemon, the MCP server,
explicit CLI search); the in-process hook fallback is lexical.

## Design rules

1. **One front door.** Agents call `get_context` and nothing else on the hot
   path. Gating, ranking and budgeting are server-side decisions so that
   retrieval quality does not vary by which agent is driving.
2. **Degrade to nothing, never to wrong.** A missing API key, an unreachable
   database or an unparsable manifest reduces what is served. It never changes
   a correct answer into an incorrect one.
3. **Provenance on every item.** Anything served is a verbatim excerpt with the
   exact place it came from (path and lines, commit URL, page, cell, turn, URL).
   Nothing is summarised or paraphrased.
4. **Inference is labelled.** An extracted "why" is a hypothesis about an
   artifact, and is marked as one.
5. **Nothing interrupts a working session.** All human review is batched.
6. **Sources are read-only.** A scan lists and reads; it never writes to a source
   and never touches version control state.
7. **Private by construction.** Credentials and personal identifiers are dropped,
   identity documents are never opened, browser reading is opt-in and
   allow-listed, and the daemon answers only on loopback with a token.

## Storage

Storage sits behind a single protocol with two implementations. SQLite is the
default so the server is available without a daemon; Postgres with pgvector is
selected by setting a `postgresql://` URL and is exercised by the same parity
test suite. No query is written against a backend directly.

Schema changes are numbered migrations applied in order on both backends:

| Migration | Adds |
|---|---|
| 001 | profile statements, projects, history records, review queue, bundle log |
| 002 | interaction history in full (prompt, trace, served text, delivery) |
| 003 | the knowledge base: documents, chunks and their full-text index (FTS5 / tsvector) |
| 004 | which passages each bundle served |
| 005 | `chunk_vectors`: float16 passage embeddings keyed by chunk and model |

For the reasoning behind these choices in plain language see
[how-it-works.md](how-it-works.md); for how the design would serve many users see
[scaling.md](scaling.md).
