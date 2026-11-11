"""Drawing passages for a blind evaluation set."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from projectmind.evaluation.real import RealSet
from projectmind.evaluation.sample import MAX_CHARS, MIN_CHARS, draw, skeleton
from projectmind.storage import ChunkRecord, DocumentRecord, SqliteStore, Store

BODY = "The retry loop backs off exponentially and gives up after five attempts. " * 6


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def add(store: Store, uri: str, text: str, source_type: str = "doc") -> None:
    now = datetime.now(UTC)
    document = DocumentRecord(
        id=uuid4(),
        uri=uri,
        source_type=source_type,
        root="r",
        project_key="p",
        title=uri,
        content_hash="h",
        mtime=now,
        indexed_at=now,
    )
    store.replace_document(
        document, [ChunkRecord(id=uuid4(), document_id=document.id, ordinal=0, text=text)]
    )


def fill(store: Store) -> None:
    for i in range(12):
        add(store, f"D:/work/notes{i}.md", BODY, "doc")
    for i in range(3):
        add(store, f"D:/work/paper{i}.pdf", BODY, "pdf")
    add(store, "D:/work/run.log", BODY, "log")


class TestDraw:
    def test_the_same_seed_draws_the_same_passages(self, store: Store) -> None:
        fill(store)
        first = [s.document.uri for s in draw(store, 6, seed=7)]
        again = [s.document.uri for s in draw(store, 6, seed=7)]
        other = [s.document.uri for s in draw(store, 6, seed=8)]
        assert first == again
        assert first != other

    def test_every_source_type_gets_a_share_before_any_gets_two(self, store: Store) -> None:
        fill(store)
        kinds = [s.document.source_type for s in draw(store, 3, seed=1)]
        assert sorted(kinds) == ["doc", "log", "pdf"]

    def test_one_passage_per_file(self, store: Store) -> None:
        for number in range(3):
            add(store, "D:/work/one.md", BODY + str(number))
        assert len(draw(store, 10, seed=1)) == 1

    def test_too_short_and_too_long_passages_are_not_drawn(self, store: Store) -> None:
        add(store, "D:/work/short.md", "x" * (MIN_CHARS - 1))
        add(store, "D:/work/huge.md", "y" * (MAX_CHARS + 1))
        assert draw(store, 5, seed=1) == []

    def test_web_pages_and_commits_are_never_drawn(self, store: Store) -> None:
        add(store, "https://example.org/a", BODY, "web")
        add(store, "https://github.com/x/y/commit/1", BODY, "commit")
        assert draw(store, 5, seed=1) == []

    def test_excluded_addresses_are_left_out(self, store: Store) -> None:
        add(store, "D:/Resume/cv.md", BODY)
        add(store, "D:/work/notes.md", BODY)
        uris = [s.document.uri for s in draw(store, 5, seed=1, exclude=["resume"])]
        assert uris == ["D:/work/notes.md"]

    def test_asking_for_more_than_exist_returns_what_exists(self, store: Store) -> None:
        fill(store)
        assert len(draw(store, 500, seed=1)) == 16


class TestSkeleton:
    def test_the_excerpt_is_redacted(self, store: Store) -> None:
        add(store, "D:/work/n.md", BODY + " my password is Tr0ub4dor&3 ok")
        text = skeleton(draw(store, 1, seed=1))
        assert "Tr0ub4dor" not in text
        assert "[REDACTED:password]" in text

    def test_it_loads_as_an_evaluation_set_once_the_prompts_are_filled_in(
        self, store: Store, tmp_path: Path
    ) -> None:
        fill(store)
        text = skeleton(draw(store, 4, seed=3))
        loaded = yaml.safe_load(text)
        assert loaded["name"] == "blind"
        assert all(q["any_of"] and q["prompt"] == "TODO" for q in loaded["queries"])
        for number, query in enumerate(loaded["queries"]):
            query["prompt"] = f"Question number {number}?"
        path = tmp_path / "blind.yaml"
        path.write_text(yaml.safe_dump(loaded), encoding="utf-8")
        assert len(RealSet.from_yaml(path).queries) == 4

    def test_the_label_is_the_files_own_tail(self, store: Store) -> None:
        add(store, "D:/work/deep/notes.md", BODY)
        [sample] = draw(store, 1, seed=1)
        assert sample.label == "deep/notes.md"
