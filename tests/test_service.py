from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from projectmind import entities as entity_matching
from projectmind.config import Settings
from projectmind.models import Category, ProfileStatement, StatementStatus
from projectmind.profile.seed import seed_profile
from projectmind.service import MemoryService, hash_prompt, project_key, slugify
from projectmind.storage import SqliteStore
from projectmind.tokens import estimate_tokens, total_tokens


@pytest.fixture
def memory(tmp_path: Path) -> Iterator[MemoryService]:
    store = SqliteStore(tmp_path / "service.db")
    store.migrate()
    service = MemoryService(store, Settings(home=str(tmp_path)))
    yield service
    service.close()


@pytest.fixture
def seeded(memory: MemoryService) -> MemoryService:
    seed_profile(memory.profile, activate=True)
    return memory


class TestTokenEstimates:
    def test_empty_text_costs_nothing(self) -> None:
        assert estimate_tokens("") == 0

    def test_estimates_grow_with_length(self) -> None:
        short = estimate_tokens("Prefers Supabase")
        long = estimate_tokens("Prefers Supabase " * 20)
        assert 0 < short < long

    def test_the_estimate_does_not_undercount_prose(self) -> None:
        """A cap that undercounts is not a cap."""
        text = "Uses Riverpod with code generation for Flutter state, not Provider or Bloc."
        assert estimate_tokens(text) >= len(text.split())

    def test_totals_add_up(self) -> None:
        parts = ["one two three", "four five six"]
        assert total_tokens(parts) == sum(estimate_tokens(p) for p in parts)


class TestEntityExtraction:
    def test_only_known_entities_match(self) -> None:
        found = entity_matching.extract(
            "Should I use Supabase or something else here?", ["supabase", "firebase"]
        )
        assert found == ("supabase",)

    def test_aliases_resolve_to_the_canonical_name(self) -> None:
        assert entity_matching.extract("switch to postgresql", ["postgres"]) == ("postgres",)
        assert entity_matching.extract("the sklearn pipeline", ["scikit-learn"]) == (
            "scikit-learn",
        )

    def test_two_word_entities_are_found(self) -> None:
        assert entity_matching.extract("the go router redirect", ["go-router"]) == ("go-router",)

    def test_an_empty_vocabulary_matches_nothing(self) -> None:
        assert entity_matching.extract("supabase firebase riverpod", []) == ()

    def test_unknown_terms_are_never_invented(self) -> None:
        assert entity_matching.extract("improve the performance of authentication", []) == ()

    def test_results_are_sorted_and_deduplicated(self) -> None:
        found = entity_matching.extract("flutter flutter riverpod", ["flutter", "riverpod"])
        assert found == ("flutter", "riverpod")


class TestProjectIdentity:
    def test_the_git_remote_wins_over_the_directory_name(self) -> None:
        key = project_key(Path("/tmp/whatever"), "https://github.com/ParthBiyani/ProjectMind.git")
        assert key == "parthbiyani-projectmind"

    def test_the_same_repo_in_two_directories_is_one_project(self) -> None:
        remote = "https://github.com/a/b.git"
        assert project_key(Path("/one"), remote) == project_key(Path("/two"), remote)

    def test_a_directory_without_a_remote_falls_back_to_its_name(self) -> None:
        assert project_key(Path("/d/Project Eris/Coffee Leaf Spray"), None) == ("coffee-leaf-spray")

    def test_slugify_collapses_punctuation(self) -> None:
        assert slugify("Pill Counting -- Old!") == "pill-counting-old"
        assert slugify("???") == "unknown"

    def test_resolving_a_project_records_it(self, memory: MemoryService, tmp_path: Path) -> None:
        project_dir = tmp_path / "SomeProject"
        project_dir.mkdir()
        resolved = memory.resolve_project(project_dir)
        assert resolved.key == "someproject"
        assert memory.store.get_project("someproject") is not None

    def test_no_path_means_an_unknown_project(self, memory: MemoryService) -> None:
        assert memory.resolve_project(None).key == "unknown"


class TestPromptHashing:
    def test_the_prompt_itself_is_never_part_of_the_hash_output(self) -> None:
        digest = hash_prompt("rebuild the auth flow for acme corp")
        assert "acme" not in digest
        assert len(digest) == 16

    def test_hashing_is_stable_and_case_insensitive(self) -> None:
        assert hash_prompt("Fix The Bug") == hash_prompt("fix the bug  ")


class TestGetContext:
    def test_an_empty_profile_serves_nothing_and_says_why(self, memory: MemoryService) -> None:
        bundle = memory.get_context("Add Supabase auth", Path.cwd())
        assert bundle.is_empty
        assert bundle.gate_reason == "no active profile statements"
        assert bundle.render() == ""

    def test_proposed_statements_are_never_served(self, memory: MemoryService) -> None:
        seed_profile(memory.profile, activate=False)
        assert memory.get_context("Add Supabase auth", Path.cwd()).is_empty

    def test_a_seeded_profile_is_served_within_budget(self, seeded: MemoryService) -> None:
        bundle = seeded.get_context("Add Supabase auth to this Flutter screen", Path.cwd())
        assert not bundle.is_empty
        assert bundle.profile_tokens <= 800
        assert bundle.episodic == ()
        assert "## ProjectMind context" in bundle.render()

    def test_prompt_entities_are_reported_on_the_bundle(self, seeded: MemoryService) -> None:
        bundle = seeded.get_context("Add Supabase auth to this Flutter screen", Path.cwd())
        assert "supabase" in bundle.entities
        assert "flutter" in bundle.entities

    def test_ignore_profile_is_an_escape_hatch_that_actually_escapes(
        self, seeded: MemoryService
    ) -> None:
        """Deliberate exploration has to be able to run clean."""
        bundle = seeded.get_context("Try something new", Path.cwd(), ignore_profile=True)
        assert bundle.is_empty
        assert bundle.gate_reason == "profile suppressed by the caller"

    def test_max_tokens_narrows_the_budget_but_cannot_widen_it(self, seeded: MemoryService) -> None:
        narrow = seeded.get_context("Add Supabase auth", Path.cwd(), max_tokens=80)
        assert narrow.profile_tokens <= 80
        wide = seeded.get_context("Add Supabase auth", Path.cwd(), max_tokens=99_999)
        assert wide.profile_tokens <= 800

    def test_every_call_is_logged_with_a_hashed_prompt(self, seeded: MemoryService) -> None:
        seeded.get_context("Add Supabase auth to this Flutter screen", Path.cwd())
        logged = seeded.store.recent_bundles()
        assert len(logged) == 1
        assert logged[0].injected is True
        assert logged[0].prompt_hash and "supabase" not in logged[0].prompt_hash
        assert logged[0].prompt_words == 7

    def test_a_skipped_bundle_is_logged_too(self, memory: MemoryService) -> None:
        """ "Did not inject" has to be measurable, so it has to be recorded."""
        memory.get_context("anything", Path.cwd())
        logged = memory.store.recent_bundles()
        assert len(logged) == 1
        assert logged[0].injected is False

    def test_logging_can_be_suppressed(self, seeded: MemoryService) -> None:
        seeded.get_context("Add Supabase auth", Path.cwd(), log_bundle=False)
        assert seeded.store.recent_bundles() == []

    def test_latency_is_always_recorded(self, seeded: MemoryService) -> None:
        assert seeded.get_context("Add Supabase auth", Path.cwd()).latency_ms > 0


class TestFeedbackAndStats:
    def test_feedback_moves_the_observed_false_injection_rate(self, seeded: MemoryService) -> None:
        good = seeded.get_context("Add Supabase auth", Path.cwd())
        bad = seeded.get_context("Something unrelated entirely", Path.cwd())
        seeded.feedback(good.bundle_id, useful=True)
        seeded.feedback(bad.bundle_id, useful=False, note="wrong stack")
        stats = seeded.stats()
        assert stats.feedback_positive == 1
        assert stats.feedback_negative == 1
        assert stats.observed_false_injection_rate == pytest.approx(0.5)

    def test_feedback_on_an_unknown_bundle_is_reported_as_a_miss(
        self, seeded: MemoryService
    ) -> None:
        from uuid import uuid4

        assert seeded.feedback(uuid4(), useful=True) is False

    def test_stats_are_empty_rather_than_broken_on_a_fresh_install(
        self, memory: MemoryService
    ) -> None:
        stats = memory.stats()
        assert stats.bundles == 0
        assert stats.injection_rate == 0.0


class TestRelevanceEndToEnd:
    def test_an_ml_prompt_does_not_surface_flutter_preferences(self, memory: MemoryService) -> None:
        memory.profile.add(
            ProfileStatement(
                statement="Uses Riverpod with code generation for Flutter state",
                category=Category.TOOL_PREFERENCE,
                confidence=0.9,
                status=StatementStatus.ACTIVE,
                entities=["riverpod", "flutter"],
            )
        )
        memory.profile.add(
            ProfileStatement(
                statement="Wraps preprocessing in a scikit-learn Pipeline",
                category=Category.TOOL_PREFERENCE,
                confidence=0.9,
                status=StatementStatus.ACTIVE,
                entities=["scikit-learn"],
            )
        )
        bundle = memory.get_context("My sklearn pipeline is leaking", Path.cwd())
        assert "scikit-learn Pipeline" in bundle.render()
        served = [item.statement for item in bundle.profile]
        assert served[0].startswith("Wraps preprocessing")
