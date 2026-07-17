"""`projectmind review ...` — the batched monthly review.

One sitting, target under five minutes, highest-impact first. Nothing
interrupts a working session; everything the loop wants a human for collects
here and waits.

Two design choices carry most of the weight:

* **Everything the evidence already settled is absent.** Passive
  re-confirmation runs first, so the list is only statements with no evidence
  either way. A review that shows you all fifty statements is a review you do
  once.
* **Rejections are logged as extractor signal.** Repeatedly rejecting the same
  contradiction means the extractor is misreading a class of artifact, which is
  a bug report about the pipeline rather than a fact about the preference.
"""

from __future__ import annotations

import time
from datetime import datetime
from uuid import UUID

import typer
from rich.panel import Panel

from projectmind.cli._common import console, fail, note, ok, percent, service, table
from projectmind.models import (
    Origin,
    ProfileStatement,
    Resolution,
    ReviewItem,
    ReviewKind,
    StatementStatus,
    utcnow,
)
from projectmind.reflection import ReflectionLoop

app = typer.Typer(help="The batched monthly review.", no_args_is_help=True)


@app.command("run")
def run_loop(
    lookback: int = typer.Option(400, help="How many days of episodic history to consider."),
    sequential: bool = typer.Option(
        False, "--sequential", help="Skip LangGraph and run the nodes directly."
    ),
) -> None:
    """Run the reflection loop and fill the review queue."""
    with service() as memory:
        loop = ReflectionLoop(memory.store, memory.settings, profile=memory.profile)
        started = time.perf_counter()
        state = loop.run(lookback_days=lookback, use_langgraph=not sequential)
        elapsed = time.perf_counter() - started

        rendered = table("stage", "result")
        rendered.add_row("statements examined", str(len(state.statements)))
        rendered.add_row("episodic records considered", str(len(state.records)))
        rendered.add_row(
            "pairs skipped by the entity prefilter",
            f"{state.detection.pairs_possible - state.detection.pairs_after_prefilter} "
            f"of {state.detection.pairs_possible} "
            f"({percent(state.detection.prefilter_saving)})",
        )
        rendered.add_row("contradictions found", str(state.detection.contradictions))
        rendered.add_row("supersession proposals", str(len(state.proposals)))
        rendered.add_row("refreshed without asking", str(state.refreshed))
        rendered.add_row("went dormant", str(state.dormant))
        rendered.add_row("queued for review", str(len(state.queued)))
        rendered.add_row("took", f"{elapsed:.2f}s")
        console.print(rendered)

        if state.errors:
            for error in state.errors:
                fail(error)
        if state.queued:
            note(f"{len(state.queued)} items waiting: projectmind review start")
        else:
            ok("nothing needs a human right now")


@app.command("list")
def list_items(
    all_items: bool = typer.Option(False, "--all", help="Include resolved items."),
) -> None:
    """Show what is waiting, highest impact first."""
    with service() as memory:
        items = memory.store.list_reviews(open_only=not all_items)
        statements = {s.id: s for s in memory.profile.all()}

    if not items:
        ok("the review queue is empty")
        return

    rendered = table("impact", "kind", "about", "proposal")
    for item in items:
        target = statements.get(item.target_id)
        rendered.add_row(
            f"{item.impact:.2f}",
            str(item.kind),
            (target.statement[:40] if target else str(item.target_id)[:8]),
            item.proposal[:46],
        )
    console.print(rendered)
    console.print(f"\n[dim]{len(items)} items[/dim]")


@app.command("start")
def start(
    limit: int = typer.Option(0, help="Stop after this many items. 0 means all."),
) -> None:
    """Work through the queue one item at a time.

    Supersession offers the three outcomes from the PRD, all of which mean
    something: supersede, keep both as context-dependent, or reject the
    evidence as noise.
    """
    started = time.perf_counter()
    resolved = {"supersede": 0, "keep_both": 0, "reject": 0, "confirm": 0, "retire": 0}
    skipped = 0

    with service() as memory:
        items = memory.store.list_reviews(open_only=True)
        if not items:
            ok("the review queue is empty; run `projectmind review run` first")
            raise typer.Exit()
        if limit:
            items = items[:limit]

        console.print(f"[bold]{len(items)} items, highest impact first[/bold]\n")
        for index, item in enumerate(items, start=1):
            target = memory.profile.get(item.target_id)
            if target is None:
                continue
            outcome = (
                _review_supersession(memory, item, target, index, len(items))
                if item.kind is ReviewKind.SUPERSESSION
                else _review_reconfirmation(memory, item, target, index, len(items))
            )
            if outcome is None:
                skipped += 1
                continue
            resolved[str(outcome)] = resolved.get(str(outcome), 0) + 1

    elapsed = time.perf_counter() - started
    console.print()
    summary = table("outcome", "count")
    for name, count in resolved.items():
        if count:
            summary.add_row(name, str(count))
    if skipped:
        summary.add_row("skipped", str(skipped))
    summary.add_row("elapsed", f"{elapsed / 60:.1f} min")
    console.print(summary)

    decided = sum(resolved.values())
    if decided:
        accepted = resolved["supersede"] + resolved["keep_both"] + resolved["confirm"]
        console.print(f"\n[dim]acceptance rate {accepted / decided:.0%}[/dim]")
    if elapsed > 300:
        note("this took over five minutes; the loop is queuing too much")


def _evidence(item: ReviewItem) -> None:
    for ref in item.evidence_refs:
        when = f" ({ref.occurred_at})" if ref.occurred_at else ""
        console.print(f"    [dim]{ref.label[:76]}{when}[/dim]")
        console.print(f"    [dim]{ref.url}[/dim]")


def _review_supersession(
    memory: object, item: ReviewItem, target: ProfileStatement, index: int, total: int
) -> Resolution | None:
    console.print(
        Panel(
            f"[bold]ACTIVE[/bold]\n{target.statement}\n"
            f"[dim]confidence {target.confidence:.2f} · active since "
            f"{target.created_at:%Y-%m} · {len(target.evidence_refs)} supporting refs[/dim]\n\n"
            f"[bold]CONTRADICTED BY[/bold]",
            title=f"{index}/{total} · supersession",
            title_align="left",
        )
    )
    _evidence(item)
    console.print(f"\n  [bold]PROPOSED[/bold]\n  {item.proposal}")
    console.print(f"  [dim]{item.rationale}[/dim]\n")

    choice = (
        typer.prompt(
            "  [s]upersede / keep [b]oth, context-dependent / [r]eject as noise / ski[p] / [q]uit",
            default="p",
        )
        .strip()
        .lower()[:1]
    )

    if choice == "q":
        raise typer.Exit()
    if choice == "s":
        return _do_supersede(memory, item, target)
    if choice == "b":
        return _do_keep_both(memory, item, target)
    if choice == "r":
        return _do_reject(memory, item, target)
    return None


def _do_supersede(memory: object, item: ReviewItem, target: ProfileStatement) -> Resolution:
    text = typer.prompt("  replacement text", default=item.proposal)
    replacement = ProfileStatement(
        statement=text,
        category=target.category,
        confidence=max(target.confidence, 0.85),
        entities=target.entities,
        evidence_refs=item.evidence_refs,
        origin=Origin.REFLECTION,
        status=StatementStatus.PROPOSED,
    )
    memory.profile.supersede(target.id, replacement)  # type: ignore[attr-defined]
    memory.store.resolve_review(item.id, Resolution.SUPERSEDE, note=text)  # type: ignore[attr-defined]
    ok("superseded; the old statement is kept in history and never served")
    return Resolution.SUPERSEDE


def _do_keep_both(memory: object, item: ReviewItem, target: ProfileStatement) -> Resolution:
    console.print(
        "  [dim]both stay active; each needs a scope so the gate can tell them apart[/dim]"
    )
    first = typer.prompt("  when does the existing one apply", default="throwaway prototypes")
    second_text = typer.prompt("  the other statement", default=item.proposal)
    second_scope = typer.prompt("  when does that one apply", default="anything shipping")

    other = ProfileStatement(
        statement=second_text,
        category=target.category,
        confidence=max(target.confidence, 0.85),
        entities=target.entities,
        evidence_refs=item.evidence_refs,
        origin=Origin.REFLECTION,
        status=StatementStatus.ACTIVE,
        scope={"when": second_scope},
    )
    memory.profile.add(other)  # type: ignore[attr-defined]
    memory.profile.keep_both(  # type: ignore[attr-defined]
        target.id, other.id, first_scope={"when": first}, second_scope={"when": second_scope}
    )
    memory.store.resolve_review(item.id, Resolution.KEEP_BOTH, note=second_scope)  # type: ignore[attr-defined]
    ok("kept both, each with a scope")
    return Resolution.KEEP_BOTH


def _do_reject(memory: object, item: ReviewItem, target: ProfileStatement) -> Resolution:
    reason = typer.prompt("  why was this noise", default="contradiction was misread")
    memory.profile.reject_contradiction(target.id)  # type: ignore[attr-defined]
    memory.store.resolve_review(item.id, Resolution.REJECT, note=reason)  # type: ignore[attr-defined]
    note("logged as extractor signal: repeated rejections here mean a systematic misread")
    return Resolution.REJECT


def _review_reconfirmation(
    memory: object, item: ReviewItem, target: ProfileStatement, index: int, total: int
) -> Resolution | None:
    console.print(
        Panel(
            f"{target.statement}\n\n"
            f"[dim]{target.category} · last confirmed {target.last_confirmed_at:%Y-%m-%d} · "
            f"confidence has decayed to {target.effective_confidence():.2f}[/dim]\n"
            f"[dim]{item.rationale}[/dim]",
            title=f"{index}/{total} · still true?",
            title_align="left",
        )
    )
    choice = typer.prompt("  [y]es / [n]o, retire it / ski[p] / [q]uit", default="p")
    choice = choice.strip().lower()[:1]

    if choice == "q":
        raise typer.Exit()
    if choice == "y":
        memory.profile.confirm(target.id)  # type: ignore[attr-defined]
        memory.store.resolve_review(item.id, Resolution.CONFIRM)  # type: ignore[attr-defined]
        return Resolution.CONFIRM
    if choice == "n":
        memory.profile.retire(target.id)  # type: ignore[attr-defined]
        memory.store.resolve_review(item.id, Resolution.RETIRE)  # type: ignore[attr-defined]
        note("retired; it stops being served but stays in history")
        return Resolution.RETIRE
    return None


@app.command("history")
def history(limit: int = typer.Option(20, help="How many resolved items to show.")) -> None:
    """What was decided, and how often proposals were accepted.

    The acceptance rate is a measurement of the reflection loop, not of the
    profile. Below about 60% the loop is proposing things a human disagrees
    with, which means the extractor or the rules need work rather than the
    statements.
    """
    with service() as memory:
        items = [item for item in memory.store.list_reviews(open_only=False) if not item.is_open]

    if not items:
        note("nothing has been reviewed yet")
        return

    items.sort(key=lambda item: item.resolved_at or utcnow(), reverse=True)
    rendered = table("resolved", "kind", "outcome", "proposal")
    for item in items[:limit]:
        when: datetime | None = item.resolved_at
        rendered.add_row(
            f"{when:%Y-%m-%d}" if when else "-",
            str(item.kind),
            str(item.resolution),
            item.proposal[:48],
        )
    console.print(rendered)

    accepted = sum(
        1
        for item in items
        if item.resolution in {Resolution.SUPERSEDE, Resolution.KEEP_BOTH, Resolution.CONFIRM}
    )
    rejected = sum(1 for item in items if item.resolution is Resolution.REJECT)
    console.print(
        f"\nacceptance rate [bold]{accepted / len(items):.0%}[/bold] over {len(items)} decisions"
    )
    if rejected:
        note(f"{rejected} rejections — each one is a signal that the extractor misread something")


@app.command("resolve")
def resolve(
    review_id: str = typer.Argument(..., help="Review item id."),
    outcome: Resolution = typer.Argument(..., help="How it was resolved."),
    note_text: str = typer.Option("", "--note", help="Why."),
) -> None:
    """Resolve one item without the interactive flow, for scripting."""
    with service() as memory:
        try:
            resolved = memory.store.resolve_review(UUID(review_id), outcome, note=note_text)
        except ValueError:
            fail("not a valid id")
            raise typer.Exit(code=1) from None
    if resolved is None:
        fail("no review item with that id")
        raise typer.Exit(code=1)
    ok(f"resolved as {outcome}")
