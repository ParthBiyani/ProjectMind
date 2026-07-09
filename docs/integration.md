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
projectmind install claude-code     # hooks + MCP registration
projectmind doctor                  # confirm
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

The hook command goes through the interpreter rather than the console script:

```
"<python>" -m projectmind.cli hook user-prompt-submit
```

A console script only resolves if its directory happens to be on `PATH`, which
it frequently is not inside an editor's hook subprocess.

## The two hooks

**`UserPromptSubmit`** is the injection path. It reads the hook event from
stdin, calls the same `MemoryService.get_context` the MCP tool calls, and prints
the rendered markdown. Silence is the common case: trivial prompts, a project
with no matching memory, or an empty profile all produce no output.

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
| Added latency | roughly 300–600 ms, almost all of it Python interpreter startup |
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

- The prompt is **hashed, never stored**. The bundle log keeps a digest, the
  word count, the served record ids and the gate's reason — enough to score the
  gate, not enough to reconstruct a session.
- Nothing leaves the machine on the retrieval path. There is no network call in
  `get_context` at all.
- Repository content reaches an external model only during ingestion, only if
  `ANTHROPIC_API_KEY` is set, and never for repositories listed in
  `PROJECTMIND_EXCLUDED_REPOS`.

## Other clients

Any MCP client can use the server directly:

```json
{
  "mcpServers": {
    "projectmind": {
      "type": "stdio",
      "command": "/path/to/python",
      "args": ["-m", "projectmind.cli", "mcp"]
    }
  }
}
```

Without a pre-prompt hook, add the snippet from `projectmind install rules` to
the project's rules file so the agent knows to call the tool first.
