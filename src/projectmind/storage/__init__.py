"""Storage. One protocol, two backends, chosen by configuration alone."""

from __future__ import annotations

from projectmind.config import Settings, get_settings
from projectmind.storage.base import (
    BundleLogEntry,
    RecordFilter,
    ScoredRecord,
    StatementFilter,
    Store,
    UsageStats,
    aggregate_usage,
)
from projectmind.storage.sqlite_store import SqliteStore

__all__ = [
    "BundleLogEntry",
    "RecordFilter",
    "ScoredRecord",
    "SqliteStore",
    "StatementFilter",
    "Store",
    "UsageStats",
    "aggregate_usage",
    "open_store",
]


def open_store(settings: Settings | None = None, *, migrate: bool = True) -> Store:
    """Open the configured backend.

    A `postgresql://` URL selects Postgres; anything else, including no
    configuration at all, gets SQLite under `PROJECTMIND_HOME`.
    """
    settings = settings or get_settings()
    if settings.uses_postgres:
        raise NotImplementedError(
            "the postgres backend is not wired up yet; unset PROJECTMIND_DB_URL to use sqlite"
        )
    settings.ensure_home()
    store: Store = SqliteStore(settings.sqlite_path)
    if migrate:
        store.migrate()
    return store
