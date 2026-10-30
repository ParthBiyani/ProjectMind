"""`projectmind semantic ...`: match questions to passages by meaning.

Optional, and off until you turn it on. It needs a small embedding model
(`pip install 'projectmind[semantic]'`, about 130 MB, downloaded once) and a few
minutes of background CPU to embed what is already indexed. After that the
daemon keeps the vectors in memory, and the hook asks the daemon.
"""

from __future__ import annotations

from pathlib import Path

import typer

from projectmind import semantic as semantic_module
from projectmind.cli._common import console, fail, note, ok, service
from projectmind.config import get_settings

app = typer.Typer(help="Find passages by meaning, not only by words.", no_args_is_help=True)

FLAG = "PROJECTMIND_SEMANTIC"


def set_flag(home: Path, value: bool) -> None:
    """Write PROJECTMIND_SEMANTIC into the home's .env, keeping every other line."""
    path = home / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    line = f"{FLAG}={'true' if value else 'false'}"
    kept = [existing for existing in lines if not existing.startswith(f"{FLAG}=")]
    path.write_text("\n".join([*kept, line]) + "\n", encoding="utf-8")


@app.command("status")
def status() -> None:
    """Is it on, installed, and how much is embedded?"""
    settings = get_settings()
    with service() as memory:
        index = semantic_module.SemanticIndex(memory.store)
        embedded = memory.store.count_chunk_vectors(index.model_name)
        total = memory.store.count_chunks()
    state = "[green]on[/green]" if settings.semantic else "[yellow]off[/yellow]"
    console.print(f"semantic search: {state}")
    installed = semantic_module.available()
    console.print("dependencies: " + ("installed" if installed else "[red]not installed[/red]"))
    console.print(f"embedded: {embedded}/{total} passages ({index.model_name})")
    if not installed:
        note("pip install 'projectmind[semantic]'")


@app.command("enable")
def enable() -> None:
    """Turn it on. Run `semantic index` to embed what is already indexed."""
    if not semantic_module.available():
        fail("fastembed and numpy are not installed")
        note("pip install 'projectmind[semantic]'")
        raise typer.Exit(code=1)
    set_flag(get_settings().ensure_home(), True)
    ok("semantic search is on; run `projectmind semantic index`, then restart the daemon")


@app.command("disable")
def disable() -> None:
    """Turn it off. Stored vectors are kept."""
    set_flag(get_settings().ensure_home(), False)
    ok("semantic search is off")


@app.command("index")
def index(
    limit: int = typer.Option(0, min=0, help="Embed at most this many (0 = all)."),
) -> None:
    """Embed passages that have no vector yet. Safe to interrupt and rerun."""
    if not semantic_module.available():
        fail("fastembed and numpy are not installed")
        raise typer.Exit(code=1)
    from projectmind.sources.priority import lower_priority

    lower_priority()
    with service() as memory:
        worker = semantic_module.SemanticIndex(memory.store)
        pending = worker.pending()
        if not pending:
            ok("nothing to embed")
            return
        note(f"embedding {limit or pending} of {pending} pending passages")

        def show(done: int, target: int) -> None:
            console.print(f"  {done}/{target}", end="\r")

        done = worker.embed_pending(limit=limit or None, progress=show)
        console.print()
        ok(f"embedded {done} passages")
