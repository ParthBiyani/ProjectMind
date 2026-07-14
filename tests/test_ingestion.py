from __future__ import annotations

import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from projectmind.config import Settings
from projectmind.ingestion.extractors import (
    DecisionExtractor,
    ExtractionContext,
    FailureExtractor,
    extract_all,
    is_noise,
    rejected_alternatives,
    stated_reason,
)
from projectmind.ingestion.git_source import Commit, LocalGitSource, discover_repositories
from projectmind.ingestion.github_source import PullRequest, is_excluded, parse_repo
from projectmind.ingestion.pipeline import IngestionPipeline
from projectmind.models import RecordType, SourceType
from projectmind.storage import SqliteStore, Store

NOW = datetime(2026, 7, 1, tzinfo=UTC)


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


def context() -> ExtractionContext:
    return ExtractionContext(
        project_id=uuid4(), project_key="demo", remote="https://example.invalid/o/demo.git"
    )


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def init_repo(path: Path, commits: list[tuple[str, str]]) -> Path:
    """A real git repository, because parsing real `git log` output is the point."""
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *args: subprocess.run(  # noqa: E731
        ["git", "-C", str(path), *args], capture_output=True, check=True, text=True
    )
    run("init", "-q", "-b", "main")
    run("config", "user.email", "parth@example.invalid")
    run("config", "user.name", "Parth Biyani")
    run("config", "commit.gpgsign", "false")
    for index, (subject, body) in enumerate(commits):
        (path / f"file{index}.py").write_text(f"# {index}\n", encoding="utf-8")
        run("add", "-A")
        message = f"{subject}\n\n{body}" if body else subject
        run("commit", "-q", "-m", message)
    return path


class TestCommitParsing:
    def test_conventional_commits_are_decomposed(self) -> None:
        one = commit("feat(gate): add the trivial prompt skip")
        assert one.commit_type == "feat"
        assert one.conventional == ("feat", "gate", "add the trivial prompt skip")
        assert one.summary == "add the trivial prompt skip"

    def test_a_plain_subject_is_left_alone(self) -> None:
        assert commit("Rework the ingestion loop").commit_type == ""
        assert commit("Rework the ingestion loop").summary == "Rework the ingestion loop"

    def test_reverts_are_recognised_in_both_spellings(self) -> None:
        assert commit('Revert "add the transformer decoder"').is_revert
        assert commit('Revert "add the transformer decoder"').reverted_subject == (
            "add the transformer decoder"
        )
        assert commit("revert: drop the transformer decoder").is_revert

    def test_merge_detection_reads_the_branch_name(self) -> None:
        merged = commit("Merge branch 'feat/relevance-gate' into main", parents=("a", "b"))
        assert merged.is_merge
        assert merged.merged_branch == "feat/relevance-gate"

    def test_pull_request_merges_are_recognised(self) -> None:
        merged = commit("Merge pull request #12 from ParthBiyani/feat/x", parents=("a", "b"))
        assert merged.merged_branch == "ParthBiyani/feat/x"

    def test_tags_are_pulled_out_of_the_ref_list(self) -> None:
        tagged = commit("release", refs=("HEAD -> main", "tag: v0.1.0"))
        assert tagged.tags == ("v0.1.0",)


class TestRealRepository:
    def test_history_is_read_back_in_order(self, tmp_path: Path) -> None:
        repo = init_repo(
            tmp_path / "repo",
            [
                ("feat: add the first thing", ""),
                ("fix: the second thing crashed on startup", "because the config was read late"),
            ],
        )
        source = LocalGitSource(repo, include_files=True)
        assert source.is_repository()
        commits = source.commits()
        assert [c.summary for c in commits] == [
            "the second thing crashed on startup",
            "add the first thing",
        ]
        assert commits[0].body == "because the config was read late"
        assert commits[0].files == ("file1.py",)

    def test_a_body_containing_the_separators_does_not_corrupt_parsing(
        self, tmp_path: Path
    ) -> None:
        """The reason metadata and file names are read in two passes."""
        nasty = "line one\n\nline two with \x1f and \x1e in it\n\nline three"
        repo = init_repo(tmp_path / "repo", [("feat: something", nasty)])
        commits = LocalGitSource(repo).commits()
        assert len(commits) == 1
        assert commits[0].summary == "something"

    def test_author_filtering(self, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo", [("feat: mine", "")])
        assert LocalGitSource(repo, authors=("parth@example.invalid",)).commits()
        assert LocalGitSource(repo, authors=("someone@else.invalid",)).commits() == []

    def test_a_non_repository_yields_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        source = LocalGitSource(tmp_path / "not-a-repo")
        assert source.is_repository() is False
        assert source.commits() == []

    def test_discovery_stops_at_the_repository_boundary(self, tmp_path: Path) -> None:
        init_repo(tmp_path / "a", [("feat: one", "")])
        init_repo(tmp_path / "b", [("feat: two", "")])
        (tmp_path / "a" / "nested").mkdir()
        found = {path.name for path in discover_repositories(tmp_path)}
        assert found == {"a", "b"}


class TestSignals:
    def test_noise_is_filtered(self) -> None:
        for subject in ("wip", "fix typo", "bump deps", "Initial commit", "cleanup", "lint"):
            assert is_noise(commit(subject)), subject

    def test_real_subjects_are_not_noise(self) -> None:
        assert not is_noise(commit("feat: adopt Supabase for auth and storage"))

    def test_a_stated_reason_is_quoted_verbatim(self) -> None:
        found = stated_reason(commit("feat: x", "Chose Supabase because RLS removes client code."))
        assert found is not None and "because RLS removes client code" in found

    def test_a_substantial_body_is_used_even_without_a_marker(self) -> None:
        body = "Row level security removes a whole class of permission code from the client."
        assert stated_reason(commit("feat: x", body)) is not None

    def test_a_trivial_body_is_not_a_reason(self) -> None:
        assert stated_reason(commit("feat: x", "wip")) is None

    def test_alternatives_are_pulled_out(self) -> None:
        found = rejected_alternatives(
            commit("feat: adopt Supabase instead of Firebase, rather than Appwrite")
        )
        assert "Firebase" in " ".join(found)
        assert "Appwrite" in " ".join(found)


class TestDecisionExtractor:
    def test_a_stated_reason_produces_a_high_confidence_record(self) -> None:
        record = DecisionExtractor().extract(
            commit(
                "feat: adopt Supabase for auth",
                "Chose it because row level security removes client-side permission code.",
            ),
            context(),
        )
        assert record is not None
        assert record.type is RecordType.DECISION
        assert record.is_inference is False
        assert record.confidence >= 0.85
        assert "row level security" in record.why

    def test_an_inferred_reason_is_labelled_as_inferred(self) -> None:
        record = DecisionExtractor().extract(
            commit("feat: wire supabase auth into the login screen"), context()
        )
        assert record is not None
        assert record.is_inference is True
        assert record.confidence < 0.85

    def test_a_bare_conventional_commit_produces_nothing(self) -> None:
        """ "feat: add button" names no choice and is not worth remembering."""
        assert DecisionExtractor().extract(commit("feat: add button"), context()) is None

    def test_chores_and_docs_are_skipped_without_a_reason(self) -> None:
        assert DecisionExtractor().extract(commit("chore: tidy the makefile"), context()) is None
        assert (
            DecisionExtractor().extract(commit("docs: expand the readme a bit"), context()) is None
        )

    def test_provenance_points_at_the_commit(self) -> None:
        record = DecisionExtractor().extract(
            commit("feat: adopt supabase", "because it is simpler"), context()
        )
        assert record is not None
        assert record.source_url.endswith("/commit/" + "a" * 40)
        assert record.source_type is SourceType.COMMIT


class TestFailureExtractor:
    def test_a_revert_always_produces_a_reversal(self) -> None:
        record = FailureExtractor().extract(
            commit('Revert "adopt the transformer decoder"'), context()
        )
        assert record is not None
        assert record.type is RecordType.REVERSAL
        assert record.source_type is SourceType.REVERT
        assert record.confidence >= 0.85

    def test_a_described_fix_produces_a_failure(self) -> None:
        record = FailureExtractor().extract(
            commit(
                "fix: validation crashed with CUDA out of memory",
                "The validation image size was larger than training.",
            ),
            context(),
        )
        assert record is not None
        assert record.type is RecordType.FAILURE
        assert "cuda" in record.entities

    def test_a_terse_fix_with_no_symptom_is_skipped(self) -> None:
        assert FailureExtractor().extract(commit("fix: it"), context()) is None

    def test_a_feature_commit_is_never_a_failure(self) -> None:
        """A `feat` commit mentioning "regression harness" is not an outage."""
        record = FailureExtractor().extract(
            commit("feat(eval): add a regression harness with a baseline diff"), context()
        )
        assert record is None

    def test_a_regression_test_is_not_a_regression(self) -> None:
        assert (
            FailureExtractor().extract(
                commit("chore: add regression tests for the parser"), context()
            )
            is None
        )


class TestExtractAll:
    def test_one_commit_produces_at_most_one_record(self) -> None:
        records = extract_all([commit("fix: the parser crashed on empty input")], context())
        assert len(records) == 1

    def test_failures_win_over_decisions_on_the_same_commit(self) -> None:
        records = extract_all(
            [commit("fix: supabase auth crashed on cold start", "because the session was late")],
            context(),
        )
        assert records[0].type is RecordType.FAILURE


class TestGitHubHelpers:
    def test_remote_parsing(self) -> None:
        assert parse_repo("https://github.com/o/n.git") == ("o", "n")
        assert parse_repo("git@github.com:o/n.git") == ("o", "n")
        assert parse_repo("https://gitlab.com/o/n") is None
        assert parse_repo("fixture://local/x") is None

    def test_exclusion_matches_bare_names_and_globs(self) -> None:
        assert is_excluded("owner/client-secret", ["client-*"])
        assert is_excluded("client-secret", ["client-*"])
        assert not is_excluded("owner/projectmind", ["client-*"])

    def test_a_pull_request_adapts_to_a_merge_commit(self) -> None:
        pull = PullRequest(
            number=12,
            title="Adopt Supabase",
            body="Because row level security removes client-side permission code.",
            merged_at=NOW,
            html_url="https://github.com/o/n/pull/12",
            head_ref="feat/supabase",
            base_ref="main",
            author="ParthBiyani",
        )
        adapted = pull.as_commit()
        assert adapted.is_merge
        assert adapted.merged_branch == "feat/supabase"
        record = DecisionExtractor().extract(adapted, context())
        assert record is not None
        assert record.is_inference is False


class TestPipeline:
    def test_ingestion_is_idempotent(self, store: Store, tmp_path: Path) -> None:
        """The property that makes re-running safe."""
        repo = init_repo(
            tmp_path / "repo",
            [
                ("feat: adopt supabase for auth", "because row level security removes code"),
                ("fix: the login screen crashed on cold start", "the session restored too late"),
            ],
        )
        pipeline = IngestionPipeline(store, Settings(home=tmp_path))
        first = pipeline.ingest_repository(repo)
        assert first.records_stored >= 2
        second = pipeline.ingest_repository(repo)
        assert second.records_extracted == first.records_extracted
        assert second.records_stored == 0
        assert store.count_records() == first.records_stored

    def test_records_carry_embeddings(self, store: Store, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "repo", [("feat: adopt supabase", "because it is simpler")])
        IngestionPipeline(store, Settings(home=tmp_path)).ingest_repository(repo)
        record = store.list_records()[0]
        stored = store.get_record(record.id)
        assert stored is not None and stored.embedding

    def test_the_project_is_registered_with_a_fingerprint(
        self, store: Store, tmp_path: Path
    ) -> None:
        repo = init_repo(tmp_path / "repo", [("feat: adopt supabase", "because it is simpler")])
        (repo / "pyproject.toml").write_text(
            '[project]\ndependencies = ["fastapi"]\n', encoding="utf-8"
        )
        IngestionPipeline(store, Settings(home=tmp_path)).ingest_repository(repo)
        projects = store.list_projects()
        assert len(projects) == 1
        assert "fastapi" in projects[0].fingerprint.dependencies

    def test_an_excluded_repository_is_never_read(self, store: Store, tmp_path: Path) -> None:
        repo = init_repo(tmp_path / "secret-client", [("feat: adopt supabase", "because")])
        settings = Settings(home=tmp_path, excluded_repos=("secret-*",))
        result = IngestionPipeline(store, settings).ingest_repository(repo)
        assert result.skipped_reason == "excluded by configuration"
        assert result.commits_read == 0
        assert store.count_records() == 0

    def test_a_directory_with_no_repositories_reports_nothing(
        self, store: Store, tmp_path: Path
    ) -> None:
        report = IngestionPipeline(store, Settings(home=tmp_path)).ingest_tree(tmp_path)
        assert report.records_stored == 0
        assert report.commits_read == 0

    def test_a_tree_of_repositories_is_ingested_together(
        self, store: Store, tmp_path: Path
    ) -> None:
        init_repo(tmp_path / "one", [("feat: adopt supabase here", "because it is simpler")])
        init_repo(tmp_path / "two", [("fix: the worker crashed on startup", "config read late")])
        report = IngestionPipeline(store, Settings(home=tmp_path)).ingest_tree(tmp_path)
        assert len(report.repositories) == 2
        assert report.records_stored >= 2
        assert len(store.list_projects()) == 2
