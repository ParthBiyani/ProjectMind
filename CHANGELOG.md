# Changelog

Notable changes, newest first. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
loosely and [Semantic Versioning](https://semver.org/spec/v2.0.0.html) strictly.

## [0.2.0] — 2026-10-30

Real-world hardening, and a knowledge base that answers from what its owner has
written down. v0.1.0 passed its tests and failed on a real machine; every item
under Fixed was found by running it against real repositories.

### Added

- **A cited knowledge base.** Notes, `CLAUDE.md` logs, scripts, notebooks, training
  logs, small result files, PDF, Word and PowerPoint documents, and Claude Code
  session transcripts become verbatim passages with exact line spans or locators
  (`projectmind sources add / list / scan / remove / search / audit`). Full-text
  search on SQLite (FTS5, Porter stemming) and Postgres (tsvector).
- **Grounded serving.** A passage is served only if it covers the question's
  distinctive terms, including its most specific one, with near-duplicates
  suppressed and a named project preferred. Commit-derived records are searchable
  the same way, so a question about your own project needs no keyword list.
- **`projectmind sync`**, a local **daemon** (`serve`, `daemon ...`), a hook that is a
  thin client of it (about 150 ms against about 400 ms in process), and
  `schedule install` to start it at logon.
- **Interaction history.** Every prompt, the exact text served, a trace of each
  candidate kept or cut, delivery and feedback (`projectmind history list / show /
  replay / label / export / purge`).
- **Browser.** Allow-listed Edge and Chrome history (off by default, dry-run first) and
  a Manifest V3 extension limited by its manifest to the same list.
- **Profile authoring**: `profile add / edit / remove` and edit inside `review`.
- **`projectmind eval real`** to score the front door on your own material, and
  **`projectmind demo`**, a guided tour that is also a regression test.
- **`doctor`** now verifies hooks and MCP registration and runs the hook the way
  the agent would.
- Documentation: [sources](docs/sources.md), [privacy](docs/privacy.md), updated
  architecture, integration, evaluation and roadmap.

### Fixed

- Repositories git refused on ownership (common after a Windows account change) were
  skipped and reported as "not a git repository"; one of fourteen was read.
- The hook wrote ANSI-code-page bytes into the agent's context and could crash on
  non-ASCII input. It now reads and writes UTF-8 and replies in ASCII-safe JSON.
- The service never passed its clock to retrieval, so the evaluation drifted with
  the wall clock and its recorded figures cannot be reproduced. The set now pins
  `as_of`. See [evaluation.md](docs/evaluation.md#correction-to-the-phase-4-figures-v020).
- Records no longer carry invented reasons ("adopted while working on llm, llm");
  `Co-Authored-By` trailers are not read as rationale; entity lists are unique and
  path matching uses whole components.
- `PROJECTMIND_EXCLUDED_REPOS` could not be set from the environment.
- `.env` was read from the working directory of whichever project was open.
- Hook and MCP server both injected the same context; hook output carried no
  bundle id, so feedback could not be recorded.
- Hook detection matched the bare word "projectmind", so uninstalling could remove
  another tool's hook.
- A seed statement sat below the activation bar and could not be approved.
- Evaluation files were found relative to the package and failed outside a checkout.

### Security and privacy

- Identity and financial documents are skipped by file and folder name; credentials
  and personal identifiers (Aadhaar, PAN, SSN, payment cards) are dropped, not
  redacted. The identifier rules are deliberately narrow so experiment logs are
  not deleted by them. `sources audit --fix` applies today's rules to what is
  already indexed.
- The daemon binds to loopback, requires a bearer token, checks the `Host` header and
  sends no CORS headers; the extension cannot read sites outside the allow-list.

### Known gaps

- Lexical retrieval returns nothing when you ask in words your notes never use.
- The history-record path misses its false-injection target (0.323 against 0.10).
- LLM-written notes are described in the configuration but not implemented.
- The extension is not exercised in a real browser in CI.

## [0.1.0] — 2026-07-17

First working version. Phases 0 through 5 of the roadmap are complete; Phase 6
is designed and deliberately unbuilt.

### Added

**Evaluation (Phase 0).** A 40-query hand-labelled set with a 51-record fixture
corpus, ten queries that must return nothing, and seven with planted
near-misses. Scores precision@5, gate precision, cross-project hit rate,
false injection rate, nDCG and MRR. Null floor and oracle ceiling committed
before any retrieval existed.

**Profile memory (Phase 1).** Fifty hand-written preference statements across
tool choice, work style, abandonments and constraints, loaded as proposals and
served only after human approval. TTL-based decay per category, supersession
state machine, fingerprint-filtered selection inside an 800-token cap.

**Storage.** One `Store` protocol, SQLite and Postgres + pgvector behind it, a
parity suite that runs against both, and pluggable embeddings with a
deterministic offline fallback.

**MCP surface.** `get_context` as the single front door, plus `search_memory`,
`memory_feedback` and `memory_stats`. Claude Code integration installs a
`UserPromptSubmit` hook and registers the server, additively and with backups.

**Fingerprinting and the gate (Phase 2).** Manifest parsing for eleven
ecosystems, a bounded directory scan, a content-hashed cache, and weighted
similarity for ranking sibling projects. A deterministic prompt classifier with
a hard trivial-prompt skip, and server-side token budgets.

**Ingestion (Phase 3).** Local git history read offline with no token, plus a
GitHub pull-request source. Decision and failure extractors tuned for precision
over recall. Idempotent on a content hash.

**Retrieval (Phase 4).** BM25 written out rather than imported, vector search
through the store, score-normalised fusion, and a relative cut that makes the
number of results depend on the question.

**Reflection (Phase 5).** Contradiction detection behind an entity prefilter,
TTL scanning with passive re-confirmation first, a LangGraph state machine with
an equivalent sequential fallback, and a batched monthly review offering the
three PRD outcomes: supersede, keep both with scopes, or reject as noise.

### Measured

| Metric | Target | Result |
|---|---|---|
| Gate precision | ≥ 0.80 | 1.000 |
| Precision @5 | ≥ 0.70 | 0.750 |
| Cross-project hit rate | ≥ 0.30 | 0.300 |
| False injection rate | ≤ 0.10 | **0.174 — missed** |
| Bundle size | ≤ 2300 tokens | 667 max |
| Front-door latency | — | 11 ms p95 |

On this machine: 11 repositories, 190 commits, 82 episodic records in 1.7s.
The reflection prefilter discards 99.1% of statement-record pairs.

### Known limitations

- **`false_injection_rate` misses its target at 0.174.** Not rounded away. The
  threshold sweep and the one trade that deliberately made the number worse are
  in [docs/evaluation.md](docs/evaluation.md#phase-4-results-including-what-was-missed).
- **Four ranking constants were fitted on the eval set**, which therefore no
  longer measures them independently. A second labelled set is the fix.
- **Proposal acceptance rate is unmeasured.** It needs a human working a queue
  of real proposals; the corpus has not drifted enough to produce one outside a
  constructed test.
- **Phase 6 is unbuilt** by design — see
  [docs/phase-6-capture.md](docs/phase-6-capture.md).

### Fixed along the way

Bugs found by running against real data rather than fixtures, kept here because
each one changed a design decision:

- Similarity renormalisation let two projects sharing only "it is Python" score
  a perfect 1.000. Flooring the denominator at the dependency weight fixed it.
- The hybrid retriever was silently lexical-only: `list_records` does not load
  embeddings, so the in-process cosine scored every record at zero.
- Reciprocal rank fusion with k=60 is flat over a fifty-record corpus, so the
  relative cut removed nothing. Normalised scores lead; RRF only breaks ties.
- Normalising to the best hit guarantees something always scores 1.0. A query
  about Kubernetes against a corpus with none served a record at 0.945 until an
  absolute floor was added before normalisation.
- A profile statement's entity list holds both what it prefers and what it
  contrasts against, so "adopted Supabase instead of Firebase" read as
  contradicting "uses Supabase". Six of sixteen proposals were backwards.
- Architect prompts excluded failure records, so the gate could offer the
  decision to label in Roboflow but never the augmentation that collapsed mAP.
- A `feat` commit mentioning a "regression harness" became a 0.80-confidence
  failure record.
- Domain-hint matching on a single keyword labelled a placement-prep app as
  robotics, machine learning and IoT simultaneously.
- A dataset-heavy repository took 3.2s to fingerprint; skipping data
  directories brought it to 0.3s.
- README lookup guessed `readme.md` then `README.MD`, which finds `README.md`
  only on a case-insensitive filesystem. Domain hints silently never worked on
  Linux or macOS until CI caught it.
- "How did I set up the eval harness before?" — the canonical question a
  cross-project memory layer exists to answer — classified as `implement` and
  therefore returned nothing without a named entity.

[0.1.0]: https://github.com/ParthBiyani/ProjectMind/releases/tag/v0.1.0
