"""`projectmind demo`: see what it does, in under a minute, without setting anything up."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel
from rich.text import Text

from projectmind.cli._common import console, fail, note, service
from projectmind.demo import STEPS, StepResult, demo_memory, run_demo

MAX_EXCERPT_LINES = 7


def _render(index: int, total: int, result: StepResult) -> None:
    step = result.step
    body = Text()
    body.append("you ask   ", style="dim")
    body.append(f"{step.prompt}\n", style="bold")
    body.append("in        ", style="dim")
    body.append(f"{step.where}\n", style="")
    body.append("shows     ", style="dim")
    body.append(f"{step.shows}\n\n", style="")

    if result.skipped:
        body.append("-> skipped: a mechanical edit, so nothing was searched\n", style="yellow")
    elif result.silent:
        body.append("-> nothing served", style="yellow")
        body.append("  (no passage covers the question)\n", style="dim")
    else:
        for citation, excerpt in zip(result.citations, result.excerpts, strict=False):
            body.append(f"-> {citation}\n", style="green")
            lines = excerpt.strip().splitlines()
            for line in lines[:MAX_EXCERPT_LINES]:
                body.append(f"   | {line}\n", style="")
            if len(lines) > MAX_EXCERPT_LINES:
                body.append(
                    f"   | ... ({len(lines) - MAX_EXCERPT_LINES} more lines)\n", style="dim"
                )
    body.append(f"\n{result.tokens} tokens, {result.latency_ms:.0f} ms", style="dim")
    console.print(Panel(body, title=f"{index}/{total}  {step.title}", title_align="left"))


def demo(
    real: bool = typer.Option(
        False, "--real", help="Ask your own memory instead (give prompts with --prompt)."
    ),
    prompt: list[str] = typer.Option([], "--prompt", "-p", help="A question to ask with --real."),
    project: Path = typer.Option(
        Path.cwd(), "--project", help="Directory to ask from with --real."
    ),
) -> None:
    """A guided tour on a fictional workspace, or your own questions with --real.

    The tour builds a small fake workspace in a temporary folder, indexes it with
    the real code, and asks seven questions through the real front door, ending
    with two that should return nothing. Your memory is not touched.
    """
    if real:
        if not prompt:
            fail("give at least one question with --prompt")
            raise typer.Exit(code=1)
        with service() as memory:
            for number, text in enumerate(prompt, start=1):
                bundle = memory.get_context(text, project, log_bundle=False)
                console.print(
                    Panel(
                        bundle.render() or "(nothing served: " + bundle.gate_reason + ")",
                        title=f"{number}/{len(prompt)}  {text}",
                        title_align="left",
                    )
                )
                note(f"{bundle.total_tokens} tokens, {bundle.latency_ms:.0f} ms")
        return

    note("building a fictional workspace and indexing it (a few seconds)...")
    with demo_memory() as (memory, paths):
        results = run_demo(memory, paths)
        chunks = memory.store.count_chunks()
        records = memory.store.count_records()
    console.print()
    for index, result in enumerate(results, start=1):
        _render(index, len(results), result)
    served = sum(1 for result in results if not result.silent and not result.skipped)
    console.print(
        f"\n[bold]{served} of {len(STEPS)} questions were answered[/bold] from {chunks} indexed "
        f"passages and {records} history records; the rest correctly returned nothing."
    )
    note("every excerpt above is copied from its source, at the line numbers shown.")
