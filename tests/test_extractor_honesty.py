"""Records must say only what the commit said.

These pin the behaviour that was wrong in v0.1: invented reasons, trailers read
as rationale, duplicated entities, and substring matches on file paths.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from projectmind.ingestion.extractors import (
    DecisionExtractor,
    ExtractionContext,
    FailureExtractor,
    clean_body,
    extract_entities,
    stated_reason,
)
from projectmind.ingestion.git_source import Commit
from projectmind.models import RecordType

NOW = datetime(2026, 7, 1, tzinfo=UTC)
TRAILER = "Co-Authored-By: Someone Else <someone@example.invalid>"


def commit(subject: str, body: str = "", **overrides: object) -> Commit:
    payload: dict[str, object] = {
        "sha": "a" * 40,
        "parents": ("b" * 40,),
        "author_name": "Parth Biyani",
        "author_email": "parth@example.invalid",
        "authored_at": NOW,
        "refs": (),
        "subject": subject,
        "body": body,
        "files": (),
    }
    payload.update(overrides)
    return Commit(**payload)  # type: ignore[arg-type]


def context(vocabulary: frozenset[str] | None = None) -> ExtractionContext:
    base = ExtractionContext(project_id=uuid4(), project_key="demo", remote=None)
    if vocabulary is None:
        return base
    return ExtractionContext(
        project_id=base.project_id, project_key="demo", remote=None, vocabulary=vocabulary
    )


class TestTrailersAreNotReasons:
    def test_a_trailer_only_body_has_no_reason(self) -> None:
        assert stated_reason(commit("feat: add a thing here", TRAILER)) is None

    def test_trailers_are_stripped_before_a_reason_is_looked_for(self) -> None:
        body = f"Chose it because the old path broke on windows paths.\n\n{TRAILER}\n"
        assert stated_reason(commit("feat: use pathlib", body)) == (
            "Chose it because the old path broke on windows paths."
        )

    def test_bare_links_and_generated_footers_are_not_prose(self) -> None:
        body = (
            "https://example.invalid/issues/12\n\nGenerated with a tool\nSigned-off-by: A <a@b.c>"
        )
        assert clean_body(body) == ""

    def test_a_commit_with_only_a_trailer_does_not_become_a_high_confidence_decision(self) -> None:
        record = DecisionExtractor().extract(
            commit("docs: update the readme text", TRAILER), context()
        )
        assert record is None


class TestNoInventedReasons:
    def test_an_inferred_decision_has_no_why(self) -> None:
        record = DecisionExtractor().extract(
            commit("feat: wire supabase auth into the login screen"), context()
        )
        assert record is not None
        assert record.is_inference is True
        assert record.why == ""

    def test_a_described_fix_without_a_stated_reason_has_no_why(self) -> None:
        record = FailureExtractor().extract(
            commit("fix: loosen the gate integrity check and skip empty peers silently"), context()
        )
        assert record is not None
        assert record.type is RecordType.FAILURE
        assert record.why == ""
        assert record.is_inference is True

    def test_a_revert_without_words_of_its_own_has_no_why(self) -> None:
        record = FailureExtractor().extract(
            commit('Revert "feat: add the oscillator bars"'), context()
        )
        assert record is not None
        assert record.type is RecordType.REVERSAL
        assert record.why == ""

    def test_a_stated_reason_is_still_kept_verbatim(self) -> None:
        record = DecisionExtractor().extract(
            commit("feat: adopt walk-forward", "Because k-fold leaked the future into training."),
            context(),
        )
        assert record is not None
        assert record.why == "Because k-fold leaked the future into training."
        assert record.is_inference is False


class TestEntities:
    VOCAB = frozenset({"llm", "openai", "bloc", "flutter"})

    def test_aliases_collapsing_to_one_entity_are_listed_once(self) -> None:
        entities = extract_entities(
            commit("feat: add the openai client for the llm layer"), self.VOCAB
        )
        assert entities.count("llm") == 1
        assert len(entities) == len(set(entities))

    def test_a_path_component_must_match_the_whole_word(self) -> None:
        entities = extract_entities(
            commit("feat: add bar blocks", files=("lib/blocks/oscillator.dart",)), self.VOCAB
        )
        assert "bloc" not in entities

    def test_a_real_component_still_counts(self) -> None:
        entities = extract_entities(
            commit("feat: add the screen state", files=("lib/bloc/screen_bloc.dart",)), self.VOCAB
        )
        assert "bloc" in entities

    def test_the_file_stem_counts(self) -> None:
        entities = extract_entities(
            commit("feat: tidy things", files=("src/flutter.md",)), self.VOCAB
        )
        assert "flutter" in entities


class TestMergeLabels:
    def test_a_labelled_merge_is_recognised_as_a_unit_of_work(self) -> None:
        merged = commit(
            "merge: phase 5 reflection loop and monthly review into main",
            parents=("b" * 40, "c" * 40),
        )
        assert merged.merged_branch == "phase 5 reflection loop and monthly review into main"

    def test_a_labelled_merge_becomes_a_record_without_a_made_up_reason(self) -> None:
        merged = commit(
            "merge: phase 5 reflection loop and monthly review into main",
            parents=("b" * 40, "c" * 40),
        )
        record = DecisionExtractor().extract(merged, context())
        assert record is not None
        assert record.why == ""
        assert record.is_inference is True

    def test_a_single_parent_commit_with_that_prefix_is_not_a_merge(self) -> None:
        assert commit("merge: not really a merge at all").merged_branch is None

    def test_git_merge_subjects_still_work(self) -> None:
        merged = commit("Merge branch 'feat/auth' into main", parents=("b" * 40, "c" * 40))
        assert merged.merged_branch == "feat/auth"
