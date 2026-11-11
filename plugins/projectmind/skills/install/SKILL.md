---
name: install
description: Install, set up, check, update or remove ProjectMind, a local-only memory for Claude Code that answers from the user's own notes, git history, past sessions and optionally browsing, with exact citations, and stays silent when nothing fits.
when_to_use: Use when the user says things like "install ProjectMind", "set up ProjectMind", "give Claude Code memory across my projects", "get ProjectMind running", or asks to fix, update or uninstall an existing ProjectMind install.
---

# ProjectMind: install and set up

ProjectMind keeps a private index of what the user has written and done (notes, `CLAUDE.md`
logs, scripts, notebooks, PDFs and Office files, commits, past Claude Code sessions and,
optionally, pages they read). Before each prompt a hook looks the question up and adds a few
verbatim, cited excerpts, or nothing if nothing covers the question. Everything stays on the
machine. No API key is needed.

Install it by working through the steps below **in order**. Show the user what each step did.
Stop and ask where a step says **ASK**; those steps read their files, change their Claude Code
configuration, or take a long time.

## Rules that do not bend

- **Look before you index.** Never add a folder you have not listed first. Never add a home
  directory, a whole drive, `Downloads`, or any folder holding identity, tax, banking, medical
  or password material. If the user names one, say why not and suggest its project subfolders.
- **Only what they choose.** ProjectMind reads read-only and the walker is a whitelist, but the
  user decides the folders. Do not guess folders from their file system without asking.
- **Credentials are redacted before anything is stored or shown**, and identity-document file
  names are never opened. That is a safety net, not permission to point it at sensitive folders.
- **Report honestly.** If a command fails, show the error and stop. Do not retry blindly or
  edit ProjectMind's files to get past it.

## 1. Preflight

Run the preflight script and read its JSON. It looks for Python, git and existing installs; it
reads none of the user's files.

```
python "${CLAUDE_SKILL_DIR}/scripts/preflight.py"
```

- Python must be 3.11 or newer. If it is not, tell the user and stop.
- `projectmind_installed: true` means this is an update or repair: go to step 8 (verify) first.
- `capabilities` says what works on this machine. The core (index, hook, MCP server, daemon)
  runs everywhere Python does. **Starting at logon and reading browser history are
  Windows-only.** It was developed and tested on Windows 11; the test suite also runs on
  Linux in CI, but say plainly that macOS and Linux are less proven.

## 2. Install the package

Prefer an isolated install. With `pipx` available:

```
pipx install "projectmind[mcp,docs,semantic] @ git+https://github.com/ParthBiyani/ProjectMind.git"
```

Otherwise a dedicated virtual environment (do not install into a project's own environment):

```
python -m venv ~/.projectmind-venv
~/.projectmind-venv/bin/python -m pip install "projectmind[mcp,docs,semantic] @ git+https://github.com/ParthBiyani/ProjectMind.git"
```

On Windows the interpreter is `~/.projectmind-venv/Scripts/python.exe`. Use that interpreter's
`-m projectmind.cli` (or the `projectmind` script beside it) for every command below. The
`semantic` extra (about 130 MB of model, downloaded on first use) is optional: drop it from the
extras if the user wants a lighter install. Check with `projectmind --version`.

## 3. Create the store

```
projectmind init --no-seed
```

Always `--no-seed`. Without it, `init` loads fifty starter statements that are the author's own
preferences; they would be proposals for the user to review, but they are not theirs.

## 4. Choose what to read (ASK)

Find candidate folders without opening files: list the top level of the places developers keep
work (a `Projects` or `dev` or `src` folder, a notes folder) and show the user the names. Then
ask which to include, and for each:

```
projectmind sources add "<folder>" --kind project --depth 1
```

- `--depth 1` means each child of the folder is a project; `--depth 0` means the folder itself is
  one project. Kinds: `project`, `competition`, `work`, `learning`, `career`, `transcripts`.
- Offer their past Claude Code sessions (they are often the richest source):
  `projectmind sources add "~/.claude/projects" --kind transcripts --depth 0`
- To keep named files or folders out: `--ignore "MyApp/private-notes.*"` (repeatable), or a
  `.projectmindignore` file in a folder, one glob per line.
- `projectmind sources list` shows what is configured.

## 5. First read

```
projectmind sync
```

This can take minutes on a large set of folders. Then check it:

```
projectmind sources list
projectmind sources audit
```

`audit` should say nothing is refused. If it lists anything, show it to the user and run
`projectmind sources audit --fix` only if they agree.

## 6. Meaning-based search (ASK, optional)

It lets a reworded question find a note that uses different words. The one-off embedding takes
roughly 25 minutes per 28,000 passages on a laptop CPU and runs in the background.

```
projectmind semantic enable
projectmind semantic index
```

## 7. Connect it to Claude Code (ASK)

This edits the user's Claude Code configuration (`settings.json` for the hook and
`~/.claude.json` for the MCP server), with backups. Say so, then:

```
projectmind install claude-code
```

Tell the user to **restart Claude Code** afterwards. To start the background service at logon
(Windows only): `projectmind schedule install`. Elsewhere, `projectmind serve` runs it in the
foreground; the hook works without it, just slower.

## 8. Verify

```
projectmind doctor
projectmind context "<a real question about one of their projects>" --project "<that project's folder>"
```

`doctor` runs the hook exactly as Claude Code will and reports anything wrong. A `--` line is
advice, not a failure: an empty profile ("0 active statements") is normal for a new install.
A good test question names something they really wrote down; a question about nothing they
have should return "nothing injected", and that is correct behaviour.

## 9. Optional: browser pages (Windows, ASK)

Reads the text of pages they read in Edge or Chrome, except a never-capture list (banking, mail,
social media, sign-in, health, government identity and similar). AI chat sites are read only on
conversation pages, never their sign-in, settings or billing pages. It needs a one-time manual
step the agent cannot do for them:

1. `projectmind browser enable`
2. In `edge://extensions` or `chrome://extensions`, turn on Developer mode, choose Load unpacked
   and select the `extension` folder of the ProjectMind repository (clone it first:
   `git clone https://github.com/ParthBiyani/ProjectMind.git`).
3. `projectmind daemon token`, then paste it into the extension's Options and press Test.

## Day to day

- `projectmind ui` opens a dashboard: ask, search, every past interaction and why each excerpt
  was kept or cut, what is indexed, and the profile review.
- Claude can rate an answer with the `memory_feedback` MCP tool; ratings improve nothing yet but
  are recorded for tuning.
- `projectmind profile review` approves or rejects statements about how the user works; only
  approved ones are ever served.

## Update and remove

Update: reinstall with the same command as step 2 plus `--upgrade`, then `projectmind doctor`.

Remove (ASK before deleting data): `projectmind install claude-code --uninstall`,
`projectmind schedule remove` (Windows), `projectmind daemon stop`, uninstall the package, and
only if the user wants their index gone, delete `~/.projectmind`.
