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


@app.command("audit")
def audit_index(
    fix: bool = typer.Option(False, "--fix", help="Remove what the current rules would refuse."),
) -> None:
    """Check what is already indexed against today's privacy rules.

    Identity and financial documents, credentials and personal identifiers are
    refused as files are read. This applies the same rules to what was indexed
    before a rule existed. Without --fix it only reports.
    """
    from projectmind.sources.audit import audit

    with service() as memory:
        report = audit(memory.store, fix=fix)
    note(f"checked {report.documents_checked} documents and {report.chunks_checked} passages")
    for uri in report.sensitive_documents[:20]:
        fail(f"identity or financial document: {uri}")
    if report.flagged_chunks:
        fail(f"{report.flagged_chunks} passage(s) contain a credential or personal identifier")
    if report.clean:
        ok("nothing the rules would refuse")
        return
    if fix:
        ok(f"removed {report.removed_documents} documents and {report.removed_chunks} passages")
        return
    note("run again with --fix to remove them")
    raise typer.Exit(code=1)


@app.command("search")
def search(
    query: str = typer.Argument(..., help="What to look for, in your own words."),
    project: str | None = typer.Option(None, help="Only this project key."),
    limit: int = typer.Option(5, help="How many results."),
) -> None:
    """Search the index by words and, if semantic search is on, by meaning."""
    from projectmind import semantic

    with service() as memory:
        if memory.settings.semantic:
            semantic.attach(memory)
        found = memory.search_sources(query, project_key=project, limit=limit)
    if not found:
        note("nothing indexed says that")
        return
    for item in found:
        how = "words" if item.via == "words" else f"meaning {item.similarity:.2f}"
        # soft_wrap: a path folded mid-word cannot be copied or opened.
        console.print(
            f"[bold]{item.evidence.project_key}[/bold] {item.evidence.citation} [dim]({how})[/dim]",
            soft_wrap=True,
        )
        console.print(item.evidence.excerpt[:400], markup=False)
        console.print()
