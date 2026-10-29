from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from projectmind.config import Settings
from projectmind.retrieval.evidence import EvidenceRetriever, idf, question_terms
from projectmind.service import MemoryService
from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.storage import SqliteStore, Store

FILLER = "Notes about nothing in particular, written to make the corpus a realistic size."


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - 7_200
    os.utime(path, (stamp, stamp))


@pytest.fixture
def corpus(tmp_path: Path) -> Iterator[tuple[Store, Path]]:
    """A small knowledge base: two competitions, one trading project, some noise."""
    root = tmp_path / "work"
    write(
        root / "Prostate MRI" / "CLAUDE.md",
        "# Prostate MRI\n\n## Score definition\n\n"
        "Score is 10 * (0.55*Dice + 0.25*boundary F1). Exact voxel matching, no tolerance.\n\n"
        "## Result\n\nBest cross validation dice reached 0.62 with the patch unet.\n",
    )
    write(
        root / "Albanian News" / "CLAUDE.md",
        "# Albanian News\n\n## Metric\n\n"
        "The metric is chrF on recovered cards. CPU only, 90 minute limit.\n",
    )
    write(
        root / "Alpha Desk" / "NOTES.md",
        "# Alpha Desk\n\n## Indicators\n\n"
        "The ADX block returns di plus, di minus and adx as a tuple.\n"
        "Use period 14 unless the strategy says otherwise.\n\n"
        "## Reports\n\nThe variations table no longer shows the profit factor column.\n",
    )
    for index in range(30):
        write(root / "Noise" / f"n{index}.md", f"# Note {index}\n\n{FILLER} Item {index}.\n")
    backend = SqliteStore(":memory:")
    backend.migrate()
    KnowledgeIndexer(backend).scan_root(RootConfig(path=root, kind="project", depth=1))
    yield backend, root
    backend.close()


def retriever(store: Store, **settings: object) -> EvidenceRetriever:
    return EvidenceRetriever(store, Settings(db_url=None, **settings))  # type: ignore[arg-type]


class TestQuestionTerms:
    def test_filler_and_stopwords_are_dropped(self) -> None:
        assert question_terms("Please help me handle the ADX block") == ["adx", "block"]

    def test_names_with_punctuation_are_split_the_way_the_index_splits_them(self) -> None:
        assert question_terms("walk-forward validation") == ["walk", "forward", "validation"]

    def test_short_fragments_are_dropped_but_short_names_are_kept(self) -> None:
        terms = question_terms("is it a UI or the CV F1 on x")
        assert "cv" in terms
        assert "f1" in terms
        assert "x" not in terms
        assert "ui" in terms

    def test_duplicates_collapse(self) -> None:
        assert question_terms("adx ADX adx") == ["adx"]

    def test_a_term_the_corpus_never_saw_is_the_most_informative(self) -> None:
        assert idf(1000, 0) > idf(1000, 1) > idf(1000, 400)


class TestServing:
    def test_a_specific_question_gets_the_passage_that_answers_it(
        self, corpus: tuple[Store, Path]
    ) -> None:
        store, _ = corpus
        [first, *_] = retriever(store).retrieve("How does the ADX block return its values?")
        assert "adx" in first.excerpt.lower()
        assert first.project_key == "alpha-desk"
        assert first.source_type == "doc"

    def test_a_question_about_something_the_corpus_never_says_gets_nothing(
        self, corpus: tuple[Store, Path]
    ) -> None:
        store, _ = corpus
        assert retriever(store).retrieve("Set up kubernetes ingress certificate rotation") == []

    def test_sharing_only_generic_words_is_not_evidence(self, corpus: tuple[Store, Path]) -> None:
        store, _ = corpus
        # "notes" and "realistic" exist in the noise; "zeppelin" and "dirigible" do not.
        assert retriever(store).retrieve("realistic notes about a zeppelin dirigible") == []

    def test_the_most_specific_word_must_be_present(self, corpus: tuple[Store, Path]) -> None:
        store, _ = corpus
        trace: list[dict[str, object]] = []
        retriever(store).retrieve("score definition for the ADX tuple period", trace=trace)
        # The Prostate notes share "score" and "definition" with this question, but
        # not its most specific word, so they must not be served on that basis.
        prostate = [row for row in trace if row.get("project") == "prostate-mri"]
        assert prostate, "the near-miss should at least have been considered"
        assert not any(row["outcome"] == "served" for row in prostate)
        served = [row for row in trace if row.get("outcome") == "served"]
        assert all("adx" in row["matched"] for row in served)  # type: ignore[operator]

    def test_a_named_project_is_preferred(self, corpus: tuple[Store, Path]) -> None:
        store, _ = corpus
        results = retriever(store).retrieve(
            "What was the best dice score on the prostate MRI task?"
        )
        assert results
        assert results[0].project_key == "prostate-mri"

    def test_the_trace_explains_every_decision(self, corpus: tuple[Store, Path]) -> None:
        store, _ = corpus
        trace: list[dict[str, object]] = []
        retriever(store).retrieve("profit factor column in the variations table", trace=trace)
        assert trace
        assert all(row["stage"] in {"evidence", "terms"} for row in trace)
        assert {row["outcome"] for row in trace if row["stage"] == "evidence"} <= {
            "served",
            "cut: below the coverage floor",
            "cut: over the limit",
            "cut: this document already contributed",
            "cut: near-duplicate of a served passage",
            "cut: too few of the question's terms",
        } | {row["outcome"] for row in trace if str(row["outcome"]).startswith("cut: does not")}

    def test_the_limit_is_respected(self, corpus: tuple[Store, Path]) -> None:
        store, _ = corpus
        assert len(retriever(store, max_evidence=1).retrieve("metric chrF dice score")) <= 1


class TestGrounding:
    """The central promise: what is shown is exactly what the file says there."""

    CITATION = re.compile(r"^(?P<path>.+):(?P<start>\d+)(?:-(?P<end>\d+))?$")

    def test_every_excerpt_is_the_literal_text_at_its_citation(
        self, corpus: tuple[Store, Path]
    ) -> None:
        store, _ = corpus
        checked = 0
        for question in (
            "How does the ADX block return its values?",
            "What was the best dice score on the prostate MRI task?",
            "What is the metric chrF on recovered cards?",
            "profit factor column variations table",
        ):
            for item in retriever(store).retrieve(question):
                match = self.CITATION.match(item.citation)
                assert match, item.citation
                lines = Path(match["path"]).read_text(encoding="utf-8").splitlines()
                start = int(match["start"])
                end = int(match["end"] or start)
                assert "\n".join(lines[start - 1 : end]).strip("\n") == item.excerpt.strip("\n")
                checked += 1
        assert checked >= 4

    def test_a_long_chunk_is_cut_to_exact_lines_around_the_match(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        body = "\n".join(f"line {i} says something unrelated about weather" for i in range(40))
        needle = "The zebrafish embryo imaging protocol uses light sheet."
        text = f"# Long\n\n{body}\n\n{needle}\n\n{body}\n"
        write(root / "p" / "long.md", text)
        store = SqliteStore(":memory:")
        store.migrate()
        KnowledgeIndexer(store).scan_root(RootConfig(path=root, depth=1))
        [item] = retriever(store).retrieve("zebrafish embryo light sheet")
        match = self.CITATION.match(item.citation)
        assert match
        lines = (root / "p" / "long.md").read_text(encoding="utf-8").splitlines()
        shown = "\n".join(lines[int(match["start"]) - 1 : int(match["end"] or match["start"])])
        assert shown.strip("\n") == item.excerpt.strip("\n")
        assert "zebrafish" in item.excerpt
        assert len(item.excerpt.splitlines()) <= 12
        store.close()


class TestFrontDoor:
    def test_evidence_reaches_the_bundle_with_its_citation(
        self, corpus: tuple[Store, Path]
    ) -> None:
        store, root = corpus
        memory = MemoryService(store, Settings(db_url=None, recency_half_life_days=1e6))
        bundle = memory.get_context(
            "How does the ADX block return its values?", root / "Alpha Desk"
        )
        assert bundle.evidence
        rendered = bundle.render()
        assert "### From your own files and notes" in rendered
        assert "NOTES.md:" in rendered
        assert bundle.evidence_tokens > 0
        assert bundle.total_tokens >= bundle.evidence_tokens

    def test_a_bundle_with_only_evidence_is_not_empty(self, corpus: tuple[Store, Path]) -> None:
        store, root = corpus
        memory = MemoryService(store, Settings(db_url=None))
        bundle = memory.get_context(
            "How does the ADX block return its values?", root / "Alpha Desk"
        )
        assert bundle.profile == ()
        assert bundle.evidence
        assert not bundle.is_empty

    def test_the_total_budget_still_holds(self, corpus: tuple[Store, Path]) -> None:
        store, root = corpus
        memory = MemoryService(store, Settings(db_url=None))
        bundle = memory.get_context(
            "What is the metric and the score on the prostate MRI task?", root
        )
        assert bundle.total_tokens <= memory.settings.total_token_cap

    def test_a_trivial_prompt_still_gets_nothing(self, corpus: tuple[Store, Path]) -> None:
        store, root = corpus
        memory = MemoryService(store, Settings(db_url=None))
        assert memory.get_context("Rename the variable adx to adx_value", root).is_empty

    def test_the_interaction_log_records_what_evidence_was_served(
        self, corpus: tuple[Store, Path]
    ) -> None:
        store, root = corpus
        memory = MemoryService(store, Settings(db_url=None))
        bundle = memory.get_context(
            "How does the ADX block return its values?", root / "Alpha Desk"
        )
        entry = store.get_bundle(bundle.bundle_id)
        assert entry is not None
        assert entry.evidence_ids == tuple(item.chunk_id for item in bundle.evidence)
        assert entry.evidence_tokens == bundle.evidence_tokens
        assert any(row.get("stage") == "evidence" for row in entry.trace["evidence"])


class TestSmallCorpus:
    """A new user's first week: a handful of passages, not tens of thousands."""

    def test_a_specific_question_is_answered_even_when_one_word_is_absent(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "r"
        write(
            root / "p" / "NOTES.md",
            "# Alpha\n\n## Indicators\n\nThe ADX block returns di plus, di minus and adx.\n",
        )
        store = SqliteStore(":memory:")
        store.migrate()
        KnowledgeIndexer(store).scan_root(RootConfig(path=root, depth=1))
        results = retriever(store).retrieve("How does the ADX block return its values?")
        assert results
        assert "adx" in results[0].excerpt.lower()
        store.close()

    def test_a_wholly_unknown_topic_is_still_silent(self, tmp_path: Path) -> None:
        root = tmp_path / "r"
        write(root / "p" / "NOTES.md", "# Alpha\n\nThe ADX block returns di plus and di minus.\n")
        store = SqliteStore(":memory:")
        store.migrate()
        KnowledgeIndexer(store).scan_root(RootConfig(path=root, depth=1))
        assert retriever(store).retrieve("Tune the kubernetes ingress certificate rotation") == []
        store.close()
