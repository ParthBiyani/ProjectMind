"""`projectmind ingest ...` — turn repositories into episodic memory."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from uuid import UUID

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.config import get_settings
from projectmind.ingestion.pipeline import IngestionPipeline, IngestionReport
from projectmind.models import RecordType, utcnow
from projectmind.storage import RecordFilter

app = typer.Typer(help="Ingest repositories into episodic memory.", no_args_is_help=True)


def _render(report: IngestionReport) -> None:
    rendered = table("project", "commits", "extracted", "new", "note")
    for item in sorted(report.repositories, key=lambda r: -r.records_stored):
        rendered.add_row(
            item.key,
            str(item.commits_read),
            str(item.records_extracted),
            str(item.records_stored),
            item.skipped_reason or f"{item.yield_rate:.0%} yield",
        )
    console.print(rendered)
    ok(f"{report.summary()} in {report.duration_seconds:.1f}s")


@app.command("local")
def ingest_local(
    root: Path = typer.Argument(..., help="A repository, or a directory containing repositories."),
    mine_only: bool = typer.Option(
        True,
        help="Only read commits authored by you. Uses git's configured user.email.",
    ),
    author: list[str] = typer.Option([], help="Extra author emails to include."),
    days: int = typer.Option(0, help="Only read commits from the last N days. 0 means all."),
    depth: int = typer.Option(2, help="How deep to look for repositories."),
    limit: int = typer.Option(0, help="Stop after N repositories. 0 means all."),
) -> None:
    """Read git history from disk. No token, no network, no rate limit."""
    root = root.expanduser()
    if not root.exists():
        fail(f"{root} does not exist")
        raise typer.Exit(code=1)

    authors = list(author)
    if mine_only:
        import subprocess

        try:
            configured = subprocess.run(
                ["git", "config", "user.email"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            configured = ""
        if configured:
            authors.append(configured)
        elif not authors:
            note("git has no configured user.email; reading commits from every author")

    since = utcnow() - timedelta(days=days) if days else None

    with service() as memory:
        pipeline = IngestionPipeline(memory.store, memory.settings)
        if (root / ".git").exists():
            report = IngestionReport(
                repositories=[pipeline.ingest_repository(root, authors=authors, since=since)]
            )
            report.finished_at = utcnow()
        else:
            report = pipeline.ingest_tree(
                root, authors=authors, since=since, max_depth=depth, max_repositories=limit
            )
        _render(report)
        note(f"{memory.store.count_records()} episodic records in total")


@app.command("github")
def ingest_github(
    remote: list[str] = typer.Option([], help="Remote URLs. Defaults to every indexed project."),
    limit: int = typer.Option(50, help="Pull requests per repository."),
) -> None:
    """Read merged pull requests. Needs GITHUB_TOKEN; local ingestion does not."""
    settings = get_settings()
    if not settings.github_token:
        fail("no GITHUB_TOKEN is set")
        note("local ingestion needs no token: projectmind ingest local <dir>")
        raise typer.Exit(code=1)

    with service() as memory:
        remotes = list(remote) or [
            project.git_remote
            for project in memory.store.list_projects()
            if project.git_remote and project.git_remote.startswith("http")
        ]
        if not remotes:
            note("no remotes to read; index some projects first")
            raise typer.Exit()
        pipeline = IngestionPipeline(memory.store, memory.settings)
        _render(pipeline.ingest_github(remotes, limit_per_repo=limit))


@app.command("status")
def status() -> None:
    """What episodic memory currently holds."""
    with service() as memory:
        total = memory.store.count_records()
        if not total:
            note("no episodic records yet; run: projectmind ingest local <dir>")
            raise typer.Exit()

        records = memory.store.list_records()
        by_type: dict[str, int] = {}
        by_project: dict[str, int] = {}
        projects = {project.id: project.key for project in memory.store.list_projects()}
        inferred = 0
        for record in records:
            by_type[record.type] = by_type.get(record.type, 0) + 1
            key = projects.get(record.project_id, "unknown")
            by_project[key] = by_project.get(key, 0) + 1
            inferred += record.is_inference

        summary = table("metric", "value")
        summary.add_row("records", str(total))
        summary.add_row("projects covered", str(len(by_project)))
        summary.add_row(
            "author stated the reason", f"{total - inferred} ({(total - inferred) / total:.0%})"
        )
        summary.add_row("reason inferred", f"{inferred} ({inferred / total:.0%})")
        oldest = min(record.occurred_at for record in records)
        newest = max(record.occurred_at for record in records)
        summary.add_row("history covered", f"{oldest:%Y-%m} to {newest:%Y-%m}")
        console.print(summary)

        console.print()
        types = table("type", "records")
        for name, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
            types.add_row(name, str(count))
        console.print(types)

        console.print()
        top = table("project", "records")
        for name, count in sorted(by_project.items(), key=lambda kv: -kv[1])[:12]:
            top.add_row(name, str(count))
        console.print(top)


@app.command("show")
def show(
    project: str = typer.Option("", help="Only records from this project key."),
    record_type: RecordType | None = typer.Option(None, "--type", help="Only this record type."),
    limit: int = typer.Option(10, help="How many to show."),
) -> None:
    """Read records back, so the extractor's output can actually be inspected."""
    with service() as memory:
        project_ids: tuple[UUID, ...] = ()
        if project:
            found = memory.store.get_project(project)
            if found is None:
                fail(f"no project with key {project!r}")
                raise typer.Exit(code=1)
            project_ids = (found.id,)

        records = memory.store.list_records(
            RecordFilter(
                project_ids=project_ids,
                types=(record_type,) if record_type else (),
                limit=limit,
            )
        )
        keys = {p.id: p.key for p in memory.store.list_projects()}

    if not records:
        note("nothing matched")
        return

    for record in records:
        marker = "inferred" if record.is_inference else "stated"
        console.print(
            f"[bold]{record.type}[/bold] · {keys.get(record.project_id, '?')} · "
            f"{record.occurred_at:%Y-%m-%d} · confidence {record.confidence:.2f} "
            f"[dim]({marker})[/dim]"
        )
        console.print(f"  {record.what}")
        if record.why:
            console.print(f"  [dim]why: {record.why}[/dim]")
        if record.rejected_alternatives:
            console.print(f"  [dim]rejected: {', '.join(record.rejected_alternatives)}[/dim]")
        if record.entities:
            console.print(f"  [dim]entities: {', '.join(record.entities)}[/dim]")
        console.print(f"  [dim]{record.source_url}[/dim]\n")
