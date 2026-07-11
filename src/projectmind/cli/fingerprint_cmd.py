"""`projectmind fingerprint ...` — index projects and inspect their neighbours."""

from __future__ import annotations

from pathlib import Path

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.fingerprint.cache import FingerprintCache
from projectmind.fingerprint.matching import compare, rank_siblings, similarity_index
from projectmind.fingerprint.scanner import ProjectScanner
from projectmind.models import Project, utcnow
from projectmind.service import project_key

app = typer.Typer(help="Index projects and inspect their neighbours.", no_args_is_help=True)


@app.command("scan")
def scan(
    root: Path = typer.Argument(..., help="A project directory, or a directory of projects."),
    children: bool = typer.Option(
        False,
        "--children",
        help="Treat each immediate subdirectory as its own project.",
    ),
    refresh: bool = typer.Option(False, "--refresh", help="Ignore any cached fingerprint."),
    limit: int = typer.Option(0, help="Stop after this many projects. 0 means all."),
) -> None:
    """Fingerprint a project, or every project under a directory.

    `--children` is the one to use on a folder full of repositories: it walks
    each subdirectory separately rather than treating the whole tree as one
    enormous project.
    """
    root = root.expanduser()
    if not root.is_dir():
        fail(f"{root} is not a directory")
        raise typer.Exit(code=1)

    targets = sorted(p for p in root.iterdir() if p.is_dir()) if children else [root]
    if limit:
        targets = targets[:limit]
    if not targets:
        note("nothing to scan")
        raise typer.Exit()

    scanner = ProjectScanner()
    rendered = table("project", "languages", "frameworks", "deps", "domains")
    indexed = skipped = 0

    with service() as memory:
        cache = FingerprintCache(memory.store, scanner)
        for target in targets:
            key = project_key(target, None)
            if refresh:
                cache.invalidate(key)
            fingerprint = cache.get(key, target).fingerprint
            if fingerprint.is_empty:
                skipped += 1
                continue

            memory.store.upsert_project(
                Project(
                    key=key,
                    name=target.name,
                    root_path=str(target),
                    git_remote=fingerprint.git_remote,
                    fingerprint=fingerprint,
                    last_active=utcnow(),
                )
            )
            indexed += 1
            rendered.add_row(
                key,
                ", ".join(fingerprint.languages) or "-",
                ", ".join(fingerprint.frameworks[:3]) or "-",
                str(len(fingerprint.dependencies)),
                ", ".join(fingerprint.domain_hints) or "-",
            )

    console.print(rendered)
    ok(f"{indexed} projects indexed")
    if skipped:
        note(f"{skipped} skipped, nothing identifiable in them")


@app.command("show")
def show(
    project: Path = typer.Argument(Path.cwd(), help="Project directory."),
) -> None:
    """Show the fingerprint for one project, and where it came from."""
    key = project_key(project.expanduser(), None)
    with service() as memory:
        lookup = FingerprintCache(memory.store).get(key, project)

    fingerprint = lookup.fingerprint
    rendered = table("field", "value")
    rendered.add_row("key", key)
    rendered.add_row("cache", "hit" if lookup.hit else f"miss ({lookup.reason})")
    rendered.add_row("languages", ", ".join(fingerprint.languages) or "-")
    rendered.add_row("frameworks", ", ".join(fingerprint.frameworks) or "-")
    rendered.add_row("dependencies", str(len(fingerprint.dependencies)))
    rendered.add_row("domain hints", ", ".join(fingerprint.domain_hints) or "-")
    rendered.add_row("manifests", ", ".join(fingerprint.manifest_files) or "none found")
    rendered.add_row("files seen", str(fingerprint.file_count))
    rendered.add_row("git remote", fingerprint.git_remote or "-")
    rendered.add_row("computed", f"{fingerprint.computed_at:%Y-%m-%d %H:%M}")
    console.print(rendered)

    if fingerprint.dependencies:
        console.print(f"\n[dim]{', '.join(fingerprint.dependencies)}[/dim]")


@app.command("neighbours")
def neighbours(
    project: Path = typer.Argument(Path.cwd(), help="Project directory."),
    limit: int = typer.Option(8, help="How many neighbours to show."),
    threshold: float = typer.Option(0.10, help="Minimum similarity to report."),
) -> None:
    """Which indexed projects look most like this one, and why."""
    key = project_key(project.expanduser(), None)
    with service() as memory:
        fingerprint = FingerprintCache(memory.store).get(key, project).fingerprint
        others = memory.store.list_projects()

    matches = rank_siblings(
        fingerprint, others, exclude_keys=(key,), min_similarity=threshold, limit=limit
    )
    if not matches:
        note("no indexed project is similar enough; try `projectmind fingerprint scan --children`")
        return

    rendered = table("similarity", "project", "shared")
    for match in matches:
        rendered.add_row(f"{match.similarity:.3f}", match.key, match.explain().split(" — ", 1)[-1])
    console.print(rendered)


@app.command("compare")
def compare_two(
    left: Path = typer.Argument(..., help="First project directory."),
    right: Path = typer.Argument(..., help="Second project directory."),
) -> None:
    """Break a similarity score down by component."""
    scanner = ProjectScanner()
    one = scanner.scan(left.expanduser()).fingerprint
    two = scanner.scan(right.expanduser()).fingerprint
    breakdown = compare(one, two)

    rendered = table("component", "overlap", "weight")
    for row in breakdown.rows():
        rendered.add_row(*row)
    console.print(rendered)
    console.print(f"\n[bold]similarity {breakdown.total:.3f}[/bold]")

    shared = sorted(set(one.dependencies) & set(two.dependencies))
    if shared:
        console.print(f"[dim]shared dependencies: {', '.join(shared)}[/dim]")


@app.command("map")
def neighbour_map(
    threshold: float = typer.Option(0.20, help="Minimum similarity to draw an edge."),
) -> None:
    """Every indexed project and its closest neighbour."""
    with service() as memory:
        projects = memory.store.list_projects()

    if not projects:
        note("nothing indexed yet; run `projectmind fingerprint scan <dir> --children`")
        return

    index = similarity_index(projects, min_similarity=threshold)
    rendered = table("project", "closest neighbour", "similarity", "shared")
    for key in sorted(index):
        matches = index[key]
        if not matches:
            rendered.add_row(key, "[dim]none[/dim]", "-", "-")
            continue
        best = matches[0]
        rendered.add_row(
            key,
            best.key,
            f"{best.similarity:.3f}",
            ", ".join(best.shared_dependencies[:4]) or ", ".join(best.shared_frameworks[:3]) or "-",
        )
    console.print(rendered)
