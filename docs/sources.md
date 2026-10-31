# What ProjectMind reads

ProjectMind answers from what you have already written down or done. Every source
below becomes **cited passages**: text copied from the source, with the place it
came from. Nothing is summarised, and nothing is served without a citation.

| Source | Where it comes from | Cited as |
|---|---|---|
| Git history | every repository under your folders, incrementally | commit URL and date |
| Notes and logs | `CLAUDE.md`, READMEs, plans, `.txt` and `.md` | `path:first-last` (exact lines) |
| Scripts | header comments and docstrings of `.py` files | `path:first-last` |
| Notebooks | markdown cells, code headers and text outputs | `notebook.ipynb (cell 7)` |
| Training logs | only the lines that report a measured result | `path:first-last` |
| Result files | small `.json` files in `experiments/`, `results/` and similar | `path (json)` |
| Documents | PDF, Word and PowerPoint | `file.pdf (page 3)`, `(slide 2)` |
| Claude Code sessions | what you asked and the closing reply, per turn | `session.jsonl (turn 19, date)` |
| Browser history | allow-listed pages and the searches that led to them | the page URL |
| Browser extension | page text read for 20 s on allow-listed sites | the page URL and date |

## Choosing folders

```bash
projectmind sources add "D:/Projects"                 # each child folder is a project
projectmind sources add "D:/Project Eris" --kind competition
projectmind sources add "D:/PineLabs" --kind work --depth 0   # the folder is one project
projectmind sources add "~/.claude/projects" --kind transcripts --depth 0
projectmind sync                                      # read everything that changed
```

`--depth` is how many levels below the folder a project lives. A new project or
competition folder dropped under a source is picked up on the next sync with no
configuration. A file inside a git repository belongs to that repository's
project, so its notes and its commits are one project.

## What is never read

The reader is a **whitelist**. It names the kinds of file that carry knowledge and
ignores everything else, whatever it is called.

- Datasets, images, audio, arrays, archives and model weights; build output,
  `node_modules`, virtual environments, caches.
- Agent configuration (`.claude`, `.cursor`, `.codex`, ...): third-party skill
  documentation, not your writing. A project's own `CLAUDE.md` at its root is read.
- `.env*`, key and certificate files, `credentials*`, `secret*`, lock files,
  `requirements*.txt`, licences.
- A folder holding hundreds of files of one type (a YOLO label folder, an export of
  per-item records) is treated as a data dump and skipped as a group.
- Files larger than the per-type ceiling (2 MB for text; 30 MB for PDFs).
- Files modified in the last 30 minutes: they are probably being edited, and a scan
  must not race a running editor or agent. They are picked up on the next pass.

**Identity and financial documents are skipped by name.** A file or folder called
Aadhaar, PAN card, passport, voter ID/list, driving licence, marksheet, KYC, bank
statement, payslip, Form 16, "Personal Docs" and similar is never opened.

**Credentials and personal identifiers are dropped, not redacted.** Any chunk that
looks like it holds an API key, token, private key, an assigned password, an
Aadhaar, PAN or SSN, or a payment card number is discarded whole. A redacted chunk
would still say where the secret was. The identifier rules are deliberately narrow
(a card number must be printed like a card or sit next to the word; a bare run of
digits never counts) because experiment logs are full of digit runs and a rule that
deletes them would delete real knowledge.

**Auditing what is already indexed.** `projectmind sources audit` re-applies
today's rules to everything in the index and `--fix` removes what they would
refuse. Run it after upgrading.

## Nothing is written to a source

A scan lists folders, stats files and reads them. It does not modify a file, does
not touch version control state (git is invoked only for `log`, `rev-parse` and
`remote`), and writes only to ProjectMind's own database.

## How a passage is chosen

For every prompt that is not a mechanical edit, the question is reduced to its
distinctive terms and each is weighted by how rare it is in your material. A
passage is served only if

1. it contains enough of the question's weight (default 60%),
2. it contains one of the question's **two most specific** terms (one, not both: a
   stray rare word in a question must not veto a passage that matches everything
   else, but generic words alone are never enough), and
3. it is not a near-duplicate of a passage already chosen.

A project named in the question ("the prostate MRI task") is preferred. A term the
material has never seen counts against every passage, which is why a question
about something you have never written about returns nothing, not the nearest
unrelated text. The cost of that rule is real: if you ask in words your notes
never use ("rate limits" for notes that say "429"), the answer is silence. See
[evaluation.md](evaluation.md) for how often that happens.

Optional [semantic search](semantic.md) adds passages that mean what the question asks
in other words, under a strict bar for automatic injection and labelled as "by meaning"
in explicit search (`projectmind sources search`, the web UI's Search view, the
`search_sources` MCP tool).

Every decision, including the candidates that were cut and why, is recorded in the
interaction history: `projectmind history show <id>`, or the web UI's History view.
