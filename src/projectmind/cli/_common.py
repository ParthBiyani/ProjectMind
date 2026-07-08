"""Shared plumbing for the command line."""

from __future__ import annotations

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


def table(*columns: str, title: str | None = None) -> Table:
    built = Table(title=title, header_style="bold", box=None, pad_edge=False, title_justify="left")
    for column in columns:
        built.add_column(column)
    return built


def fail(message: str) -> None:
    err_console.print(f"[red]error[/red] {message}")


def note(message: str) -> None:
    console.print(f"[dim]{message}[/dim]")


def ok(message: str) -> None:
    console.print(f"[green]ok[/green] {message}")


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"
