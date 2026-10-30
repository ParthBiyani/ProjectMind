# Privacy

ProjectMind reads folders full of your work, your conversations with coding agents
and, if you allow it, parts of your browsing. This page states exactly what it does
with them. Each statement is enforced in code and covered by a test.

## It stays on your computer

- The database is a file in `~/.projectmind` (or a Postgres you point it at).
- **There is no network call on the retrieval path**, and none in ingestion unless
  you set `ANTHROPIC_API_KEY` (optional notes) or `GITHUB_TOKEN` (pull requests).
- The daemon listens on `127.0.0.1` only. Every request needs a bearer token held
  in a file only your account can read; a foreign `Host` header is refused (this
  stops a web page reaching it under its own name); no CORS headers are ever sent.

## What is stored

| | Default | Notes |
|---|---|---|
| Passages from your files | yes | verbatim, with citation; credentials dropped |
| Every question and answer | yes (`PROJECTMIND_HISTORY=full`) | for audit and replay. `minimal` keeps hashed metadata; `off` keeps nothing |
| Browser history | **off** | allow-listed domains only, no query string or fragment |
| Browser page text | **off** | needs the extension and `browser enable`; allow-listed domains, or a page you explicitly remember |

Projects matching `PROJECTMIND_EXCLUDED_REPOS` are never read, and their
interactions are stored as `minimal` whatever the setting.

## Browser rules

- Reading is **opt-in** (`projectmind browser enable`) and begins with
  `browser scan --dry-run`, which counts what would be stored and stores nothing.
- Only pages on the **allow-list** (documentation, GitHub, Kaggle, arXiv and
  similar) are considered. Mail, banking, social media and shopping are never read.
- A **search query is kept only if you opened an allowed page within minutes of it.**
  A search that led nowhere you allowed is not stored at all.
- The history database is **copied** and the copy is read; the browser's own file is
  never opened for writing.
- `deny` beats `allow`, and beats even "Remember this page".
- The extension's manifest grants it access to the allow-listed sites **only**
  (no `<all_urls>`), so the browser itself prevents it reading anything else.

## Sessions

Claude Code transcripts are reduced to your messages and the assistant's closing
reply per turn. Tool calls, tool output, reasoning and sub-agent sessions are
ignored. Sessions modified in the last 30 minutes are skipped, so a running
session is never read while it is being written.

## Controlling it

| To | Do |
|---|---|
| see what it knows | `projectmind sources list`, `projectmind sources search "..."` |
| see every answer it gave | `projectmind history list`, `history show <id>` |
| stop reading a folder | `projectmind sources remove <folder>` (its passages are forgotten) |
| stop browser reading | `projectmind browser disable` (its pages are forgotten) |
| delete old interactions | `projectmind history purge --older-than-days 30` |
| take it out of your agent | `projectmind install claude-code --uninstall` |
| stop the daemon | `projectmind daemon stop`; `projectmind schedule remove` |
| forget everything | delete `~/.projectmind` |
