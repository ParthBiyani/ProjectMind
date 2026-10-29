"""`projectmind history ...`: everything asked of ProjectMind and what it answered.

The interaction log is how retrieval gets better from real use. `show` explains
a single answer down to every candidate that was served or cut; `replay` reruns
a stored prompt against today's code at the original moment, so a change to the
floors or the ranking can be checked against the questions that were actually
asked; `label` records whether an answer helped.
"""

from __future__ import annotations

import difflib
import json
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.models import utcnow
from projectmind.service import MemoryService
from projectmind.storage.base import BundleLogEntry, HistoryFilter

app = typer.Typer(help="Browse, replay and label past interactions.", no_args_is_help=True)

LOOKBACK = 5_000


def _resolve(memory: MemoryService, reference: str) -> BundleLogEntry | None:
    """An interaction by full id or by any unambiguous prefix."""
    cleaned = reference.strip().lower()
    try:
        return memory.store.get_bundle(UUID(cleaned))
    except ValueError:
        pass
    matches = [
        entry
        for entry in memory.store.find_bundles(HistoryFilter(limit=LOOKBACK))
        if str(entry.bundle_id).startswith(cleaned)
    ]
    if len(matches) > 1:
        fail(f"{reference} matches {len(matches)} interactions; use a longer prefix")
        return None
    return matches[0] if matches else None


def _summary(entry: BundleLogEntry) -> str:
    if entry.prompt_text:
        return " ".join(entry.prompt_text.split())
    return f"(text not kept; {entry.prompt_words} words)"


@app.command("list")
def list_interactions(
    project: str | None = typer.Option(None, help="Only this project key."),
    source: str | None = typer.Option(None, help="hook, mcp:get_context, mcp:search_memory, cli."),
    served: bool | None = typer.Option(
        None, "--served/--empty", help="Only interactions that injected, or only empty ones."
    ),
    days: int = typer.Option(0, help="Only the last N days. 0 means all."),
    limit: int = typer.Option(25, help="How many to show."),
) -> None:
    """The most recent interactions, newest first."""
    since = utcnow() - timedelta(days=days) if days else None
    with service() as memory:
        entries = memory.store.find_bundles(
            HistoryFilter(
                project_key=project, source=source, served=served, since=since, limit=limit
            )
        )
    if not entries:
        note("no interactions match")
        return

    rendered = table("id", "when", "source", "project", "served", "tok", "prompt")
    for entry in entries:
        text = _summary(entry)
        rendered.add_row(
            str(entry.bundle_id)[:8],
            entry.created_at.astimezone().strftime("%m-%d %H:%M"),
            entry.source,
            entry.project_key or "-",
            "[green]yes[/green]" if entry.injected else "[dim]no[/dim]",
            str(entry.profile_tokens + entry.episodic_tokens),
            text if len(text) <= 70 else text[:67] + "...",
        )
    console.print(rendered)
    note(f"{len(entries)} shown")


@app.command("show")
def show_interaction(
    reference: str = typer.Argument(..., help="Interaction id or prefix."),
) -> None:
    """One interaction in full: the question, the gate, every candidate, the answer."""
    with service() as memory:
        entry = _resolve(memory, reference)
    if entry is None:
        fail(f"no interaction matches {reference}")
        raise typer.Exit(code=1)

    console.print(f"[bold]{entry.bundle_id}[/bold]")
    console.print(
        f"{entry.created_at.astimezone():%Y-%m-%d %H:%M:%S} · {entry.source} · "
        f"project {entry.project_key or '-'} · {entry.latency_ms:.1f} ms · "
        f"embedder {entry.embedder or '-'} · v{entry.version or '-'}"
    )
    if entry.cwd:
        console.print(f"[dim]cwd {entry.cwd}[/dim]")
    if entry.session_id:
        console.print(f"[dim]session {entry.session_id}[/dim]")

    console.print("\n[bold]asked[/bold]")
    console.print(entry.prompt_text or f"(text not kept; {entry.prompt_words} words)", markup=False)

    decision = entry.trace.get("decision", {})
    console.print(f"\n[bold]gate[/bold] {entry.gate_reason}")
    if decision:
        console.print(
            f"[dim]task {decision.get('task_type')} · entities "
            f"{', '.join(decision.get('entities') or []) or '-'} · profile "
            f"{decision.get('inject_profile')} · episodic {decision.get('inject_episodic')} · "
            f"boost {decision.get('cross_project_boost')}[/dim]"
        )

    candidates = [
        row for row in entry.trace.get("retrieval", []) if row.get("stage") == "candidate"
    ]
    if candidates:
        console.print("\n[bold]candidates[/bold]")
        rendered = table("outcome", "score", "lex", "vec", "project", "record")
        for row in candidates:
            style = "green" if row["outcome"] == "served" else "dim"
            rendered.add_row(
                f"[{style}]{row['outcome']}[/{style}]",
                f"{row['score']:.3f}",
                f"{row['lexical']:.2f}",
                f"{row['vector']:.2f}",
                str(row["project"]),
                str(row["what"]),
            )
        console.print(rendered)

    console.print(f"\n[bold]answered[/bold] {entry.profile_tokens + entry.episodic_tokens} tokens")
    if entry.rendered_text:
        console.print(entry.rendered_text, markup=False)
    elif entry.injected:
        note("(text not kept)")
    else:
        note("nothing was injected")

    if entry.delivered is not None:
        console.print(f"\ndelivered to the agent: {'yes' if entry.delivered else 'NO'}")
    if entry.feedback is not None:
        verdict = "useful" if entry.feedback else "not useful"
        console.print(
            f"feedback: {verdict}" + (f" ({entry.feedback_note})" if entry.feedback_note else "")
        )


@app.command("replay")
def replay_interaction(
    reference: str = typer.Argument(..., help="Interaction id or prefix."),
) -> None:
    """Rerun a stored prompt on today's code, at the moment it was first asked.

    Prints whether the answer is unchanged. After changing a floor or a weight,
    replaying real questions is the check that the change did what you meant.
    """
    with service() as memory:
        entry = _resolve(memory, reference)
        if entry is None:
            fail(f"no interaction matches {reference}")
            raise typer.Exit(code=1)
        if not entry.prompt_text:
            fail("this interaction was stored without its text (history was minimal)")
            raise typer.Exit(code=1)

        bundle = memory.get_context(
            entry.prompt_text,
            entry.cwd or Path.cwd(),
            now=entry.created_at,
            log_bundle=False,
        )

    before = set(entry.profile_ids) | set(entry.episodic_ids)
    after = {item.id for item in bundle.profile} | {item.id for item in bundle.episodic}
    old_text = (entry.rendered_text or "").splitlines()
    new_text = bundle.render().splitlines()

    if before == after and old_text == new_text:
        ok(f"unchanged: {len(after)} items, {bundle.total_tokens} tokens")
        return

    console.print(
        f"[yellow]changed[/yellow]: {len(before)} items before, {len(after)} now "
        f"({len(before - after)} dropped, {len(after - before)} added)"
    )
    diff = difflib.unified_diff(old_text, new_text, "then", "now", lineterm="", n=1)
    for line in diff:
        style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
        console.print(line, style=style, markup=False)
    raise typer.Exit(code=2)


@app.command("label")
def label_interaction(
    reference: str = typer.Argument(..., help="Interaction id or prefix."),
    useful: bool = typer.Option(..., "--useful/--not-useful", help="Did the answer help?"),
    note_text: str = typer.Option("", "--note", help="Why, in a few words."),
) -> None:
    """Record whether an answer helped. This is the live false-injection signal."""
    with service() as memory:
        entry = _resolve(memory, reference)
        if entry is None:
            fail(f"no interaction matches {reference}")
            raise typer.Exit(code=1)
        memory.feedback(entry.bundle_id, useful=useful, note=note_text)
    ok(f"{str(entry.bundle_id)[:8]} marked {'useful' if useful else 'not useful'}")


@app.command("export")
def export_interactions(
    destination: Path = typer.Argument(..., help="JSONL file to write."),
    labelled_only: bool = typer.Option(False, help="Only interactions that have feedback."),
    days: int = typer.Option(0, help="Only the last N days. 0 means all."),
) -> None:
    """Write interactions as JSON lines, for building an evaluation set from real use."""
    since = utcnow() - timedelta(days=days) if days else None
    with service() as memory:
        entries = memory.store.find_bundles(HistoryFilter(since=since, limit=LOOKBACK))

    written = 0
    with destination.open("w", encoding="utf-8") as handle:
        for entry in reversed(entries):
            if labelled_only and entry.feedback is None:
                continue
            handle.write(
                json.dumps(
                    {
                        "id": str(entry.bundle_id),
                        "at": entry.created_at.isoformat(),
                        "source": entry.source,
                        "project": entry.project_key,
                        "prompt": entry.prompt_text,
                        "served": entry.injected,
                        "gate": entry.gate_reason,
                        "episodic_ids": [str(item) for item in entry.episodic_ids],
                        "answer": entry.rendered_text,
                        "feedback": entry.feedback,
                        "note": entry.feedback_note,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            written += 1
    ok(f"wrote {written} interactions to {destination}")


@app.command("purge")
def purge_interactions(
    older_than_days: int = typer.Option(..., "--older-than-days", min=0, help="Delete older ones."),
    yes: bool = typer.Option(False, "--yes", help="Do not ask first."),
) -> None:
    """Delete interactions older than N days. Nothing else is touched."""
    cutoff = utcnow() - timedelta(days=older_than_days)
    if not yes and not typer.confirm(f"Delete every interaction before {cutoff:%Y-%m-%d}?"):
        raise typer.Exit()
    with service() as memory:
        removed = memory.store.purge_bundles(cutoff)
    ok(f"deleted {removed} interactions")
