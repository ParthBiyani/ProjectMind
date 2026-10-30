"""Small perturbations of a good question must not turn an answer into silence,
and big ones (a mostly unfamiliar question) still must."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from projectmind.config import Settings
from projectmind.retrieval.evidence import EvidenceRetriever, question_terms
from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.storage import SqliteStore, Store


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - 7_200
    os.utime(path, (stamp, stamp))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    root = tmp_path / "work"
    write(
        root / "ledger" / "NOTES.md",
        "# Ledger\n\n## Reports\n\n"
        "We dropped the profit factor column from the variations table.\n",
    )
    write(
        root / "cafe" / "NOTES.md",
        "# Menu\n\n## Drinks\n\n"
        "The café menu lists espresso, cortado and a seasonal filter coffee.\n",
    )
    for index in range(60):
        write(
            root / "noise" / f"n{index}.md",
            # "table" and "report" are ordinary words in real notes, as they are here.
            f"# Note {index}\n\nFiller about item {index}: the table, the report and a summary.\n",
        )
    backend = SqliteStore(":memory:")
    backend.migrate()
    KnowledgeIndexer(backend).scan_root(RootConfig(path=root, depth=1))
    yield backend
    backend.close()


def ask(store: Store, prompt: str) -> list[str]:
    retriever = EvidenceRetriever(store, Settings(db_url=None))
    return [item.excerpt for item in retriever.retrieve(prompt)]


class TestUnicode:
    def test_an_accented_word_is_one_word(self) -> None:
        assert "café" in question_terms("What is on the café menu?")
        assert "caf" not in question_terms("What is on the café menu?")

    def test_accented_text_is_found_by_its_accented_word(self, store: Store) -> None:
        assert any("espresso" in text for text in ask(store, "What does the café menu list?"))

    def test_the_unaccented_spelling_finds_it_too(self, store: Store) -> None:
        assert any("espresso" in text for text in ask(store, "What does the cafe menu list?"))

    def test_non_latin_text_does_not_break_the_tokenizer(self) -> None:
        assert question_terms("数据库 index") != []


class TestOneUnfamiliarWord:
    def test_a_single_unknown_word_does_not_veto_a_full_match(self, store: Store) -> None:
        texts = ask(store, "Why did we drop the profit factor column, zorblax?")
        assert any("profit factor column" in text for text in texts)

    def test_a_stray_rare_word_that_exists_elsewhere_does_not_veto_either(
        self, store: Store
    ) -> None:
        """ "café" is in the corpus (in another document), so it is a rare word the
        ledger note lacks. That must not stop the note that answers the question."""
        texts = ask(store, "Why did we drop the profit factor column? café")
        assert any("profit factor column" in text for text in texts)

    def test_generic_words_alone_are_still_never_enough(self, store: Store) -> None:
        assert ask(store, "zorblax quuxify the table, the report and a summary") == []

    def test_a_trailing_emoji_or_symbol_changes_nothing(self, store: Store) -> None:
        texts = ask(store, "Why did we drop the profit factor column? — ✨")
        assert any("profit factor column" in text for text in texts)

    def test_a_question_that_is_mostly_unfamiliar_still_gets_nothing(self, store: Store) -> None:
        assert ask(store, "Explain zorblax quuxification of the frobnicator column") == []

    def test_an_unfamiliar_subject_with_generic_words_is_still_silent(self, store: Store) -> None:
        assert ask(store, "Fix the zorblax table report") == []
