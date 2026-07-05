"""SQLite backend.

The default, because a memory server that needs a daemon running is a memory
server that is unavailable exactly when you reach for it. Everything here is
stdlib `sqlite3`; vectors are stored as little-endian float32 blobs and scored
in Python, which is comfortably fast at the scale this system operates on
(thousands of records, not millions). When that stops being true, the Postgres
backend is a URL away.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from projectmind.logging import get_logger
from projectmind.models import (
    EpisodicRecord,
    Fingerprint,
    ProfileStatement,
    Project,
    Resolution,
    ReviewItem,
    utcnow,
)
from projectmind.storage.base import (
    BundleLogEntry,
    RecordFilter,
    ScoredRecord,
    StatementFilter,
    Store,
    UsageStats,
    aggregate_usage,
    dumps,
    from_iso,
    loads,
    project_from_row,
    project_to_row,
    record_from_row,
    record_to_row,
    review_from_row,
    review_to_row,
    statement_from_row,
    statement_to_row,
    to_iso,
)
from projectmind.storage.vectors import cosine_similarity, decode_vector, encode_vector

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "sqlite"


class SqliteStore(Store):
    """Store backed by a single SQLite file, or `:memory:` in tests."""

    dialect = "sqlite"

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")

    # --- lifecycle ---------------------------------------------------------
    def migrate(self) -> int:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        applied = {
            int(row["version"])
            for row in self._conn.execute("SELECT version FROM schema_migrations")
        }
        for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(script.name.split("_", 1)[0])
            if version in applied:
                continue
            self._conn.executescript(script.read_text(encoding="utf-8"))
            self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, to_iso(utcnow())),
            )
            log.info("applied migration", extra={"version": version, "dialect": self.dialect})
        self._conn.commit()
        row = self._conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        return int(row["v"] or 0)

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()

    # --- projects ----------------------------------------------------------
    def upsert_project(self, project: Project) -> Project:
        row = project_to_row(project)
        existing = self.get_project(project.key)
        if existing:
            row["id"] = str(existing.id)
            row["first_seen"] = to_iso(existing.first_seen)
        self._conn.execute(
            """
            INSERT INTO projects (id, key, name, root_path, git_remote, fingerprint,
                                  first_seen, last_active)
            VALUES (:id, :key, :name, :root_path, :git_remote, :fingerprint,
                    :first_seen, :last_active)
            ON CONFLICT (key) DO UPDATE SET
                name = excluded.name,
                root_path = excluded.root_path,
                git_remote = excluded.git_remote,
                fingerprint = excluded.fingerprint,
                last_active = excluded.last_active
            """,
            row,
        )
        self._conn.commit()
        stored = self.get_project(project.key)
        assert stored is not None
        return stored

    def get_project(self, key: str) -> Project | None:
        row = self._conn.execute("SELECT * FROM projects WHERE key = ?", (key,)).fetchone()
        return project_from_row(row) if row else None

    def get_project_by_id(self, project_id: UUID) -> Project | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (str(project_id),)
        ).fetchone()
        return project_from_row(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY last_active DESC").fetchall()
        return [project_from_row(row) for row in rows]

    # --- profile -----------------------------------------------------------
    def upsert_statement(self, statement: ProfileStatement) -> ProfileStatement:
        row = statement_to_row(statement)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        updates = ", ".join(f"{name} = excluded.{name}" for name in row if name != "id")
        self._conn.execute(
            f"INSERT INTO profile_statements ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()
        return statement

    def get_statement(self, statement_id: UUID) -> ProfileStatement | None:
        row = self._conn.execute(
            "SELECT * FROM profile_statements WHERE id = ?", (str(statement_id),)
        ).fetchone()
        return statement_from_row(row) if row else None

    def list_statements(self, filters: StatementFilter | None = None) -> list[ProfileStatement]:
        filters = filters or StatementFilter()
        clauses: list[str] = []
        params: list[Any] = []
        if filters.statuses:
            clauses.append(f"status IN ({','.join('?' * len(filters.statuses))})")
            params.extend(str(status) for status in filters.statuses)
        if filters.categories:
            clauses.append(f"category IN ({','.join('?' * len(filters.categories))})")
            params.extend(str(category) for category in filters.categories)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = f"LIMIT {int(filters.limit)}" if filters.limit else ""
        rows = self._conn.execute(
            f"SELECT * FROM profile_statements {where} ORDER BY confidence DESC {limit}",
            params,
        ).fetchall()
        statements = [statement_from_row(row) for row in rows]
        if filters.entities:
            wanted = {entity.lower() for entity in filters.entities}
            statements = [s for s in statements if wanted & set(s.entities)]
        return statements

    def delete_statement(self, statement_id: UUID) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM profile_statements WHERE id = ?", (str(statement_id),)
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- episodic ----------------------------------------------------------
    def add_records(self, records: Iterable[EpisodicRecord]) -> int:
        inserted = 0
        for record in records:
            row = record_to_row(record)
            columns = ", ".join(row)
            placeholders = ", ".join(f":{name}" for name in row)
            cursor = self._conn.execute(
                f"INSERT INTO episodic_records ({columns}) VALUES ({placeholders}) "
                f"ON CONFLICT (content_hash) DO NOTHING",
                row,
            )
            if cursor.rowcount == 0:
                continue
            inserted += 1
            self._conn.executemany(
                "INSERT OR IGNORE INTO record_entities (record_id, entity) VALUES (?, ?)",
                [(str(record.id), entity) for entity in record.entities],
            )
            if record.embedding:
                self._conn.execute(
                    "INSERT OR REPLACE INTO record_embeddings (record_id, dimensions, vector) "
                    "VALUES (?, ?, ?)",
                    (str(record.id), len(record.embedding), encode_vector(record.embedding)),
                )
        self._conn.commit()
        return inserted

    def get_record(self, record_id: UUID) -> EpisodicRecord | None:
        row = self._conn.execute(
            "SELECT * FROM episodic_records WHERE id = ?", (str(record_id),)
        ).fetchone()
        if not row:
            return None
        return record_from_row(row, self._embedding_for(record_id))

    def list_records(self, filters: RecordFilter | None = None) -> list[EpisodicRecord]:
        where, params = self._record_where(filters or RecordFilter())
        limit = ""
        if filters and filters.limit:
            limit = f"LIMIT {int(filters.limit)}"
        rows = self._conn.execute(
            f"SELECT * FROM episodic_records {where} ORDER BY occurred_at DESC {limit}",
            params,
        ).fetchall()
        return [record_from_row(row) for row in rows]

    def count_records(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM episodic_records").fetchone()
        return int(row["n"])

    def vector_search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 20,
        filters: RecordFilter | None = None,
    ) -> list[ScoredRecord]:
        where, params = self._record_where(filters or RecordFilter())
        join = "JOIN record_embeddings e ON e.record_id = r.id"
        sql = f"SELECT r.*, e.vector AS vector FROM episodic_records r {join} {where}"
        rows = self._conn.execute(sql, params).fetchall()
        scored: list[ScoredRecord] = []
        for row in rows:
            vector = decode_vector(row["vector"])
            scored.append(
                ScoredRecord(
                    record=record_from_row(row, vector),
                    similarity=cosine_similarity(embedding, vector),
                )
            )
        scored.sort(key=lambda item: item.similarity, reverse=True)
        return scored[:limit]

    def _record_where(self, filters: RecordFilter) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        # Column names are unqualified because they are unambiguous in both
        # the plain query and the embeddings join used by vector_search.
        if filters.project_ids:
            clauses.append(f"project_id IN ({','.join('?' * len(filters.project_ids))})")
            params.extend(str(value) for value in filters.project_ids)
        if filters.exclude_project_ids:
            clauses.append(
                f"project_id NOT IN ({','.join('?' * len(filters.exclude_project_ids))})"
            )
            params.extend(str(value) for value in filters.exclude_project_ids)
        if filters.types:
            clauses.append(f"type IN ({','.join('?' * len(filters.types))})")
            params.extend(str(value) for value in filters.types)
        if filters.min_confidence > 0:
            clauses.append("confidence >= ?")
            params.append(filters.min_confidence)
        if filters.occurred_after:
            clauses.append("occurred_at >= ?")
            params.append(to_iso(filters.occurred_after))
        if not filters.include_superseded:
            clauses.append("superseded_by IS NULL")
        if filters.entities:
            placeholders = ",".join("?" * len(filters.entities))
            clauses.append(
                f"id IN (SELECT record_id FROM record_entities WHERE entity IN ({placeholders}))"
            )
            params.extend(entity.lower() for entity in filters.entities)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def _embedding_for(self, record_id: UUID) -> list[float] | None:
        row = self._conn.execute(
            "SELECT vector FROM record_embeddings WHERE record_id = ?", (str(record_id),)
        ).fetchone()
        return decode_vector(row["vector"]) if row else None

    def set_embedding(self, record_id: UUID, embedding: Sequence[float]) -> None:
        """Attach or replace an embedding after the fact."""
        self._conn.execute(
            "INSERT OR REPLACE INTO record_embeddings (record_id, dimensions, vector) "
            "VALUES (?, ?, ?)",
            (str(record_id), len(embedding), encode_vector(embedding)),
        )
        self._conn.commit()

    # --- review queue ------------------------------------------------------
    def enqueue_review(self, item: ReviewItem) -> ReviewItem:
        row = review_to_row(item)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        updates = ", ".join(f"{name} = excluded.{name}" for name in row if name != "id")
        self._conn.execute(
            f"INSERT INTO review_queue ({columns}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            row,
        )
        self._conn.commit()
        return item

    def list_reviews(self, *, open_only: bool = True) -> list[ReviewItem]:
        where = "WHERE resolution IS NULL" if open_only else ""
        rows = self._conn.execute(
            f"SELECT * FROM review_queue {where} ORDER BY impact DESC, created_at ASC"
        ).fetchall()
        return [review_from_row(row) for row in rows]

    def resolve_review(
        self,
        review_id: UUID,
        resolution: Resolution,
        *,
        note: str = "",
        resolved_at: datetime | None = None,
    ) -> ReviewItem | None:
        cursor = self._conn.execute(
            "UPDATE review_queue SET resolution = ?, resolution_note = ?, resolved_at = ? "
            "WHERE id = ?",
            (str(resolution), note, to_iso(resolved_at or utcnow()), str(review_id)),
        )
        self._conn.commit()
        if cursor.rowcount == 0:
            return None
        row = self._conn.execute(
            "SELECT * FROM review_queue WHERE id = ?", (str(review_id),)
        ).fetchone()
        return review_from_row(row) if row else None

    # --- fingerprint cache -------------------------------------------------
    def get_cached_fingerprint(self, project_key: str) -> tuple[str, Fingerprint] | None:
        row = self._conn.execute(
            "SELECT manifest_hash, fingerprint FROM fingerprint_cache WHERE project_key = ?",
            (project_key,),
        ).fetchone()
        if not row:
            return None
        return row["manifest_hash"], Fingerprint.model_validate(loads(row["fingerprint"], {}))

    def put_cached_fingerprint(self, project_key: str, fingerprint: Fingerprint) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO fingerprint_cache "
            "(project_key, manifest_hash, fingerprint, computed_at) VALUES (?, ?, ?, ?)",
            (
                project_key,
                fingerprint.manifest_hash,
                dumps(fingerprint.model_dump(mode="json")),
                to_iso(fingerprint.computed_at),
            ),
        )
        self._conn.commit()

    def invalidate_fingerprint(self, project_key: str) -> None:
        self._conn.execute("DELETE FROM fingerprint_cache WHERE project_key = ?", (project_key,))
        self._conn.commit()

    # --- telemetry ---------------------------------------------------------
    def log_bundle(self, entry: BundleLogEntry) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO bundle_log (
                bundle_id, created_at, project_key, task_type, prompt_hash, prompt_words,
                profile_ids, episodic_ids, cross_project_ids, profile_tokens, episodic_tokens,
                gate_reason, latency_ms, injected, feedback, feedback_note
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(entry.bundle_id),
                to_iso(entry.created_at),
                entry.project_key,
                entry.task_type,
                entry.prompt_hash,
                entry.prompt_words,
                dumps([str(value) for value in entry.profile_ids]),
                dumps([str(value) for value in entry.episodic_ids]),
                dumps([str(value) for value in entry.cross_project_ids]),
                entry.profile_tokens,
                entry.episodic_tokens,
                entry.gate_reason,
                entry.latency_ms,
                int(entry.injected),
                None if entry.feedback is None else int(entry.feedback),
                entry.feedback_note,
            ),
        )
        self._conn.commit()

    def record_feedback(self, bundle_id: UUID, *, useful: bool, note: str = "") -> bool:
        cursor = self._conn.execute(
            "UPDATE bundle_log SET feedback = ?, feedback_note = ? WHERE bundle_id = ?",
            (int(useful), note, str(bundle_id)),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def recent_bundles(self, limit: int = 50) -> list[BundleLogEntry]:
        rows = self._conn.execute(
            "SELECT * FROM bundle_log ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [_bundle_from_row(row) for row in rows]

    def usage_stats(self, *, since: datetime | None = None) -> UsageStats:
        where = "WHERE created_at >= ?" if since else ""
        params = [to_iso(since)] if since else []
        rows = self._conn.execute(f"SELECT * FROM bundle_log {where}", params).fetchall()
        return aggregate_usage(_bundle_from_row(row) for row in rows)


def _bundle_from_row(row: Any) -> BundleLogEntry:
    return BundleLogEntry(
        bundle_id=UUID(str(row["bundle_id"])),
        created_at=from_iso(row["created_at"]),
        project_key=row["project_key"],
        task_type=row["task_type"],
        prompt_hash=row["prompt_hash"],
        prompt_words=int(row["prompt_words"]),
        profile_ids=tuple(UUID(v) for v in loads(row["profile_ids"], [])),
        episodic_ids=tuple(UUID(v) for v in loads(row["episodic_ids"], [])),
        cross_project_ids=tuple(UUID(v) for v in loads(row["cross_project_ids"], [])),
        profile_tokens=int(row["profile_tokens"]),
        episodic_tokens=int(row["episodic_tokens"]),
        gate_reason=row["gate_reason"] or "",
        latency_ms=float(row["latency_ms"]),
        injected=bool(row["injected"]),
        feedback=None if row["feedback"] is None else bool(row["feedback"]),
        feedback_note=row["feedback_note"] or "",
    )


__all__ = ["SqliteStore"]
