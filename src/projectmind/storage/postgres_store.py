"""Postgres and pgvector backend.

Selected by setting a `postgresql://` URL. Same contract, same table names and
same semantics as the SQLite backend; the differences are that JSON is `jsonb`,
timestamps are `timestamptz`, and nearest-neighbour search happens in the
database against an HNSW index rather than in Python.

`psycopg` is an optional dependency. Install with `projectmind[postgres]`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
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

if TYPE_CHECKING:  # pragma: no cover - import only needed for annotations
    from psycopg import Connection

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "postgres"

#: Columns that must be cast on the way in, because the shared row codecs hand
#: over JSON strings and ISO timestamps rather than native objects.
_JSON_COLUMNS = frozenset(
    {
        "fingerprint",
        "scope",
        "evidence_refs",
        "contradiction_refs",
        "entities",
        "rejected_alternatives",
        "profile_ids",
        "episodic_ids",
        "cross_project_ids",
    }
)
_TIMESTAMP_COLUMNS = frozenset(
    {
        "first_seen",
        "last_active",
        "created_at",
        "last_confirmed_at",
        "occurred_at",
        "ingested_at",
        "resolved_at",
        "computed_at",
    }
)
_UUID_COLUMNS = frozenset(
    {"id", "project_id", "superseded_by", "supersedes", "target_id", "batch_id", "bundle_id"}
)


def vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text input format."""
    return "[" + ",".join(f"{float(value):.8g}" for value in vector) + "]"


def parse_vector(value: Any) -> list[float]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [float(item) for item in value]
    text = str(value).strip().strip("[]")
    return [float(part) for part in text.split(",") if part.strip()]


def _placeholder(column: str) -> str:
    """Named placeholder with the cast the column needs."""
    if column in _JSON_COLUMNS:
        return f"%({column})s::jsonb"
    if column in _TIMESTAMP_COLUMNS:
        return f"%({column})s::timestamptz"
    if column in _UUID_COLUMNS:
        return f"%({column})s::uuid"
    return f"%({column})s"


class PostgresStore(Store):
    """Store backed by Postgres with the pgvector extension."""

    dialect = "postgres"

    def __init__(self, dsn: str) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ModuleNotFoundError as exc:  # pragma: no cover - depends on extras
            raise RuntimeError(
                "the postgres backend needs psycopg; install projectmind[postgres]"
            ) from exc
        self.dsn = dsn
        self._conn: Connection[dict[str, Any]] = psycopg.connect(dsn, row_factory=dict_row)
        self._conn.autocommit = True

    # --- lifecycle ---------------------------------------------------------
    def migrate(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL)"
            )
            cur.execute("SELECT version FROM schema_migrations")
            applied = {int(row["version"]) for row in cur.fetchall()}
            for script in sorted(MIGRATIONS_DIR.glob("*.sql")):
                version = int(script.name.split("_", 1)[0])
                if version in applied:
                    continue
                cur.execute(script.read_text(encoding="utf-8"))
                cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (version, utcnow()),
                )
                log.info("applied migration", extra={"version": version, "dialect": self.dialect})
            cur.execute("SELECT MAX(version) AS v FROM schema_migrations")
            row = cur.fetchone()
        return int((row or {}).get("v") or 0)

    def close(self) -> None:
        self._conn.close()

    def _query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _execute(self, sql: str, params: Any = None) -> int:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def _upsert(self, table: str, row: dict[str, Any], conflict: str) -> None:
        columns = ", ".join(row)
        values = ", ".join(_placeholder(name) for name in row)
        updates = ", ".join(f"{name} = EXCLUDED.{name}" for name in row if name != conflict)
        self._execute(
            f"INSERT INTO {table} ({columns}) VALUES ({values}) "
            f"ON CONFLICT ({conflict}) DO UPDATE SET {updates}",
            row,
        )

    # --- projects ----------------------------------------------------------
    def upsert_project(self, project: Project) -> Project:
        row = project_to_row(project)
        existing = self.get_project(project.key)
        if existing:
            row["id"] = str(existing.id)
            row["first_seen"] = to_iso(existing.first_seen)
        self._upsert("projects", row, "key")
        stored = self.get_project(project.key)
        assert stored is not None
        return stored

    def get_project(self, key: str) -> Project | None:
        rows = self._query("SELECT * FROM projects WHERE key = %s", (key,))
        return project_from_row(rows[0]) if rows else None

    def get_project_by_id(self, project_id: UUID) -> Project | None:
        rows = self._query("SELECT * FROM projects WHERE id = %s", (str(project_id),))
        return project_from_row(rows[0]) if rows else None

    def list_projects(self) -> list[Project]:
        rows = self._query("SELECT * FROM projects ORDER BY last_active DESC")
        return [project_from_row(row) for row in rows]

    # --- profile -----------------------------------------------------------
    def upsert_statement(self, statement: ProfileStatement) -> ProfileStatement:
        self._upsert("profile_statements", statement_to_row(statement), "id")
        return statement

    def get_statement(self, statement_id: UUID) -> ProfileStatement | None:
        rows = self._query("SELECT * FROM profile_statements WHERE id = %s", (str(statement_id),))
        return statement_from_row(rows[0]) if rows else None

    def list_statements(self, filters: StatementFilter | None = None) -> list[ProfileStatement]:
        filters = filters or StatementFilter()
        clauses: list[str] = []
        params: list[Any] = []
        if filters.statuses:
            clauses.append("status = ANY(%s)")
            params.append([str(status) for status in filters.statuses])
        if filters.categories:
            clauses.append("category = ANY(%s)")
            params.append([str(category) for category in filters.categories])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = f"LIMIT {int(filters.limit)}" if filters.limit else ""
        rows = self._query(
            f"SELECT * FROM profile_statements {where} ORDER BY confidence DESC {limit}", params
        )
        statements = [statement_from_row(row) for row in rows]
        if filters.entities:
            wanted = {entity.lower() for entity in filters.entities}
            statements = [s for s in statements if wanted & set(s.entities)]
        return statements

    def delete_statement(self, statement_id: UUID) -> bool:
        return (
            self._execute("DELETE FROM profile_statements WHERE id = %s", (str(statement_id),)) > 0
        )

    # --- episodic ----------------------------------------------------------
    def add_records(self, records: Iterable[EpisodicRecord]) -> int:
        inserted = 0
        for record in records:
            row = record_to_row(record)
            row["is_inference"] = bool(record.is_inference)
            columns = ", ".join(row)
            values = ", ".join(_placeholder(name) for name in row)
            affected = self._execute(
                f"INSERT INTO episodic_records ({columns}) VALUES ({values}) "
                f"ON CONFLICT (content_hash) DO NOTHING",
                row,
            )
            if affected == 0:
                continue
            inserted += 1
            for entity in record.entities:
                self._execute(
                    "INSERT INTO record_entities (record_id, entity) VALUES (%s::uuid, %s) "
                    "ON CONFLICT DO NOTHING",
                    (str(record.id), entity),
                )
            if record.embedding:
                self.set_embedding(record.id, record.embedding)
        return inserted

    def get_record(self, record_id: UUID) -> EpisodicRecord | None:
        rows = self._query(
            "SELECT r.*, e.vector AS vector FROM episodic_records r "
            "LEFT JOIN record_embeddings e ON e.record_id = r.id WHERE r.id = %s",
            (str(record_id),),
        )
        if not rows:
            return None
        vector = parse_vector(rows[0].get("vector"))
        return record_from_row(rows[0], vector or None)

    def list_records(self, filters: RecordFilter | None = None) -> list[EpisodicRecord]:
        where, params = self._record_where(filters or RecordFilter())
        limit = ""
        if filters and filters.limit:
            limit = f"LIMIT {int(filters.limit)}"
        rows = self._query(
            f"SELECT * FROM episodic_records {where} ORDER BY occurred_at DESC {limit}", params
        )
        return [record_from_row(row) for row in rows]

    def count_records(self) -> int:
        rows = self._query("SELECT COUNT(*) AS n FROM episodic_records")
        return int(rows[0]["n"])

    def vector_search(
        self,
        embedding: Sequence[float],
        *,
        limit: int = 20,
        filters: RecordFilter | None = None,
    ) -> list[ScoredRecord]:
        where, params = self._record_where(filters or RecordFilter(), alias="r")
        literal = vector_literal(embedding)
        sql = (
            "SELECT r.*, e.vector AS vector, "
            "1 - (e.vector <=> %s::vector) AS similarity "
            "FROM episodic_records r JOIN record_embeddings e ON e.record_id = r.id "
            f"{where} ORDER BY e.vector <=> %s::vector LIMIT %s"
        )
        rows = self._query(sql, [literal, *params, literal, int(limit)])
        return [
            ScoredRecord(
                record=record_from_row(row, parse_vector(row.get("vector")) or None),
                similarity=float(row["similarity"]),
            )
            for row in rows
        ]

    def _record_where(self, filters: RecordFilter, alias: str = "") -> tuple[str, list[Any]]:
        prefix = f"{alias}." if alias else ""
        clauses: list[str] = []
        params: list[Any] = []
        if filters.project_ids:
            clauses.append(f"{prefix}project_id = ANY(%s::uuid[])")
            params.append([str(value) for value in filters.project_ids])
        if filters.exclude_project_ids:
            clauses.append(f"NOT ({prefix}project_id = ANY(%s::uuid[]))")
            params.append([str(value) for value in filters.exclude_project_ids])
        if filters.types:
            clauses.append(f"{prefix}type = ANY(%s)")
            params.append([str(value) for value in filters.types])
        if filters.min_confidence > 0:
            clauses.append(f"{prefix}confidence >= %s")
            params.append(filters.min_confidence)
        if filters.occurred_after:
            clauses.append(f"{prefix}occurred_at >= %s")
            params.append(filters.occurred_after)
        if not filters.include_superseded:
            clauses.append(f"{prefix}superseded_by IS NULL")
        if filters.entities:
            clauses.append(
                f"{prefix}id IN (SELECT record_id FROM record_entities WHERE entity = ANY(%s))"
            )
            params.append([entity.lower() for entity in filters.entities])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def set_embedding(self, record_id: UUID, embedding: Sequence[float]) -> None:
        self._execute(
            "INSERT INTO record_embeddings (record_id, dimensions, vector) "
            "VALUES (%s::uuid, %s, %s::vector) "
            "ON CONFLICT (record_id) DO UPDATE SET "
            "dimensions = EXCLUDED.dimensions, vector = EXCLUDED.vector",
            (str(record_id), len(embedding), vector_literal(embedding)),
        )

    # --- review queue ------------------------------------------------------
    def enqueue_review(self, item: ReviewItem) -> ReviewItem:
        self._upsert("review_queue", review_to_row(item), "id")
        return item

    def list_reviews(self, *, open_only: bool = True) -> list[ReviewItem]:
        where = "WHERE resolution IS NULL" if open_only else ""
        rows = self._query(
            f"SELECT * FROM review_queue {where} ORDER BY impact DESC, created_at ASC"
        )
        return [review_from_row(row) for row in rows]

    def resolve_review(
        self,
        review_id: UUID,
        resolution: Resolution,
        *,
        note: str = "",
        resolved_at: datetime | None = None,
    ) -> ReviewItem | None:
        affected = self._execute(
            "UPDATE review_queue SET resolution = %s, resolution_note = %s, resolved_at = %s "
            "WHERE id = %s::uuid",
            (str(resolution), note, resolved_at or utcnow(), str(review_id)),
        )
        if affected == 0:
            return None
        rows = self._query("SELECT * FROM review_queue WHERE id = %s", (str(review_id),))
        return review_from_row(rows[0]) if rows else None

    # --- fingerprint cache -------------------------------------------------
    def get_cached_fingerprint(self, project_key: str) -> tuple[str, Fingerprint] | None:
        rows = self._query(
            "SELECT manifest_hash, fingerprint FROM fingerprint_cache WHERE project_key = %s",
            (project_key,),
        )
        if not rows:
            return None
        return rows[0]["manifest_hash"], Fingerprint.model_validate(
            loads(rows[0]["fingerprint"], {})
        )

    def put_cached_fingerprint(self, project_key: str, fingerprint: Fingerprint) -> None:
        self._execute(
            "INSERT INTO fingerprint_cache (project_key, manifest_hash, fingerprint, computed_at) "
            "VALUES (%s, %s, %s::jsonb, %s) "
            "ON CONFLICT (project_key) DO UPDATE SET "
            "manifest_hash = EXCLUDED.manifest_hash, fingerprint = EXCLUDED.fingerprint, "
            "computed_at = EXCLUDED.computed_at",
            (
                project_key,
                fingerprint.manifest_hash,
                dumps(fingerprint.model_dump(mode="json")),
                fingerprint.computed_at,
            ),
        )

    def invalidate_fingerprint(self, project_key: str) -> None:
        self._execute("DELETE FROM fingerprint_cache WHERE project_key = %s", (project_key,))

    # --- telemetry ---------------------------------------------------------
    def log_bundle(self, entry: BundleLogEntry) -> None:
        self._execute(
            """
            INSERT INTO bundle_log (
                bundle_id, created_at, project_key, task_type, prompt_hash, prompt_words,
                profile_ids, episodic_ids, cross_project_ids, profile_tokens, episodic_tokens,
                gate_reason, latency_ms, injected, feedback, feedback_note
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (bundle_id) DO UPDATE SET
                feedback = EXCLUDED.feedback, feedback_note = EXCLUDED.feedback_note
            """,
            (
                str(entry.bundle_id),
                entry.created_at,
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
                entry.injected,
                entry.feedback,
                entry.feedback_note,
            ),
        )

    def record_feedback(self, bundle_id: UUID, *, useful: bool, note: str = "") -> bool:
        return (
            self._execute(
                "UPDATE bundle_log SET feedback = %s, feedback_note = %s WHERE bundle_id = %s",
                (useful, note, str(bundle_id)),
            )
            > 0
        )

    def recent_bundles(self, limit: int = 50) -> list[BundleLogEntry]:
        rows = self._query(
            "SELECT * FROM bundle_log ORDER BY created_at DESC LIMIT %s", (int(limit),)
        )
        return [_bundle_from_row(row) for row in rows]

    def usage_stats(self, *, since: datetime | None = None) -> UsageStats:
        where = "WHERE created_at >= %s" if since else ""
        rows = self._query(f"SELECT * FROM bundle_log {where}", (since,) if since else None)
        return aggregate_usage(_bundle_from_row(row) for row in rows)


def _bundle_from_row(row: dict[str, Any]) -> BundleLogEntry:
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


__all__ = ["PostgresStore", "parse_vector", "vector_literal"]
