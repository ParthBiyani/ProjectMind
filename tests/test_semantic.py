"""Matching by meaning: stored vectors, search, and serving only what is close enough.

A tiny concept encoder stands in for the real model, so these tests run without a
download: "heart" and "cardiac" land on the same axis, as a real model would put them
close together.
"""

from __future__ import annotations

import os
import zlib
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from projectmind.config import Settings
from projectmind.retrieval.evidence import EvidenceRetriever
from projectmind.semantic import SemanticIndex
from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.storage import SqliteStore, Store

CONCEPTS = {
    "heart": 0,
    "cardiac": 0,
    "cycle": 1,
    "beat": 1,
    "voice": 2,
    "speech": 2,
    "brief": 3,
    "summary": 3,
    "kubernetes": 4,
}
DIM = 8 + 256


class ConceptEncoder:
    """Words that mean the same thing share an axis; everything else is noise."""

    def _vector(self, text: str) -> np.ndarray[Any, Any]:
        vector = np.zeros(DIM, dtype="float32")
        for word in text.lower().replace("\n", " ").split():
            token = word.strip(".,?!:#")
            axis = CONCEPTS.get(token, 8 + zlib.crc32(token.encode()) % 256)
            vector[axis] += 1.0
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else vector

    def embed(self, texts: Sequence[str]) -> Any:
        return np.vstack([self._vector(text) for text in texts])

    def embed_query(self, text: str) -> Any:
        return self._vector(text)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - 7_200
    os.utime(path, (stamp, stamp))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    root = tmp_path / "work"
    write(
        root / "cardiology" / "NOTES.md", "# Cardiac\n\n## Cardiac Cycle\n\nCardiac cycle timing.\n"
    )
    write(root / "dictation" / "NOTES.md", "# Voice\n\n## Speech\n\nSpeech voice capture.\n")
    write(root / "ops" / "NOTES.md", "# Ops\n\n## Cluster\n\nKubernetes kubernetes rollout.\n")
    backend = SqliteStore(":memory:")
    backend.migrate()
    KnowledgeIndexer(backend).scan_root(RootConfig(path=root, depth=1))
    yield backend
    backend.close()


@pytest.fixture
def index(store: Store) -> SemanticIndex:
    built = SemanticIndex(store, encoder=ConceptEncoder())
    built.embed_pending()
    built.load()
    return built


class TestBuilding:
    def test_every_passage_gets_a_vector_once(self, store: Store) -> None:
        built = SemanticIndex(store, encoder=ConceptEncoder())
        total = store.count_chunks()
        assert built.pending() == total
        assert built.embed_pending() == total
        assert built.pending() == 0
        assert built.embed_pending() == 0

    def test_a_limit_embeds_only_that_many(self, store: Store) -> None:
        built = SemanticIndex(store, encoder=ConceptEncoder())
        assert built.embed_pending(limit=1) == 1
        assert built.pending() == store.count_chunks() - 1

    def test_load_reads_back_what_was_stored(self, store: Store) -> None:
        built = SemanticIndex(store, encoder=ConceptEncoder())
        built.embed_pending()
        assert built.load() == store.count_chunks()
        assert built.size == store.count_chunks()

    def test_a_different_model_starts_empty(self, store: Store) -> None:
        SemanticIndex(store, encoder=ConceptEncoder()).embed_pending()
        other = SemanticIndex(store, model_name="other", encoder=ConceptEncoder())
        assert other.pending() == store.count_chunks()

    def test_deleting_a_document_removes_its_vectors(self, store: Store, tmp_path: Path) -> None:
        built = SemanticIndex(store, encoder=ConceptEncoder())
        built.embed_pending()
        before = store.count_chunk_vectors(built.model_name)
        (tmp_path / "work" / "ops" / "NOTES.md").unlink()
        KnowledgeIndexer(store).scan_root(RootConfig(path=tmp_path / "work", depth=1))
        assert store.count_chunk_vectors(built.model_name) < before


class TestSearch:
    def test_a_synonym_finds_the_passage(self, index: SemanticIndex, store: Store) -> None:
        top = index.search("the heart beat", k=1)[0]
        found = store.get_chunk(top.chunk_id)
        assert found is not None
        assert "cardiology" in found.document.uri
        assert top.similarity > 0.7

    def test_nothing_loaded_finds_nothing(self, store: Store) -> None:
        assert SemanticIndex(store, encoder=ConceptEncoder()).search("heart") == []

    def test_results_are_best_first(self, index: SemanticIndex) -> None:
        similarities = [hit.similarity for hit in index.search("heart cycle", k=5)]
        assert similarities == sorted(similarities, reverse=True)


class TestServing:
    def retriever(self, store: Store, index: SemanticIndex | None) -> EvidenceRetriever:
        return EvidenceRetriever(store, Settings(db_url=None), semantic=index)

    def test_without_the_index_a_reworded_question_is_silent(self, store: Store) -> None:
        assert self.retriever(store, None).retrieve("the heart beat") == []

    def test_with_the_index_a_reworded_question_is_answered_and_cited(
        self, store: Store, index: SemanticIndex
    ) -> None:
        served = self.retriever(store, index).retrieve("the heart beat")
        assert served
        assert "Cardiac cycle timing" in served[0].excerpt
        assert "cardiology" in served[0].citation

    def test_a_question_about_nothing_written_stays_silent(
        self, store: Store, index: SemanticIndex
    ) -> None:
        assert self.retriever(store, index).retrieve("quarterly invoice reconciliation") == []

    def test_a_close_but_unrelated_passage_is_below_the_threshold(
        self, store: Store, index: SemanticIndex
    ) -> None:
        strict = Settings(db_url=None, semantic_threshold=0.999)
        assert EvidenceRetriever(store, strict, semantic=index).retrieve("heart voice") == []

    def test_lexical_answers_are_kept_and_not_repeated(
        self, store: Store, index: SemanticIndex
    ) -> None:
        served = self.retriever(store, index).retrieve("What is the cardiac cycle?")
        ids = [item.chunk_id for item in served]
        assert ids
        assert len(ids) == len(set(ids))

    def test_the_trace_records_what_was_chosen(self, store: Store, index: SemanticIndex) -> None:
        trace: list[dict[str, Any]] = []
        self.retriever(store, index).retrieve("the heart beat", trace=trace)
        assert any(step["stage"] == "semantic" and step["outcome"] == "served" for step in trace)
