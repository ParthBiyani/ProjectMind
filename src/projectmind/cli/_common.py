"""Shared plumbing for the command line."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.table import Table

from projectmind.config import Settings, get_settings
from projectmind.service import MemoryService

console = Console()
err_console = Console(stderr=True)


@contextmanager
def service(settings: Settings | None = None) -> Iterator[MemoryService]:
    """Open the store, hand over a service, always close it."""
    resolved = settings or get_settings()
    memory = MemoryService.open(resolved)
    try:
        yield memory
    finally:
        memory.close()


def use_utf8_streams() -> None:
    """Write UTF-8 when output is piped or redirected.

    On Windows a pipe uses the ANSI code page, so "·", "—" and "…" reach the reader as
    garbage. A real console is left alone: Rich writes to it directly.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None or stream.isatty():
            continue
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            reconfigure(encoding="utf-8", errors="replace")


def table(*columns: str, title: str | None = None) -> Table:
    built = Table(title=title, header_style="bold", box=None, pad_edge=False, title_justify="left")
    for column in columns:
        # Fold long values onto the next line; the default cuts them off with an ellipsis.
        built.add_column(column, overflow="fold")
    return built


def fail(message: str) -> None:
    err_console.print(f"[red]error[/red] {message}", soft_wrap=True)


def note(message: str) -> None:
    console.print(f"[dim]{message}[/dim]", soft_wrap=True)


def ok(message: str) -> None:
    console.print(f"[green]ok[/green] {message}", soft_wrap=True)


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"
