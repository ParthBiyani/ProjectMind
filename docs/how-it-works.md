# How ProjectMind works

A plain-language explanation of what ProjectMind is, the problem it solves, how it was
built, the decisions behind it and the numbers that support them. No background is
assumed. For the module map see [architecture.md](architecture.md); for the evaluation in
detail see [evaluation.md](evaluation.md).

## 1. The problem

A coding agent (Claude Code, Cursor and similar) starts every task with amnesia. It does
not know you fixed this bug in another repository, that your competition notes explain why
a metric is low, that a commit last week dropped a column and why, or that you read the
documentation page that answers the question yesterday.

Per-repository context (a rules file, a codebase index) cannot close that gap, because it
only sees one repository. The missing knowledge is **cross-project** and **personal**.

Filling the gap by guessing is worse than leaving it. A confidently injected, unrelated
passage steers the agent wrong, and nobody notices. So ProjectMind is built on three rules:

1. **Verbatim and cited.** Text is copied from a source with the exact place it came from
   (`notes.md:12-19`, a commit URL, a PDF page, a notebook cell, a session turn, a URL).
   Nothing is summarised.
2. **Relevant or silent.** If nothing covers the question, nothing is served.
3. **Private by construction.** Everything stays on your machine. Credentials, identity
   documents and personal identifiers never enter the index.

## 2. A few terms

| Term | Meaning |
|---|---|
| Context window | Everything the model sees for one answer. Finite, and irrelevant text makes answers worse. |
| Token | The unit a model reads (about ¾ of a word). ProjectMind's answer is capped at 2,300. |
| Hook | A command Claude Code runs before the model sees your prompt; its output is added to context. |
| MCP | Model Context Protocol: a standard way for an agent to call another program's tools. |
| Lexical retrieval | Finding passages that contain the question's words (BM25, SQLite FTS5). |
| Embedding | A list of numbers representing meaning; similar texts are close (cosine similarity). |
| IDF | How rare a word is across the collection. Rare words carry more information. |
| False injection | Serving something irrelevant. The failure that matters most. |

## 3. What happened: the first version passed its tests and failed in real use

Version 0.1 had 700+ passing tests and a 40-query evaluation. Run against a real machine,
it barely worked:

| Found | Cause | Fix |
|---|---|---|
| Read 1 of 14 git repositories | Git's "dubious ownership" refusal was reported as "not a git repository" | Per-command `safe.directory`, real error reasons: 14 of 14 |
| Knew only commit messages | Git was the only source | A knowledge base over notes, scripts, notebooks, logs, PDF/Word/PowerPoint, agent sessions and browser pages |
| Answers mostly empty | Retrieval was gated by a closed list of technology words | Open-vocabulary passage retrieval |
| Invented "why" fields | Templates filled gaps | Removed: no stated reason, no "why" |
| Memory expired with time | Recency multiplier vs an absolute floor; the evaluation used the wall clock | Recency as a tie-breaker; the evaluation clock is pinned |
| Corrupted non-ASCII text in the hook | Windows pipes use the ANSI code page | UTF-8 bytes in, ASCII-safe JSON out |
| Context injected twice; no feedback | Hook and MCP both injected; no bundle id | One path, a bundle id marker, a feedback tool |

The lesson behind the rest of the work: **every decision was checked against real data**, not
only the synthetic test set.

## 4. How knowledge gets in

```
folders ──► walker ──► chunker ──► secret / identifier scan ──► index
git ──────► commit extractor ───────────────────────────────────┘
sessions ─► turn distiller ─────────────────────────────────────┘
browser ──► history and pages, minus never-capture ──────────────┘
```

- **Walker**: a whitelist of file types that carry knowledge. Skips datasets, build output,
  agent configuration folders, any folder with more than 150 files of one type (a data dump),
  oversized files, and files changed in the last 30 minutes (so a scan never races an editor).
  Files named like identity or financial documents are never opened.
- **Chunker**: cuts documents on headings, cells, pages and definitions, keeping exact line
  spans or locators, so every passage can be cited.
- **Scan**: any passage that looks like it holds a credential or a personal identifier is
  **dropped whole**, never redacted. The rules are narrow on purpose: an early card-number rule
  flagged 333 passages of experiment logs (digit runs are everywhere in ML logs) and was
  narrowed to grouped or keyworded numbers with a valid issuer and checksum.
- **Index**: SQLite full-text search (FTS5, stemming, Unicode-aware) by default; Postgres
  behind the same interface.
- **Growth**: `projectmind sync` is incremental; the daemon runs it every 30 minutes at low
  priority. New project folders are picked up automatically.

## 5. How a question is answered

1. **Is it trivial?** Mechanical edits (rename, reformat, fix a typo) are skipped before
   anything is searched. A deterministic classifier, no model call.
2. **Weigh the question's words by rarity.** Common words count for little; a word that
   appears in a handful of passages counts for a lot.
3. **Score each candidate passage by coverage**: the share of the question's weight that the
   passage contains. Coverage is between 0 and 1, so a fixed floor means the same thing in any
   collection. That is what makes "serve nothing" a decidable answer.
4. **Apply the rules.** Coverage at least 60% (35% if the question names the passage's
   project); contains one of the question's two most specific words; at most two passages per
   document; no near-duplicates; at most four passages.
5. **Optionally add meaning matches** (semantic search, section 7).
6. **Budget**: 800 tokens profile, 1,500 history records, 1,200 passages, 2,300 total.
7. **Record everything**: the prompt, every candidate with its score and why it was kept or
   cut, and the text served. This history is what makes the system auditable and is the source
   of future evaluation data.

## 6. The numbers

Measured on a real knowledge base of about 9,000 documents and 27,000 passages, with 42
questions: 20 in the sources' own words, 12 deliberately reworded, and 10 about topics that
appear nowhere in the material.

| | Result |
|---|---|
| Right source cited, own words | **0.900** |
| Right source cited, reworded | **0.667** |
| Right source cited, all answerable (lexical) | **0.812** |
| With semantic search on (default bar) | **0.844** |
| Silent on absent topics | **1.000** (false injection 0.000) |
| Explicit search finds the right source | **30 of 32** (28 by words alone) |
| Hook latency | about 150 ms through the daemon |

On the reproducible 40-query synthetic set: gate precision 1.000, precision@5 0.750,
cross-project hit rate 0.325, false injection 0.174 against a target of 0.10 (missed, and
stated as missed).

## 7. Semantic search: the decision the data made

After the lexical fixes, the clearest remaining weakness was **vocabulary mismatch**:
reworded questions hit 8 of 12. "Heart cycle" does not find "Cardiac Cycle".

All 27,000 passages were embedded with a small sentence model (about 25 minutes on a laptop
CPU). The nearest passages by meaning found the right document for **all 12** reworded
questions. But questions about absent topics also scored high: their best similarity was
0.67–0.78, while correct answers scored 0.72–0.93. A similarity score says "same field", not
"answers the question".

| Threshold | Hit rate | Silence on absent topics |
|---|---|---|
| none (lexical only) | 0.812 | 1.000 |
| 0.72 | 0.938 | 0.70 |
| 0.76 | 0.875 | 0.90 |
| **0.78 (default)** | **0.844** | **1.000** |

So meaning is used twice, with two bars: automatic injection keeps a strict one (0.78, close
to the best match, and sharing a distinctive word), while explicit search shows near matches
labelled "by meaning, similarity 0.74". See [semantic.md](semantic.md).

## 8. Key decisions

| Decision | Why | Evidence |
|---|---|---|
| Copy, never summarise | A summary can be wrong and cannot be checked | Every served excerpt is verifiable at its citation |
| Silence is an answer | Wrong context is invisible damage | 10/10 absent topics silent |
| Coverage, not raw rank | A rank has no absolute meaning, so it cannot say "nothing" | Hit rate from near zero to 0.81 with silence held |
| Two anchor words, not one | A single stray rare word vetoed correct answers | Robustness tests |
| Named-project floor | Naming a project is strong evidence | Reworded hits 5/12 → 8/12 |
| Remove a rule rather than tune it | Threshold sweeps could not fix false injection from vague prompts | 0.323 → 0.240 → 0.174 |
| Recency as tie-break, pinned eval clock | Memory must not expire; results must reproduce | Precision used to decay 0.80 → 0.0 over a year |
| SQLite by default | A memory that needs a server is unavailable when you need it | Parity suite on SQLite and Postgres |
| Daemon plus thin hook | Start-up and imports per prompt were the cost | ~150 ms |
| No model on the hot path, no API key | Latency, cost, privacy | Every feature works offline |
| A web UI with a demo mode | Demos must be live and must not show personal data | `projectmind ui --demo` |

## 9. Where it runs today

One user, one machine. A SQLite file in `~/.projectmind`, a daemon started at logon, a
Claude Code hook and MCP server, an optional browser extension and a local web dashboard.
This is deliberate: the material is private, and nothing needs to leave the machine.
How the same design would serve a million users is in [scaling.md](scaling.md).
