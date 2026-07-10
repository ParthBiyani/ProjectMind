"""The `projectmind` command line.

A thin shell over `MemoryService`, for the same reason the MCP server is one:
if the CLI and the agent take different code paths, the numbers stop
describing the same system.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import typer

from projectmind import __version__
from projectmind.cli import fingerprint_cmd, hook_cmd, install_cmd, profile_cmd
from projectmind.cli._common import console, fail, note, ok, percent, service, table
from projectmind.config import get_settings
from projectmind.models import utcnow
from projectmind.profile.seed import seed_profile
from projectmind.storage import open_store

app = typer.Typer(
    name="projectmind",
    help="Cross-project engineering memory for coding agents.",
    add_completion=False,
)
app.add_typer(profile_cmd.app, name="profile")
app.add_typer(install_cmd.app, name="install")
app.add_typer(hook_cmd.app, name="hook")
app.add_typer(fingerprint_cmd.app, name="fingerprint")


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    if version:
        console.print(f"projectmind {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def init(
    seed: bool = typer.Option(True, help="Load the starter profile as proposals."),
) -> None:
    """Create the home directory, run migrations and load the starter profile."""
    settings = get_settings()
    home = settings.ensure_home()
    store = open_store(settings)
    version = store.migrate()
    ok(f"schema at version {version} on {settings.describe_backend()}")
    ok(f"home is {home}")

    if seed:
        from projectmind.profile.repository import ProfileRepository

        result = seed_profile(ProfileRepository(store, settings))
        ok(result.summary)
    store.close()

    console.print("\n[bold]next[/bold]")
    console.print("  1. projectmind profile review      approve the statements you agree with")
    console.print("  2. projectmind install claude-code wire it into your agent")
    console.print("  3. projectmind context 'some prompt' --project .   see what it would inject")


@app.command()
def context(
    prompt: str = typer.Argument(..., help="The prompt to retrieve context for."),
    project: Path = typer.Option(Path.cwd(), "--project", "-p", help="Project directory."),
    as_json: bool = typer.Option(False, "--json", help="Emit the bundle as JSON."),
    raw: bool = typer.Option(False, "--raw", help="Emit only the injectable markdown."),
    ignore_profile: bool = typer.Option(False, help="Suppress the profile slice."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Do not log this call."),
) -> None:
    """Show exactly what the agent would receive for a prompt."""
    with service() as memory:
        bundle = memory.get_context(
            prompt,
            project,
            ignore_profile=ignore_profile,
            log_bundle=not quiet,
        )

    if as_json:
        console.print_json(bundle.model_dump_json())
        return
    if raw:
        sys.stdout.write(bundle.render())
        return

    if bundle.is_empty:
        note(f"nothing injected — {bundle.gate_reason}")
        return

    console.print(bundle.render())
    console.print(
        f"\n[dim]{bundle.total_tokens} tokens "
        f"({bundle.profile_tokens} profile + {bundle.episodic_tokens} episodic) · "
        f"{bundle.latency_ms:.1f} ms · {bundle.gate_reason}[/dim]"
    )
    if bundle.entities:
        note(f"entities matched: {', '.join(bundle.entities)}")


@app.command()
def report(days: int = typer.Option(30, help="Window in days.")) -> None:
    """What memory actually did, measured rather than assumed."""
    since = utcnow() - timedelta(days=max(1, days))
    with service() as memory:
        stats = memory.stats(since=since)

    if not stats.bundles:
        note(f"no context bundles in the last {days} days")
        return

    rendered = table("metric", "value", title=f"Last {days} days")
    rendered.add_row("context calls", str(stats.bundles))
    rendered.add_row("injected", f"{stats.injected} ({percent(stats.injection_rate)})")
    rendered.add_row("skipped", str(stats.skipped))
    rendered.add_row("with episodic memory", str(stats.with_episodic))
    rendered.add_row("with a cross-project hit", str(stats.cross_project_bundles))
    rendered.add_row(
        "tokens served",
        f"{stats.total_profile_tokens + stats.total_episodic_tokens:,}",
    )
    rendered.add_row("mean latency", f"{stats.mean_latency_ms:.1f} ms")
    rendered.add_row("p95 latency", f"{stats.p95_latency_ms:.1f} ms")
    rated = stats.feedback_positive + stats.feedback_negative
    if rated:
        rendered.add_row("rated helpful", f"{stats.feedback_positive}/{rated}")
        rendered.add_row(
            "observed false injection",
            percent(stats.observed_false_injection_rate),
        )
    else:
        rendered.add_row("rated", "[dim]none yet[/dim]")
    console.print(rendered)

    if stats.by_task_type:
        console.print()
        by_type = table("task type", "calls")
        for name, count in sorted(stats.by_task_type.items(), key=lambda kv: -kv[1]):
            by_type.add_row(name, str(count))
        console.print(by_type)

    if stats.by_project:
        console.print()
        by_project = table("project", "calls")
        for name, count in sorted(stats.by_project.items(), key=lambda kv: -kv[1])[:10]:
            by_project.add_row(name, str(count))
        console.print(by_project)


@app.command()
def doctor() -> None:
    """Check that everything this machine needs is actually in place."""
    settings = get_settings()
    checks: list[tuple[str, bool, str]] = []

    checks.append(("home directory", settings.home.exists(), str(settings.home)))

    try:
        store = open_store(settings)
        version = store.migrate()
        records = store.count_records()
        statements = len(store.list_statements())
        store.close()
        checks.append(("storage", True, f"{settings.describe_backend()} at schema v{version}"))
        checks.append(
            ("memory", statements > 0, f"{statements} active statements, {records} records")
        )
    except Exception as exc:  # doctor reports failures instead of raising them
        checks.append(("storage", False, str(exc)))

    try:
        import mcp  # noqa: F401

        checks.append(("mcp sdk", True, "installed"))
    except ModuleNotFoundError:
        checks.append(("mcp sdk", False, "pip install 'projectmind[mcp]'"))

    from projectmind.storage.embeddings import build_embedder

    embedder = build_embedder(settings)
    checks.append(
        (
            "embeddings",
            True,
            f"{embedder.name}, {embedder.dimensions} dimensions"
            + ("" if embedder.name != "hashing" else " (offline fallback)"),
        )
    )

    checks.append(
        (
            "llm extraction",
            settings.has_llm,
            "enabled" if settings.has_llm else "off — heuristics only, set ANTHROPIC_API_KEY",
        )
    )
    checks.append(("git", shutil.which("git") is not None, shutil.which("git") or "not found"))

    hook = _claude_settings_path()
    checks.append(
        ("claude code", hook.exists(), str(hook) if hook.exists() else "no settings.json found")
    )

    rendered = table("check", "", "detail")
    for name, passed, detail in checks:
        mark = "[green]ok[/green]" if passed else "[yellow]--[/yellow]"
        rendered.add_row(name, mark, detail)
    console.print(rendered)

    blocking = [name for name, passed, _ in checks if not passed and name in {"storage"}]
    if blocking:
        fail(f"blocking problems: {', '.join(blocking)}")
        raise typer.Exit(code=1)


@app.command("mcp")
def mcp_server() -> None:
    """Run the MCP server on stdio. Normally started by the agent, not by hand."""
    from projectmind.mcp.server import main as serve

    serve()


@app.command("export")
def export_memory(
    destination: Path = typer.Argument(..., help="File to write."),
) -> None:
    """Dump the whole profile as JSON, for backup or for editing by hand."""
    with service() as memory:
        payload = [statement.model_dump(mode="json") for statement in memory.profile.all()]
    destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ok(f"wrote {len(payload)} statements to {destination}")


def _claude_settings_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(base or Path.home() / ".claude") / "settings.json"


def main() -> None:
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
