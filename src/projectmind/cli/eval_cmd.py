"""`projectmind eval ...` — run the offline evaluation and compare phases."""

from __future__ import annotations

import tempfile
from pathlib import Path

import typer

from projectmind.cli._common import console, fail, note, ok, table
from projectmind.evaluation import EvalSet, NullSystem
from projectmind.evaluation.live import LiveSystem, live_service
from projectmind.evaluation.runner import OracleSystem, load, run, save
from projectmind.evaluation.scoring import compare as compare_reports

app = typer.Typer(help="Run the offline evaluation.", no_args_is_help=True)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_QUERIES = REPO_ROOT / "eval" / "queries.yaml"

SYSTEMS = ("live", "null", "oracle")


@app.command("run")
def run_eval(
    system: str = typer.Option("live", help=f"One of {', '.join(SYSTEMS)}."),
    queries: Path = typer.Option(DEFAULT_QUERIES, help="Query set to score against."),
    save_to: Path | None = typer.Option(None, "--save", help="Write the report as JSON."),
    verbose: bool = typer.Option(False, "--verbose", help="Show every query that missed."),
) -> None:
    """Score a system against the hand-labelled query set."""
    if system not in SYSTEMS:
        fail(f"unknown system {system!r}; expected one of {', '.join(SYSTEMS)}")
        raise typer.Exit(code=1)

    eval_set = EvalSet.from_yaml(queries)

    if system == "null":
        report = run(eval_set, NullSystem())
    elif system == "oracle":
        report = run(eval_set, OracleSystem())
    else:
        with tempfile.TemporaryDirectory() as tmp, live_service(Path(tmp)) as service:
            report = run(eval_set, LiveSystem(service))

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
        note("not every target is met yet; see docs/roadmap.md for which phase closes which gap")


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
