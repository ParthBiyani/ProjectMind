# ProjectMind

**A memory for your coding agent that answers from your own work: your notes, your
commits, your competition logs, your past sessions and what you read, quoted
verbatim with the exact place it came from, and silent when it has nothing.**

[![CI](https://github.com/ParthBiyani/ProjectMind/actions/workflows/ci.yml/badge.svg)](https://github.com/ParthBiyani/ProjectMind/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![License](https://img.shields.io/badge/license-MIT-green)

---

## The problem

A coding agent starts every task with amnesia. It does not know you solved this in
another repository eight months ago, that the competition notes say exact voxel
matching makes boundary F1 brutal, or that you dropped a column from a report last
week and why. Per-repository context cannot close that gap, because it is scoped to
the repository it lives in. The gap is **cross-project** and **personal**.

A memory that fills the gap by guessing makes things worse. So the design rules are
about restraint:

1. **Verbatim and cited.** What is served is copied from a source, with the file and
   line numbers (or commit, page, cell, turn, URL). Nothing is summarised.
2. **Relevant or silent.** A passage is served only if it covers the question's
   distinctive terms, including one of its two most specific. Ask about something you have
   never written about and the answer is nothing, not the nearest unrelated text.
3. **Private by construction.** Everything stays on your machine. Credentials,
   identity documents and personal identifiers never enter the index. Browser
   reading is opt-in and allow-listed.

## See it

```bash
projectmind demo        # a guided tour on a fictional workspace; touches nothing of yours
```

Abridged output:

```
 3/7  Your own git history
 you ask   Why did we drop the profit factor column?
 -> git://ledger-desk/commit/c4a9274 (2026-10-30)
    | [failure] drop the profit factor column from the variations table
    | outcome: fixed in this commit

 4/7  Knowledge from another project
 you ask   How should the client back off when the provider rate limits us?   (in pulse-api)
 -> echo-notes/DECISIONS.md:1-7
    | When the provider rate limits us (HTTP 429), the client backs off exponentially with
    | jitter, capped at 30 seconds, and gives up after five attempts. ...

 7/7  Nothing to say
 you ask   Tune the Kubernetes ingress certificate rotation
 -> nothing served   (no passage covers the question)
```

## What it reads

| Source | Cited as |
|---|---|
| Git history, incrementally | commit URL and date |
| Notes, plans, READMEs, `CLAUDE.md` logs | `path:first-last` (exact lines) |
| Scripts (header comments, docstrings), training logs (result lines), small result files | `path:first-last` |
| Notebooks (markdown, code headers, text outputs) | `notebook.ipynb (cell 7)` |
| PDF, Word, PowerPoint | `file.pdf (page 3)`, `(slide 2)` |
| Claude Code sessions: what you asked and the closing reply | `session.jsonl (turn 19, date)` |
| Browser history and pages (Edge and Chrome) | the page URL |

Details, and what is never read, in [docs/sources.md](docs/sources.md). The privacy
guarantees, each backed by a test, are in [docs/privacy.md](docs/privacy.md).

## Measured

On **my own material** (about 9,000 documents and 27,000 passages: git history,
competition logs, notebooks, PDFs, sessions, browsing), 30 questions I wrote about
real projects, 10 of them about topics that appear nowhere in the material:

| | |
|---|---|
| Correct source cited | **0.90** (18 of 20 answerable) |
| Silent on absent topics | **1.00** (10 of 10); false injection **0.00** |
| Tokens per answer / p95 latency | 552 / 86 ms in process |

Read the caveats in [docs/evaluation.md](docs/evaluation.md#v020-measured-on-real-material):
I labelled these after seeing the index, 30 queries is small, and precision-first
retrieval returns nothing when you ask in words your notes never use.

On the 40-query synthetic set (clock pinned, reproducible): gate precision 1.00,
precision@5 0.795, cross-project hit rate 0.35, **false injection 0.323 against a
target of 0.10, which is missed**. That figure and the figures previously published
for it could not be reproduced: v0.1.0 never gave its clock to the retriever, so its
evaluation drifted with the date. This is documented, not rounded away.

## Quickstart (Windows, Python 3.11+)

```powershell
git clone https://github.com/ParthBiyani/ProjectMind.git
cd ProjectMind
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[mcp,docs]"
.venv\Scripts\Activate.ps1                   # so `projectmind` is on your PATH

projectmind demo                              # see it first
projectmind init                              # create the store, load the starter profile
projectmind profile review                    # the starter statements describe the author: approve
                                              # what is true of you, then add your own with `profile add`
projectmind sources add "D:/Projects"         # what to read
projectmind sources add "~/.claude/projects" --kind transcripts --depth 0
projectmind sync                              # read it (minutes the first time)
projectmind install claude-code               # hook + MCP server, with backups
projectmind schedule install                  # start the daemon at logon
projectmind doctor                            # it runs the hook the way your agent would
```

Before pointing it at a folder, look at what is in it: do not add folders of personal
or financial documents. It skips obvious ones by name and drops identifiers, but the
safest source is one you chose on purpose. `projectmind sources audit` re-checks
what is already indexed.

Ask it something:

```powershell
projectmind context "Why did we drop the profit factor column?" --project D:\Projects\MyApp
projectmind history list            # everything it was asked and what it answered
projectmind history show <id>       # why each passage was served or cut
projectmind history replay <id>     # re-run an old question on today's code
```

### Browser (optional, off by default)

```powershell
projectmind browser scan --dry-run   # what would be stored, storing nothing
projectmind browser enable           # allow-listed documentation sites only
```

For page text, load the extension (Edge or Chrome) as described in
[extension/README.md](extension/README.md). Its manifest limits it to the same
allow-list, so the browser itself prevents it reading anything else.

## How it answers

```
 hook ──localhost──► daemon (warm database)
                       │
                       ▼
 prompt ──► skip mechanical edits ──► profile slice (<= 800 tokens)
                       ├────────────► history records (gate + hybrid retrieval)
                       └────────────► source passages: rarity-weighted coverage,
                                      one of two most specific terms required, deduplicated
                                                │
                                   hard budget: 2300 tokens total
                                                │
                                  verbatim excerpts + citations
```

The module map and the design rules are in [docs/architecture.md](docs/architecture.md);
agent wiring in [docs/integration.md](docs/integration.md).

## Commands

| Command | What it does |
|---|---|
| `demo` | Guided tour on a fictional workspace (`--real -p "question"` asks your memory) |
| `init`, `doctor`, `report` | Set up, verify (runs the hook), and measure what memory did |
| `sources add / list / scan / remove / search / audit` | Choose what is read; query the raw index; re-check it against the privacy rules |
| `sync` | One incremental pass over git, folders, sessions and browser |
| `serve`, `daemon status / stop / sync / token`, `schedule install / remove / status` | The local daemon and starting it at logon |
| `context "<prompt>" -p <dir>` | Show exactly what the agent would receive |
| `history list / show / replay / label / export / purge` | Everything asked and answered |
| `profile list / add / edit / remove / review / seed / sweep` | The always-on statements about how you work |
| `browser status / enable / disable / allow / deny / scan` | Browser history, allow-listed |
| `install claude-code / rules / status` | Hook and MCP registration, with backups |
| `eval run / compare / real` | The synthetic set, and your own questions |
| `ingest local / github`, `fingerprint`, `review` | Git ingestion, project similarity, the monthly review |
| `mcp`, `export`, `hook` | MCP server on stdio, profile backup, hook entry points |

## MCP tools

| Tool | Purpose |
|---|---|
| `get_context(prompt, project_path)` | The front door. Skip it when a hook already injected a block. |
| `search_memory(query, project_path, limit)` | Direct search, when the user asks what they did before. |
| `memory_feedback(bundle_id, useful, note)` | Whether a block helped, by the id in its marker. |
| `memory_stats(days)` | Usage figures as JSON. |

## Configuration

Read from the environment and from `~/.projectmind/.env`; never from a project's `.env`.

| Variable | Default | Notes |
|---|---|---|
| `PROJECTMIND_HOME` | `~/.projectmind` | Database, sources, logs, token |
| `PROJECTMIND_DB_URL` | unset | A `postgresql://` URL switches to Postgres + pgvector |
| `PROJECTMIND_AUTHORS` | git `user.email` | Comma-separated emails whose commits are yours |
| `PROJECTMIND_EXCLUDED_REPOS` | empty | Globs never read; their interactions are stored as `minimal` |
| `PROJECTMIND_HISTORY` | `full` | `full`, `minimal` (hashed metadata) or `off` |
| `PROJECTMIND_EMBEDDING_PROVIDER` | `auto` | Thresholds are calibrated for `hashing`; set it explicitly |
| `PROJECTMIND_DAEMON_PORT` | `47615` | Fixed so the browser extension can find it |
| `PROJECTMIND_DAEMON_SYNC_MINUTES` | `30` | `0` turns the scheduler off |
| `PROJECTMIND_EVIDENCE_COVERAGE_FLOOR` | `0.6` | How much of a question a passage must cover |
| `GITHUB_TOKEN` | unset | Only for `ingest github` |

Full list with rationale: [`config.py`](src/projectmind/config.py).

## Storage

SQLite by default (a memory that needs a daemon is unavailable exactly when you reach
for it); Postgres with pgvector behind the same `Store` protocol and the same parity
suite, run against a real container in CI.

## Status and known gaps

v0.2.0. Details in [docs/roadmap.md](docs/roadmap.md#known-gaps-after-v020).

- Vocabulary mismatch means silence: retrieval is lexical with stemming.
- The history-record path misses its false-injection target (see above).
- LLM-written notes are not implemented; nothing in this version calls a model.
- The browser extension is tested for logic and manifest safety, not yet in a browser in CI.

## Development

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
pytest                  # 730+ tests; the Postgres half skips without a server
ruff check . ; ruff format --check .
mypy                    # strict
node --test extension/test/lib.test.js
```

CI runs lint, strict type checking, tests on Python 3.11, 3.12 and 3.13, a Windows
job, and the storage parity suite against a pgvector container.

## License

MIT. See [LICENSE](LICENSE).
