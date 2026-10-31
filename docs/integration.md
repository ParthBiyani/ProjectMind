# Wiring ProjectMind into a coding agent

The goal is that the agent never has to know memory exists. This document says
how that is achieved, what it costs, and what happens when it fails.

## The honest version of "default injection"

MCP is pull-based. A server cannot push content into an agent's context window,
so "injected by default" is implemented as two things:

1. **One mandatory front-door tool.** `get_context(prompt, project_path)` is
   the only tool on the hot path. All gating, ranking and budgeting happen
   server-side, so retrieval quality does not vary by which agent is driving
   and can actually be measured.
2. **A client-side hook, where the client has one.** Claude Code runs a
   `UserPromptSubmit` hook before the model sees the prompt and appends the
   hook's stdout to the context. That makes the call automatic and invisible.

Where a client has no hook, a one-line instruction in the project rules file
does the same job less reliably. `projectmind install rules` prints it.

**Fallback behaviour is explicit.** If the hook does not run and the agent does
not call the tool, it gets no memory. The system degrades to a normal agent,
never to a wrong one.

## Install

```bash
projectmind init                    # create the database, load starter statements
projectmind profile review          # approve the statements you actually agree with
projectmind sources add "D:/Projects"   # what to read (see docs/sources.md)
projectmind sync                    # read it
projectmind install claude-code     # hooks + MCP registration
projectmind schedule install        # start the daemon at logon (Windows)
projectmind doctor                  # confirm; it runs the hook the way your agent would
```

Restart the client afterwards. Check what changed at any point with
`projectmind install status`, and undo all of it with
`projectmind install claude-code --uninstall`.

## What the installer touches

| File | Change |
|---|---|
| `~/.claude/settings.json` | Appends one `UserPromptSubmit` entry and one `SessionStart` entry under `hooks`. |
| `~/.claude.json` | Adds a `projectmind` entry under `mcpServers`. |

Both edits are **additive and idempotent**. Existing hooks from other tools are
left exactly where they are, every file is backed up to
`<name>.bak-projectmind-<timestamp>` before being written, and running the
installer twice changes nothing the second time. A config file that is not
valid JSON aborts the install rather than being overwritten.

The hook command goes through the interpreter rather than the console script,
in isolated mode, to a slim entry point that does not import the CLI:

```
"<python>" -I -m projectmind.hook_entry user-prompt-submit
```

`-I` keeps the project's own files off the import path: a hook runs with the
user's project as its working directory, and a project containing a `yaml.py` or a
`typer/` folder must not be able to shadow the real module.

A console script only resolves if its directory happens to be on `PATH`, which
it frequently is not inside an editor's hook subprocess.

## The two hooks

**`UserPromptSubmit`** is the injection path. It reads the hook event from
stdin (as UTF-8 bytes; on Windows a piped stream is otherwise the ANSI code page),
gets the same `MemoryService.get_context` answer the MCP tool returns, and replies
with ASCII-only JSON (`hookSpecificOutput.additionalContext`) ending in a
`<!-- projectmind bundle <id> -->` marker, so the agent can report whether the
context helped. Silence is the common case: trivial prompts, a question nothing
covers, or an empty profile all produce no output.

It works in two tiers. If the daemon is running (`projectmind serve`), the hook is
a few lines of standard library that ask it over localhost; the database is already
open and the index warm. If not, it does the work itself. Either way the answer is
the same, and a hook that cannot get one exits 0 and says nothing.

**`SessionStart`** warms the project cache so the first prompt of a session is
not the slow one. It prints nothing, deliberately — a session-start hook that
writes into the context window is a session-start hook that gets uninstalled.

Three rules govern both, because a misbehaving hook breaks the editor rather
than just this tool:

1. Never raise. Every failure path exits 0 with empty output.
2. Never print anything but context. Diagnostics go to
   `PROJECTMIND_HOME/logs/projectmind.log`.
3. Never block. The work is bounded; the client's timeout is the backstop.

## Cost per prompt

| | |
|---|---|
| Added latency | about 150 ms through the daemon, about 160 ms warm in process and up to a few seconds on a cold first run (measured; interpreter start-up alone is about 70 ms) |
| Added tokens | 0 when nothing is served; at most 2300, capped server-side |
| Network calls | none on the retrieval path |

The token cap is enforced before anything is returned, so a bundle cannot grow
past its budget regardless of how much memory exists.

## Turning it off

| Scope | How |
|---|---|
| One call | `get_context(..., ignore_profile=true)` |
| A whole session | remove the hook, or `projectmind install claude-code --uninstall` |
| Permanently | the same, plus delete `PROJECTMIND_HOME` |

The escape hatch is not a nicety. A system that always tells your agent what
you usually reach for makes you faster at what you already do and quietly worse
at anything new. Deliberate exploration has to be able to run clean.

## Privacy

- The interaction history keeps, **locally**, each prompt, what was served and why
  each candidate was kept or cut, so answers can be audited and replayed
  (`projectmind history`). `PROJECTMIND_HISTORY=minimal` keeps hashed metadata only;
  `off` keeps nothing; excluded projects are always `minimal`. See
  [privacy.md](privacy.md).
- Nothing leaves the machine on the retrieval path. There is no network call in
  `get_context` at all.
- No content reaches an external model at all: optional model-written notes are not
  implemented, and semantic search runs a local model on the CPU.

## MCP tools

| Tool | When the agent should use it |
|---|---|
| `get_context(prompt, project_path)` | The front door, when no hook block is present. |
| `search_sources(query, project_path, limit)` | The injected context is missing or off-topic and the user's own files may still answer. Finds passages by words and, when semantic search is on, by meaning; each result says how it was found. |
| `search_memory(query, project_path, limit)` | The user asks what they did before (history records). |
| `memory_feedback(bundle_id, useful, note)` | Once per injected block, near the end of the task. |
| `memory_stats(days)` | Usage figures as JSON. |

## Semantic search and the hook

If semantic search is on ([semantic.md](semantic.md)), meaning matches are added only
when the hook is answered by the daemon, which holds the vectors in memory. The
in-process fallback stays lexical so a prompt never waits for a model to load.

## Other clients

Any MCP client can use the server directly:

```json
{
  "mcpServers": {
    "projectmind": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-I", "-m", "projectmind.cli", "mcp"]
    }
  }
}
```

Without a pre-prompt hook, add the snippet from `projectmind install rules` to
the project's rules file so the agent knows to call the tool first.
