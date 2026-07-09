"""`projectmind profile ...` — inspect and curate the always-injected layer."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import typer
from rich.panel import Panel

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.models import Category, StatementStatus
from projectmind.profile.lifecycle import TransitionError
from projectmind.profile.seed import SEED_PATH, seed_profile

app = typer.Typer(help="Inspect and curate profile memory.", no_args_is_help=True)

STATUS_STYLE = {
    StatementStatus.ACTIVE: "green",
    StatementStatus.PROPOSED: "yellow",
    StatementStatus.SUPERSEDED: "dim",
    StatementStatus.DORMANT: "dim",
    StatementStatus.REJECTED: "red",
}


@app.command("list")
def list_statements(
    status: StatementStatus | None = typer.Option(None, help="Filter by status."),
    category: Category | None = typer.Option(None, help="Filter by category."),
    full: bool = typer.Option(False, "--full", help="Show ids and evidence counts."),
) -> None:
    """List profile statements."""
    with service() as memory:
        statements = memory.profile.all()
        if status:
            statements = [s for s in statements if s.status is status]
        if category:
            statements = [s for s in statements if s.category is category]
        if not statements:
            note("no statements match")
            raise typer.Exit()

        rendered = table("status", "category", "conf", "statement", *(["id"] if full else []))
        for statement in sorted(statements, key=lambda s: (s.category, -s.confidence)):
            style = STATUS_STYLE[statement.status]
            row = [
                f"[{style}]{statement.status}[/{style}]",
                str(statement.category),
                f"{statement.confidence:.2f}",
                statement.statement,
            ]
            if full:
                row.append(str(statement.id))
            rendered.add_row(*row)
        console.print(rendered)
        console.print(f"\n[dim]{len(statements)} statements[/dim]")


@app.command("stats")
def stats() -> None:
    """Summarise the profile."""
    with service() as memory:
        summary = memory.profile.stats()
        if not summary.total:
            note("the profile is empty; run `projectmind seed`")
            raise typer.Exit()
        rendered = table("metric", "value")
        rendered.add_row("statements", str(summary.total))
        for name, count in sorted(summary.by_status.items()):
            rendered.add_row(f"  {name}", str(count))
        for name, count in sorted(summary.by_category.items()):
            rendered.add_row(f"  {name}", str(count))
        rendered.add_row("mean confidence", f"{summary.mean_confidence:.2f}")
        if summary.oldest_unconfirmed:
            rendered.add_row("oldest confirmation", f"{summary.oldest_unconfirmed:%Y-%m-%d}")
        console.print(rendered)


@app.command("review")
def review(
    limit: int = typer.Option(0, help="Stop after this many statements. 0 means all."),
) -> None:
    """Approve or drop proposed statements, one at a time.

    Seeded statements arrive as proposals and are never served until they pass
    through here. This is the Phase 1 review; the batched monthly review that
    handles supersessions is a separate command.
    """
    with service() as memory:
        pending = memory.profile.by_status(StatementStatus.PROPOSED)
        if not pending:
            ok("nothing to review")
            raise typer.Exit()

        if limit:
            pending = pending[:limit]
        console.print(f"[bold]{len(pending)} statements awaiting approval[/bold]\n")
        approved = dropped = skipped = 0

        for index, statement in enumerate(pending, start=1):
            console.print(
                Panel(
                    f"{statement.statement}\n\n"
                    f"[dim]{statement.category} · confidence {statement.confidence:.2f} · "
                    f"ttl {statement.ttl_days}d"
                    + (
                        f" · entities: {', '.join(statement.entities)}"
                        if statement.entities
                        else ""
                    )
                    + "[/dim]",
                    title=f"{index}/{len(pending)}",
                    title_align="left",
                )
            )
            choice = typer.prompt("  [a]pprove / [d]rop / [s]kip / [q]uit", default="s").lower()[:1]
            if choice == "a":
                try:
                    memory.profile.activate(statement.id)
                    approved += 1
                except TransitionError as exc:
                    fail(str(exc))
            elif choice == "d":
                memory.store.upsert_statement(
                    statement.model_copy(update={"status": StatementStatus.REJECTED})
                )
                dropped += 1
            elif choice == "q":
                break
            else:
                skipped += 1

        console.print(
            f"\n[green]{approved} approved[/green], {dropped} dropped, {skipped} left as proposals"
        )


@app.command("show")
def show(statement_id: str = typer.Argument(..., help="Statement id.")) -> None:
    """Show one statement in full, evidence included."""
    with service() as memory:
        try:
            statement = memory.profile.get(UUID(statement_id))
        except ValueError:
            fail("not a valid id")
            raise typer.Exit(code=1) from None
        if statement is None:
            fail("no statement with that id")
            raise typer.Exit(code=1)

        console.print(Panel(statement.statement, title=str(statement.status), title_align="left"))
        rendered = table("field", "value")
        rendered.add_row("category", str(statement.category))
        rendered.add_row("confidence", f"{statement.confidence:.2f}")
        rendered.add_row("effective now", f"{statement.effective_confidence():.2f}")
        rendered.add_row("ttl", f"{statement.ttl_days} days")
        rendered.add_row("last confirmed", f"{statement.last_confirmed_at:%Y-%m-%d}")
        rendered.add_row("origin", str(statement.origin))
        if statement.scope:
            rendered.add_row("scope", statement.scope_label())
        if statement.entities:
            rendered.add_row("entities", ", ".join(statement.entities))
        if statement.contradiction_count:
            rendered.add_row("contradictions", str(statement.contradiction_count))
        if statement.supersedes:
            rendered.add_row("supersedes", str(statement.supersedes))
        if statement.superseded_by:
            rendered.add_row("superseded by", str(statement.superseded_by))
        console.print(rendered)
        for ref in statement.evidence_refs:
            console.print(f"  [dim]evidence:[/dim] {ref.kind} {ref.url}")


@app.command("seed")
def seed(
    path: Path = typer.Option(SEED_PATH, "--file", help="Seed file to load."),
    activate_all: bool = typer.Option(
        False,
        "--activate-all",
        help="Activate immediately. Only do this if you have read the file.",
    ),
) -> None:
    """Load starter statements as proposals."""
    with service() as memory:
        result = seed_profile(memory.profile, path=path, activate=activate_all)
        ok(result.summary)
        if not activate_all and result.loaded:
            note("nothing is served until you approve it: projectmind profile review")


@app.command("sweep")
def sweep() -> None:
    """Apply the TTL decay schedule and report what moved."""
    with service() as memory:
        result = memory.profile.sweep_decay()
        ok(
            f"examined {result.examined}, {result.decayed} past their ttl, "
            f"{result.went_dormant} went dormant"
        )
        if result.due_for_review:
            note(f"{len(result.due_for_review)} statements are due for re-confirmation")
