"""`projectmind browser ...`: what, if anything, is read from browser history.

Off until you turn it on. Even then only pages on domains you have allowed are
considered, and a search is kept only if it led to one of them. `scan --dry-run`
shows what would be stored without storing anything.
"""

from __future__ import annotations

from pathlib import Path

import typer

from projectmind.cli._common import console, fail, note, ok, service, table
from projectmind.config import get_settings
from projectmind.sources.browser import (
    BrowserConfig,
    clean_domain,
    find_databases,
    index_history,
)

app = typer.Typer(help="Control reading browser history (Edge and Chrome).", no_args_is_help=True)


def _load() -> tuple[BrowserConfig, Path]:
    home = get_settings().ensure_home()
    return BrowserConfig.load(home), home


@app.command("status")
def status() -> None:
    """Is it on, which browsers were found, and which sites are never read?"""
    config, _ = _load()
    state = "[green]on[/green]" if config.enabled else "[yellow]off[/yellow]"
    console.print(f"reading browser history: {state}")
    databases = find_databases()
    rendered = table("browser", "profile", "history file")
    for database in databases:
        rendered.add_row(database.browser, database.profile, str(database.path))
    console.print(rendered if databases else "no Edge or Chrome history found")
    note(f"every site except {len(config.deny)} on the never-capture list; last {config.days} days")
    note(f"never: {', '.join(sorted(config.deny))}")


@app.command("enable")
def enable(days: int = typer.Option(365, min=1, help="How far back to read.")) -> None:
    """Turn it on. Sites on the never-capture list, and this machine, are never read."""
    config, home = _load()
    config.enabled = True
    config.days = days
    path = config.save(home)
    ok(f"on; the never-capture list and settings are in {path}")
    note("see what would be stored first with: projectmind browser scan --dry-run")


@app.command("disable")
def disable() -> None:
    """Turn it off. Pages already indexed are removed on the next sync."""
    config, home = _load()
    config.enabled = False
    config.save(home)
    with service() as memory:
        removed = sum(
            int(memory.store.delete_document(uri))
            for uri in list(memory.store.document_hashes("browser"))
        )
    ok(f"off; forgot {removed} pages")


@app.command("allow")
def allow(domain: str = typer.Argument(..., help="e.g. github.com")) -> None:
    """Take a site off the never-capture list, so it is read again."""
    config, home = _load()
    cleaned = clean_domain(domain)
    if cleaned is None or cleaned not in config.deny:
        fail(f"{domain} is not on the never-capture list")
        raise typer.Exit(code=1)
    config.allowing(cleaned).save(home)
    ok(f"{cleaned} will be read again")


@app.command("deny")
def deny(
    domain: str = typer.Argument(..., help="A site to never read, e.g. kite.com."),
) -> None:
    """Never read a site, and forget what was already read from it."""
    from projectmind.capture import forget_domain

    config, home = _load()
    cleaned = clean_domain(domain)
    if cleaned is None:
        fail(f"{domain} is not a domain")
        raise typer.Exit(code=1)
    config.denying(cleaned).save(home)
    with service() as memory:
        forgotten = forget_domain(memory, cleaned)
    ok(f"{cleaned} will never be read" + (f"; forgot {forgotten} pages" if forgotten else ""))


@app.command("scan")
def scan(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Count what would be stored; store nothing."
    ),
) -> None:
    """Read browser history into the knowledge base."""
    config, _ = _load()
    if not config.enabled and not dry_run:
        fail("browser history is off; turn it on with: projectmind browser enable")
        raise typer.Exit(code=1)
    with service() as memory:
        report = index_history(memory.store, config, dry_run=dry_run)
    if report.errors:
        for error in report.errors:
            fail(error)
    if dry_run:
        ok(
            f"would store {report.pages} pages ({report.with_queries} with the search that led "
            f"to them) from {report.databases} browser profile(s)"
        )
        return
    ok(report.summary())
