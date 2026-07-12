"""Scoring the real front door.

The Phase 0 harness scores anything that answers an :class:`EvalQuery`. This
module makes the actual `MemoryService` one of those things, by building a
throwaway store, loading the fixture corpus into it and answering each query
through exactly the code path the MCP server uses.

Nothing here reimplements retrieval. If it did, the eval would be scoring a
copy of the system rather than the system.
"""

from __future__ import annotations

#: Fixture ids are stable strings; the domain model wants UUIDs. A namespaced
#: UUID5 keeps the mapping deterministic in both directions, so a scored result
#: can be traced back to the labelled record it came from.
import uuid as _uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from projectmind.config import Settings
from projectmind.evaluation.fixtures import FixtureCorpus, FixtureProject, FixtureRecord
from projectmind.evaluation.spec import EvalQuery, RetrievalResult
from projectmind.models import (
    EpisodicRecord,
    Fingerprint,
    Project,
    RecordType,
    SourceType,
    utcnow,
)
from projectmind.profile.seed import seed_profile
from projectmind.service import MemoryService
from projectmind.storage import SqliteStore, Store
from projectmind.storage.embeddings import EmbeddingProvider, HashingEmbedder

FIXTURE_NAMESPACE = _uuid.UUID("6f1d1f4a-2c8e-5b7a-9d3e-0a1b2c3d4e5f")


def fixture_uuid(fixture_id: str) -> UUID:
    return _uuid.uuid5(FIXTURE_NAMESPACE, fixture_id)


def _project_from_fixture(fixture: FixtureProject) -> Project:
    return Project(
        id=fixture_uuid(f"project:{fixture.key}"),
        key=fixture.key,
        name=fixture.name,
        git_remote=fixture.git_remote,
        fingerprint=Fingerprint(
            languages=fixture.languages,
            frameworks=fixture.frameworks,
            dependencies=fixture.dependencies,
            manifest_hash=f"fixture-{fixture.key}",
        ),
    )


def _record_from_fixture(
    fixture: FixtureRecord,
    project_id: UUID,
    embedder: EmbeddingProvider,
) -> EpisodicRecord:
    occurred = datetime(
        fixture.occurred_at.year, fixture.occurred_at.month, fixture.occurred_at.day, tzinfo=UTC
    )
    record = EpisodicRecord(
        id=fixture_uuid(fixture.id),
        project_id=project_id,
        type=RecordType(fixture.type),
        what=fixture.what,
        why=fixture.why,
        rejected_alternatives=fixture.rejected_alternatives,
        outcome=fixture.outcome,
        entities=fixture.entities,
        source_type=SourceType(fixture.source_type),
        source_url=fixture.source_url,
        occurred_at=occurred,
        confidence=fixture.confidence,
        is_inference=fixture.is_inference,
        ingested_at=utcnow(),
    )
    return record.model_copy(
        update={"embedding": tuple(embedder.embed([record.searchable_text])[0])}
    )


def load_corpus(store: Store, corpus: FixtureCorpus | None = None) -> dict[str, UUID]:
    """Load the fixture corpus into a store. Returns fixture id to record id."""
    corpus = corpus or FixtureCorpus.load()
    embedder: EmbeddingProvider = HashingEmbedder()

    project_ids: dict[str, UUID] = {}
    for fixture in corpus.projects:
        stored = store.upsert_project(_project_from_fixture(fixture))
        project_ids[fixture.key] = stored.id

    records = [
        _record_from_fixture(record, project_ids[record.project], embedder)
        for record in corpus.records
    ]
    store.add_records(records)
    return {record.id: fixture_uuid(record.id) for record in corpus.records}


@contextmanager
def live_service(
    tmp_dir: Path,
    *,
    with_corpus: bool = True,
    with_profile: bool = True,
) -> Iterator[MemoryService]:
    """A fully configured service over a throwaway database."""
    settings = Settings(home=tmp_dir, db_url=None)
    store = SqliteStore(tmp_dir / "eval.db")
    store.migrate()
    service = MemoryService(store, settings)
    if with_profile:
        seed_profile(service.profile, activate=True)
    if with_corpus:
        load_corpus(store)
    try:
        yield service
    finally:
        service.close()


class LiveSystem:
    """Adapter that answers eval queries through the real front door."""

    name = "live"

    def __init__(self, service: MemoryService, corpus: FixtureCorpus | None = None) -> None:
        self.service = service
        self.corpus = corpus or FixtureCorpus.load()
        self._roots: dict[str, str] = {}
        self._by_uuid = {fixture_uuid(record.id): record.id for record in self.corpus.records}

    def __call__(self, query: EvalQuery) -> RetrievalResult:
        bundle = self.service.get_context(
            query.prompt,
            self._root_for(query.project),
            log_bundle=False,
        )
        return RetrievalResult(
            query_id=query.id,
            profile_ids=tuple(str(item.id) for item in bundle.profile),
            episodic_ids=tuple(
                self._by_uuid.get(item.id, str(item.id)) for item in bundle.episodic
            ),
            task_type=bundle.task_type,
            profile_tokens=bundle.profile_tokens,
            episodic_tokens=bundle.episodic_tokens,
            latency_ms=bundle.latency_ms,
            skipped_reason=bundle.gate_reason if bundle.is_empty else None,
        )

    def _root_for(self, project_key: str) -> str:
        """A path the service can resolve to the right fixture project.

        The fixture projects have no directory on disk, so the service is
        pointed at a name that slugifies to the fixture key. The fingerprint
        then comes from the stored project record rather than from a scan.
        """
        return self._roots.setdefault(project_key, project_key)
