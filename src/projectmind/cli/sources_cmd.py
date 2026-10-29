"""`projectmind sources ...`: choose what ProjectMind reads, and read it.

A source is a folder. Everything under it that carries knowledge (notes, docs,
scripts, notebooks, logs, small result files, documents) is read, project by
project. Nothing in a source is ever modified.
"""

from __future__ import annotations

from pathlib import Path

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.config import get_settings
from projectmind.sources.config import KINDS, RootConfig, SourcesConfig
from projectmind.sources.indexer import DEFAULT_MIN_AGE_SECONDS, KnowledgeIndexer, ScanReport
from projectmind.sources.priority import lower_priority

app = typer.Typer(help="Choose and read knowledge sources.", no_args_is_help=True)


def _roots_or_exit() -> tuple[SourcesConfig, Path]:
    home = get_settings().ensure_home()
    config = SourcesConfig.load(home)
    return config, home


@app.command("add")
def add(
    path: Path = typer.Argument(..., help="A folder to read."),
    kind: str = typer.Option("project", help=f"One of: {', '.join(KINDS)}."),
    depth: int = typer.Option(
        1, min=0, help="Levels below the folder where each project lives. 0 = the folder itself."
    ),
) -> None:
    """Add a folder as a knowledge source."""
    resolved = path.expanduser().resolve()
    if not resolved.is_dir():
        fail(f"{resolved} is not a folder")
        raise typer.Exit(code=1)
    if kind not in KINDS:
        fail(f"kind must be one of: {', '.join(KINDS)}")
        raise typer.Exit(code=1)
    config, home = _roots_or_exit()
    created = config.add(RootConfig(path=resolved, kind=kind, depth=depth))
    config.save(home)
    ok(f"{'added' if created else 'updated'} {resolved} ({kind}, depth {depth})")
    note("read it now with: projectmind sources scan")


@app.command("remove")
def remove(path: Path = typer.Argument(..., help="A folder previously added.")) -> None:
    """Stop reading a folder. What was already indexed from it is forgotten on the next scan."""
    config, home = _roots_or_exit()
    resolved = path.expanduser().resolve()
    if not config.remove(resolved):
        fail(f"{resolved} is not a configured source")
        raise typer.Exit(code=1)
    config.save(home)
    with service() as memory:
        removed = 0
        for uri in list(memory.store.document_hashes(resolved.as_posix())):
            removed += int(memory.store.delete_document(uri))
    ok(f"removed {resolved} and forgot {removed} documents")


@app.command("list")
def list_sources() -> None:
    """The configured folders and how much of each is indexed."""
    config, _ = _roots_or_exit()
    if not config.roots:
        note("no sources yet; add one with: projectmind sources add <folder>")
        return
    with service() as memory:
        rendered = table("folder", "kind", "depth", "documents")
        for root in config.roots:
            exists = "" if root.path.is_dir() else " [red](missing)[/red]"
            rendered.add_row(
                f"{root.path}{exists}",
                root.kind,
                str(root.depth),
                str(memory.store.count_documents(root.key)),
            )
        total_chunks = memory.store.count_chunks()
    console.print(rendered)
    note(f"{total_chunks} chunks indexed in total")


def _render(reports: list[ScanReport]) -> None:
    rendered = table("folder", "indexed", "unchanged", "recent", "removed", "chunks", "note")
    for report in reports:
        details = report.error or ", ".join(f"{n} {kind}" for kind, n in report.by_type.items())
        if report.withheld:
            details += f"; {report.withheld} chunk(s) withheld as credentials"
        if report.failed:
            details += f"; {report.failed} unreadable"
        for library, count in report.missing.items():
            details += f"; {count} skipped (needs {library})"
        rendered.add_row(
            report.root,
            str(report.indexed),
            str(report.unchanged),
            str(report.too_recent),
            str(report.removed),
            str(report.chunks),
            details,
        )
    console.print(rendered)


@app.command("scan")
def scan(
    path: Path | None = typer.Argument(None, help="Only this configured folder."),
    min_age: float = typer.Option(
        DEFAULT_MIN_AGE_SECONDS,
        help="Leave files modified within this many seconds; they are probably being edited.",
    ),
) -> None:
    """Read every source folder: new and changed files in, deleted files out.

    Runs at below-normal priority so it stays out of the way of your editor.
    """
    lower_priority()
    config, _ = _roots_or_exit()
    roots = config.roots
    if path is not None:
        target = path.expanduser().resolve().as_posix()
        roots = [root for root in roots if root.key == target]
        if not roots:
            fail(f"{path} is not a configured source")
            raise typer.Exit(code=1)
    if not roots:
        note("no sources yet; add one with: projectmind sources add <folder>")
        return

    with service() as memory:
        indexer = KnowledgeIndexer(memory.store, min_age_seconds=min_age)
        reports = [indexer.scan_root(root) for root in roots]
        total = memory.store.count_chunks()
    _render(reports)
    ok(f"{sum(r.indexed for r in reports)} files read; {total} chunks indexed in total")


@app.command("search")
def search(
    query: str = typer.Argument(..., help="Words to look for."),
    project: str | None = typer.Option(None, help="Only this project key."),
    limit: int = typer.Option(5, help="How many results."),
) -> None:
    """Search the raw index. Useful for seeing what ProjectMind actually knows."""
    terms = query.split()
    with service() as memory:
        hits = memory.store.search_chunks(terms, limit=limit, project_key=project)
    if not hits:
        note("nothing indexed says that")
        return
    for hit in hits:
        where = (
            f"L{hit.chunk.start_line}-{hit.chunk.end_line}"
            if hit.chunk.start_line
            else hit.chunk.locator
        )
        # soft_wrap: a path folded mid-word cannot be copied or opened.
        console.print(
            f"[bold]{hit.document.project_key}[/bold] {hit.document.uri} [dim]{where}[/dim]",
            soft_wrap=True,
        )
        console.print(hit.chunk.text[:400], markup=False)
        console.print()
