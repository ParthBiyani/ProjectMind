# ProjectMind

**A cross-project engineering memory layer for coding agents.** Served over
MCP, gated on project fingerprint and prompt intent, and measured against a
hand-labelled evaluation set from the first commit.

[![CI](https://github.com/ParthBiyani/ProjectMind/actions/workflows/ci.yml/badge.svg)](https://github.com/ParthBiyani/ProjectMind/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## The problem

Coding agents start every task with amnesia. They do not know you already
solved this auth pattern in a different repository, in a different language,
eight months ago. They do not know you tried Riverpod and abandoned it.

Per-repository context — a rules file, a codebase index — cannot close that gap
and structurally never will, because it is scoped to the repository it lives
in. The gap is **cross-project** and **personal**.

## What it does

Two kinds of memory, separated by what it costs to be wrong about them.

| | **Profile** | **Episodic** |
|---|---|---|
| Content | how you work, what you reach for, what you abandoned | what happened, on which project, when, why |
| Volume | 50–200 statements | thousands of records |
| Written by | a human, always | continuous ingestion |
| Injected | every task, fingerprint-filtered | only when the gate fires |
| Wrong record costs | every future task | one task |
| Confidence bar | ≥ 0.85 to activate | ≥ 0.6 to serve |

An agent calls one tool, `get_context(prompt, project_path)`, and gets back at
most 2300 tokens — or nothing, which is a correct and common answer.

## Measured

Scored against 40 hand-labelled queries ([`eval/queries.yaml`](eval/queries.yaml)),
ten of which must return **nothing**. Full method and caveats in
[docs/evaluation.md](docs/evaluation.md).

| Metric | Target | Result | |
|---|---|---|---|
| Gate precision (inject when useful, not when not) | ≥ 0.80 | **1.000** | met |
| Retrieval precision @5 | ≥ 0.70 | **0.750** | met |
| Cross-project hit rate | ≥ 0.30 | **0.300** | met |
| False injection rate | ≤ 0.10 | **0.174** | **missed** |
| Near-misses served (`forbidden` labels) | — | 0.000 | |
| Bundle size | ≤ 2300 tokens | 667 max, 384 mean | |
| Front-door latency | — | 11 ms p95 | |

**The miss is real and not rounded away.** Of 23 episodic records served across
the whole set, four are on-topic but not in the label list; none are the
planted near-misses. Every configuration that pushed the rate under 0.15 also
dropped precision below 0.70 or the cross-project rate below 0.30 — the three
move against each other, and
[the analysis says exactly what each alternative cost](docs/evaluation.md#phase-4-results-including-what-was-missed).

On this machine's own history: **11 repositories, 190 commits, 82 episodic
records** in 1.7 seconds, with no network call and no API token. The reflection
loop's entity prefilter discards **99.1%** of statement-record pairs before any
rule runs, which is what makes contradiction detection affordable at all.

## Quickstart

Needs Python 3.11+. No database server, no API key, no network.

```bash
git clone https://github.com/ParthBiyani/ProjectMind.git
cd ProjectMind
uv venv && uv pip install -e ".[mcp,reflection]"

projectmind init                       # create the store, load 50 starter statements
projectmind profile review             # approve the ones you agree with
projectmind ingest local ~/Projects    # read your git history, offline
projectmind install claude-code        # wire in the hook and the MCP server
projectmind doctor                     # confirm
```

Restart your agent. That is the whole setup.

> Seeded statements load as **proposals** and are never served until approved.
> A profile statement is injected into every task, so nothing gets in without a
> human saying yes. Read them, then `projectmind seed --activate-all` if you
> accept the lot.

## See what it would inject

```bash
$ projectmind context "Add Supabase auth to this Flutter screen" --project .

## ProjectMind context

### How this developer works
- Reaches for Flutter for anything with a client-side UI...
- Uses Supabase for backend-as-a-service, getting auth, Postgres and storage...
- Abandoned Firebase and Firestore for anything with relational reads...

481 tokens (481 profile + 0 episodic) · 22.3 ms · profile slice: 15 of 50
entities matched: auth, flutter, supabase
```

```bash
$ projectmind context "Rename this variable" --project .
nothing injected — skipped: trivial prompt (mechanical edit)
```

## How it works

```
              ┌─────────────────────────────────────────────────┐
  prompt ────►│                  get_context                    │
project_path  │                (the front door)                 │
              └───────┬─────────────────────────────┬───────────┘
                      ▼                             ▼
             ┌─────────────────┐          ┌───────────────────┐
             │   fingerprint   │          │  prompt analysis  │
             │ manifests, deps │          │ task type, named  │
             │ cached, no LLM  │          │ entities, novelty │
             └────────┬────────┘          └─────────┬─────────┘
                      └──────────┬──────────────────┘
                                 ▼
                        ┌─────────────────┐
                        │ relevance gate  │  may return nothing
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │  BM25 + vector  │  fused, then cut
                        └────────┬────────┘
                                 ▼
                        ┌─────────────────┐
                        │ rank and budget │  cross-project boost, hard caps
                        └────────┬────────┘
                                 ▼
                          ContextBundle
```

Four properties hold everywhere:

1. **One front door.** Gating, ranking and budgeting are server-side, so
   retrieval quality does not vary by which agent is driving — and can be
   measured at all.
2. **Degrade to nothing, never to wrong.** A missing key, an unreachable
   database or an unparsable manifest reduces what is served. It never turns a
   correct answer into an incorrect one.
3. **Provenance on everything.** Every served record links to the commit or
   pull request it came from. An extracted *why* is labelled as inference.
4. **Nothing interrupts a working session.** All human review is batched.

`docs/architecture.md` has the module map.

## Default injection, honestly stated

MCP is pull-based: a server cannot push into a context window. So "injected by
default" is one mandatory front-door tool plus a client hook. `projectmind
install claude-code` adds a `UserPromptSubmit` hook whose stdout becomes
context, making the call invisible. Where a client has no hook,
`projectmind install rules` prints a line for the rules file.

If neither is wired, the agent gets no memory and behaves normally. The
fallback is always toward silence. Details and the exact files touched:
[docs/integration.md](docs/integration.md).

## Commands

| Command | What it does |
|---|---|
| `projectmind init` | Create the store, run migrations, load the starter profile |
| `projectmind context <prompt>` | Show exactly what the agent would receive |
| `projectmind doctor` | Check storage, embeddings, MCP, hooks, git |
| `projectmind report` | Injection rate, tokens served, latency, feedback |
| `projectmind profile list / review / show / sweep` | Inspect and curate the profile |
| `projectmind fingerprint scan <dir> --children` | Index a folder of repositories |
| `projectmind fingerprint neighbours` | Which projects look like this one, and why |
| `projectmind ingest local <dir>` | Read git history offline |
| `projectmind ingest github` | Read merged pull requests (needs `GITHUB_TOKEN`) |
| `projectmind ingest status / show` | What episodic memory holds |
| `projectmind review run / start / history` | The reflection loop and monthly review |
| `projectmind eval run --system live` | Score the front door against the query set |
| `projectmind install claude-code` | Hooks and MCP registration, with backups |
| `projectmind mcp` | Run the MCP server on stdio |

## MCP surface

| Tool | Purpose |
|---|---|
| `get_context(prompt, project_path, ignore_profile=false)` | The front door. Call once per task. |
| `search_memory(query, project_path, limit)` | Direct search, when the user asks what they did before. |
| `memory_feedback(bundle_id, useful, note)` | Whether a bundle helped. The only source of a live false-injection number. |
| `memory_stats(days)` | Usage and assist figures as JSON. |

## Configuration

Everything is optional. Copy [`.env.example`](.env.example) to `.env`.

| Variable | Default | Notes |
|---|---|---|
| `PROJECTMIND_HOME` | `~/.projectmind` | Database, cache, logs |
| `PROJECTMIND_DB_URL` | unset | A `postgresql://` URL switches to Postgres + pgvector |
| `ANTHROPIC_API_KEY` | unset | Enables LLM extraction; heuristics are used without it |
| `GITHUB_TOKEN` | unset | Only for the GitHub source; local ingestion needs none |
| `PROJECTMIND_EXCLUDED_REPOS` | empty | Globs never read, checked before any request |
| `PROJECTMIND_CROSS_PROJECT_BOOST` | `1.45` | Multiplier on results from other repositories |
| `PROJECTMIND_EMBEDDING_PROVIDER` | `auto` | `fastembed` when installed, else a deterministic fallback |

Full list with rationale: [`config.py`](src/projectmind/config.py).

## Storage

SQLite by default, because a memory server that needs a daemon is unavailable
exactly when you reach for it. Point `PROJECTMIND_DB_URL` at Postgres and the
same code runs against pgvector instead — one `Store` protocol, two backends,
one parity suite that runs against both in CI.

```bash
docker compose up -d
export PROJECTMIND_DB_URL=postgresql://projectmind:projectmind@localhost:5433/projectmind
projectmind init
```

## Privacy

- Prompts are **hashed, never stored**. The bundle log keeps a digest, a word
  count, the served record ids and the gate's reason — enough to score the
  gate, not enough to reconstruct a session.
- **No network call on the retrieval path at all.**
- Repository content reaches an external model only during ingestion, only if
  `ANTHROPIC_API_KEY` is set, and never for repositories matching
  `PROJECTMIND_EXCLUDED_REPOS`, which is checked before any request is made.

## Roadmap

| Phase | Status |
|---|---|
| 0 — Eval harness | complete |
| 1 — Profile memory, always-on injection | complete |
| 2 — Project fingerprinting and the gate | complete |
| 3 — Cross-project episodic ingestion | complete |
| 4 — Retrieval quality | 3 of 4 targets met |
| 5 — Reflection loop and monthly review | complete; acceptance rate not yet measurable |
| 6 — Browser and AI chat capture | designed, deliberately unbuilt |

[docs/roadmap.md](docs/roadmap.md) records what each phase measured.

## Development

```bash
uv pip install -e ".[dev]"
pytest                  # 394 tests; the Postgres half skips without a server
ruff check . && ruff format --check .
mypy                    # strict
```

CI runs lint, strict type checking, tests on 3.11 and 3.12, and the storage
parity suite against a real pgvector container.

## License

MIT — see [LICENSE](LICENSE).
