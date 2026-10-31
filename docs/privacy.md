# Privacy

ProjectMind reads folders full of your work, your conversations with coding agents
and, if you allow it, parts of your browsing. This page states exactly what it does
with them. Each statement is enforced in code and covered by a test.

## It stays on your computer

- The database is a file in `~/.projectmind` (or a Postgres you point it at).
- **There is no network call on the retrieval path**, and none in ingestion unless
  you set `GITHUB_TOKEN` (pull requests). Semantic search runs a local model; its one
  network call is downloading that model once, when you install it.
- The daemon listens on `127.0.0.1` only. Every request needs a bearer token held
  in a file only your account can read; a foreign `Host` header is refused (this
  stops a web page reaching it under its own name); no CORS headers are ever sent.

- The **web UI** is served by the same daemon on loopback. Its static files carry no
  data; every API call needs the token, which reaches the page in the URL fragment
  (never sent to a server) and is then removed from the address bar. The page makes
  no external requests and cannot be framed. `projectmind ui --demo` shows a fictional
  workspace, so a demo exposes nothing of yours.

## What is stored

| | Default | Notes |
|---|---|---|
| Passages from your files | yes | verbatim, with citation; credentials dropped |
| Every question and answer | yes (`PROJECTMIND_HISTORY=full`) | for audit and replay. `minimal` keeps hashed metadata; `off` keeps nothing |
| Browser history | **off** | every site except the never-capture list and this machine; no query string or fragment |
| Passage embeddings (semantic search) | **off** | numbers derived from the passages, stored beside them; removed with their document |
| Browser page text | **off** | needs the extension and `browser enable`; every site except the never-capture list, this machine, and pages asking for a password or card number |

Projects matching `PROJECTMIND_EXCLUDED_REPOS` are never read, and their
interactions are stored as `minimal` whatever the setting.

## Browser rules

- Reading is **opt-in** (`projectmind browser enable`) and begins with
  `browser scan --dry-run`, which counts what would be stored and stores nothing.
- Every site is read **except the never-capture list**, which starts with banking,
  brokers (Kite/Zerodha, Fyers and others), payments, mail, chat, social media, sign-in
  pages, password managers, government identity and tax sites, health, personal
  documents, cloud consoles and AI chats. Edit it in the extension's options, with the
  popup's "Never capture this site", or `projectmind browser deny / allow`. Adding a
  site also deletes what was already read from it.
- **This machine and the local network are never read**, whatever the list says:
  localhost, private addresses and `.local` names, where dashboards and development
  servers (including ProjectMind's own) live. The extension's manifest excludes them.
- A page that **asks for a password or a card number** is not read, on any site.
- Until the extension has received the list from the daemon it reads nothing
  (fail closed), and it never runs in private windows.
- A **search query is kept only if you opened a readable page within minutes of it.**
- The history database is **copied** and the copy is read; the browser's own file is
  never opened for writing.
- The list beats "Remember this page". The daemon applies every rule again; the
  extension is a convenience, not the boundary.

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
