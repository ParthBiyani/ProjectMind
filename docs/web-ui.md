# Web UI

A dark, dependency-free dashboard over ProjectMind, served by the local daemon. It exists
for two reasons: to make every decision visible (what was served, and why each candidate
was kept or cut), and to give a live demo without opening a new project or showing personal
data.

## Start it

```bash
projectmind ui                # opens the dashboard of the running daemon
                              # (starts the daemon in the foreground first if it is not running)
projectmind ui --demo         # a fictional, fully indexed workspace on a free port
projectmind ui --no-browser   # print the URL instead of opening it
```

`--port` fixes the port (`0`, the default for `--demo`, picks a free one). A link can open
straight onto an answer: `#/ask?q=<question>&p=<folder>`, or `#/ask?s=<n>` for the n-th
suggested question, which is how a demo can start on a result. `#/history?id=<id>` (or
`id=latest`) opens one interaction, and `#/tour?run=1` starts the demo tour.

The dashboard is at `http://127.0.0.1:<port>/ui/`. It is plain HTML, CSS and JavaScript: no
build step, no CDN, no external request of any kind.

`--demo` uses the same workspace as `projectmind demo`: four fictional projects
(harbor-vision, ledger-desk, echo-notes, pulse-api), a git history, a recorded agent session
and a captured web page. Nothing of yours is read or shown.

## Views

| View | What it shows |
|---|---|
| **Ask** | Type a question and a project folder. See the gate decision, each cited excerpt with source type, project and line span, profile statements and history records, tokens and latency, a "why" trace of every candidate kept or cut (coverage, matched terms, reason), the exact text the agent would receive, and useful / not useful feedback. |
| **Search** | Explicit search by words and, when semantic search is on, by meaning. Every result says how it was found. |
| **History** | Every interaction, with filters. Detail shows the prompt, the served text, the trace and the feedback. |
| **Knowledge** | Documents, passages, history records and projects; passages by source type; top projects; source roots; last sync; Sync now. |
| **Profile** | Proposed, active and rejected statements. Approve or reject in place. |
| **Evaluation** | The last real-data report (hit rate, silence, precision, p95, per-query outcomes). In demo mode, "Run the tour" runs the seven scripted steps and checks each. |
| **System** | Version, daemon, semantic search status, embedder, history mode, home directory. |

## Security

The daemon is reachable by every web page you open, so:

- **Loopback only**, and the `Host` header must be `127.0.0.1` or `localhost` (stops DNS
  rebinding).
- **Static files carry no data** and need no token.
- **Every `/api/*` call needs the bearer token.** `projectmind ui` opens the page with the
  token in the URL fragment (`#token=...`). Browsers never send the fragment to a server; the
  page moves it to `sessionStorage` and removes it from the address bar.
- **Content-Security-Policy** `default-src 'self'; frame-ancestors 'none'`, so the page cannot
  load anything external or be framed.
- **No CORS headers**, so another site cannot read an answer.

## API

All JSON, all under `/api/`, all requiring `Authorization: Bearer <token>`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/overview` | Counts, version, last sync, semantic status |
| POST | `/api/ask` | `{prompt, project}` → the bundle, its trace and rendered text |
| POST | `/api/search` | `{query, project}` → results labelled by words or by meaning |
| GET | `/api/history` | Recent interactions (filters as query parameters) |
| GET | `/api/history/<id>` | One interaction in full |
| POST | `/api/feedback` | `{id, useful, note}` |
| GET | `/api/knowledge` | Documents and passages by source type and project; roots |
| GET | `/api/profile` | Statements by status |
| POST | `/api/profile/<id>/approve` · `/reject` | Review a statement |
| GET | `/api/eval` | The last real-data report |
| GET | `/api/tour` | Demo mode: run the scripted tour |
| POST | `/api/sync` | Start a sync |
| GET | `/api/suggestions` | Example questions for the Ask view |
