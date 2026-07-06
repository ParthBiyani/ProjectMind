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
from projectmind.storage.embeddings import (
    EmbeddingProvider,
    HashingEmbedder,
    build_embedder,
    embed_one,
    get_embedder,
)
from projectmind.storage.sqlite_store import SqliteStore

__all__ = [
    "BundleLogEntry",
    "EmbeddingProvider",
    "HashingEmbedder",
    "RecordFilter",
    "ScoredRecord",
    "SqliteStore",
    "StatementFilter",
    "Store",
    "UsageStats",
    "aggregate_usage",
    "build_embedder",
    "embed_one",
    "get_embedder",
    "open_store",
]


def open_store(settings: Settings | None = None, *, migrate: bool = True) -> Store:
    """Open the configured backend.

    A `postgresql://` URL selects Postgres; anything else, including no
    configuration at all, gets SQLite under `PROJECTMIND_HOME`. The Postgres
    driver is imported lazily, so the common path costs nothing and a missing
    `psycopg` is only an error for people who asked for Postgres.
    """
    settings = settings or get_settings()
    store: Store
    if settings.uses_postgres:
        from projectmind.storage.postgres_store import PostgresStore

        assert settings.db_url is not None
        store = PostgresStore(settings.db_url)
    else:
        settings.ensure_home()
        store = SqliteStore(settings.sqlite_path)
    if migrate:
        store.migrate()
    return store
