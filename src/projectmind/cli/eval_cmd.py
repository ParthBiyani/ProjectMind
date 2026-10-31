"""`projectmind eval ...` — run the offline evaluation and compare phases."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.evaluation import EvalSet, NullSystem
from projectmind.evaluation.live import LiveSystem, live_service
from projectmind.evaluation.paths import eval_dir
from projectmind.evaluation.real import RealSet, run_real
from projectmind.evaluation.runner import OracleSystem, load, run, save
from projectmind.evaluation.scoring import compare as compare_reports

app = typer.Typer(help="Run the offline evaluation.", no_args_is_help=True)


SYSTEMS = ("live", "null", "oracle")


@app.command("run")
def run_eval(
    system: str = typer.Option("live", help=f"One of {', '.join(SYSTEMS)}."),
    queries: Path | None = typer.Option(
        None, help="Query set to score against. Defaults to the repository's eval/queries.yaml."
    ),
    save_to: Path | None = typer.Option(None, "--save", help="Write the report as JSON."),
    verbose: bool = typer.Option(False, "--verbose", help="Show every query that missed."),
) -> None:
    """Score a system against the hand-labelled query set."""
    if system not in SYSTEMS:
        fail(f"unknown system {system!r}; expected one of {', '.join(SYSTEMS)}")
        raise typer.Exit(code=1)

    try:
        queries = queries or eval_dir() / "queries.yaml"
    except FileNotFoundError as exc:
        fail(str(exc))
        raise typer.Exit(code=1) from exc
    eval_set = EvalSet.from_yaml(queries)

    if system == "null":
        report = run(eval_set, NullSystem())
    elif system == "oracle":
        report = run(eval_set, OracleSystem())
    else:
        with tempfile.TemporaryDirectory() as tmp, live_service(Path(tmp)) as service:
            report = run(eval_set, LiveSystem(service, as_of=eval_set.as_of))

    rendered = table(
        "metric", "value", "target", "", title=f"{report.system} · {len(eval_set)} queries"
    )
    for row in report.summary_rows():
        style = "green" if row[3] == "pass" else "red"
        rendered.add_row(row[0], row[1], row[2], f"[{style}]{row[3]}[/{style}]")
    console.print(rendered)

    diagnostics = table("diagnostic", "value")
    diagnostics.add_row("episodic gate precision", f"{report.episodic_gate_precision:.3f}")
    diagnostics.add_row("cross-project recall", f"{report.cross_project_recall:.3f}")
    diagnostics.add_row("ndcg@5 / mrr", f"{report.ndcg_at_k:.3f} / {report.mrr:.3f}")
    diagnostics.add_row("forbidden served", f"{report.forbidden_rate:.3f}")
    diagnostics.add_row("unwanted bundles", f"{report.unwanted_bundle_rate:.3f}")
    diagnostics.add_row("empty when expected", f"{report.empty_when_expected_rate:.3f}")
    diagnostics.add_row(
        "mean / max tokens", f"{report.mean_total_tokens:.0f} / {report.max_total_tokens}"
    )
    diagnostics.add_row("budget violations", str(report.budget_violations))
    diagnostics.add_row(
        "p50 / p95 latency", f"{report.p50_latency_ms:.1f} / {report.p95_latency_ms:.1f} ms"
    )
    console.print()
    console.print(diagnostics)

    if verbose:
        misses = table("query", "expected", "served", "hits")
        for score in report.per_query:
            if score.gate_correct and not score.misses:
                continue
            misses.add_row(
                score.query_id,
                "episodic"
                if score.expected_episodic
                else ("profile" if score.expected_injection else "nothing"),
                str(score.served_episodic),
                str(score.hits),
            )
        console.print()
        console.print(misses)

    if save_to:
        destination = save(report, save_to)
        ok(f"wrote {destination}")

    if not report.passed:
        note("not every target is met yet; the README lists the known gaps")


@app.command("compare")
def compare(
    before: Path = typer.Argument(..., help="Earlier report."),
    after: Path = typer.Argument(..., help="Later report."),
) -> None:
    """Show what changed between two recorded runs."""
    one, two = load(before), load(after)
    rendered = table("metric", one.system, two.system, "delta")
    for name, old, new, delta in compare_reports(one, two):
        arrow = "+" if delta > 0 else ""
        style = "dim" if abs(delta) < 1e-9 else ("green" if delta > 0 else "red")
        rendered.add_row(name, f"{old:.3f}", f"{new:.3f}", f"[{style}]{arrow}{delta:.3f}[/{style}]")
    console.print(rendered)
    note("higher is better for every metric except false_injection_rate and the latencies")


@app.command("real")
def real(
    queries: Path | None = typer.Argument(
        None, help="Your query file. Default: <projectmind home>/eval/queries-real.yaml"
    ),
    save: Path | None = typer.Option(None, help="Write the full report as JSON."),
    min_hit: float = typer.Option(0.0, help="Exit non-zero if the hit rate is below this."),
    max_false_injection: float = typer.Option(
        1.0, help="Exit non-zero if the false injection rate is above this."
    ),
    semantic: bool = typer.Option(
        False, "--semantic/--lexical", help="Also match by meaning (needs `semantic index`)."
    ),
) -> None:
    """Score the front door against questions about your own material.

    Runs against the live store, read-only: nothing is logged and nothing is
    written. Labels cite sources by substring, so they survive re-ingestion.
    """
    from projectmind.config import get_settings

    path = queries or get_settings().home / "eval" / "queries-real.yaml"
    if not path.is_file():
        fail(f"no query file at {path}; see eval/real.example.yaml for the format")
        raise typer.Exit(code=1)
    query_set = RealSet.from_yaml(path)

    with service() as memory:
        if semantic:
            from projectmind import semantic as semantic_module

            if semantic_module.attach(memory) is None:
                fail("no stored vectors; run `projectmind semantic index` first")
                raise typer.Exit(code=1)
        report = run_real(memory, query_set)

    rendered = table("metric", "value", title=f"{report.name} · {report.queries} queries")
    rendered.add_row("hit rate (answerable)", f"{report.hit_rate:.3f}  ({report.answerable})")
    rendered.add_row("served precision", f"{report.served_precision:.3f}")
    rendered.add_row(
        "silence rate (absent topics)", f"{report.silence_rate:.3f}  ({report.silent})"
    )
    rendered.add_row("false injection rate", f"{report.false_injection_rate:.3f}")
    rendered.add_row("forbidden sources served", str(report.forbidden_served))
    rendered.add_row("mean tokens", f"{report.mean_tokens:.0f}")
    rendered.add_row("p95 latency", f"{report.p95_latency_ms:.0f} ms")
    console.print(rendered)

    for outcome in report.failures:
        fail(f"{outcome.id}: {outcome.reason}")
        note(f"  {outcome.prompt}")
        for citation in outcome.served[:3]:
            note(f"    served {citation}")

    if save:
        save.write_text(report.model_dump_json(indent=2), encoding="utf-8")
        ok(f"wrote {save}")

    if report.hit_rate < min_hit or report.false_injection_rate > max_false_injection:
        raise typer.Exit(code=1)
