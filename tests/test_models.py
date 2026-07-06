from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from projectmind.models import (
    DEFAULT_TTL_DAYS,
    Category,
    ContextBundle,
    EpisodicRecord,
    Fingerprint,
    ProfileStatement,
    RecordType,
    ServedRecord,
    ServedStatement,
    SourceType,
    StatementStatus,
)

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def statement(**overrides: object) -> ProfileStatement:
    defaults: dict[str, object] = {
        "statement": "Prefers Supabase",
        "category": Category.TOOL_PREFERENCE,
        "confidence": 1.0,
        "status": StatementStatus.ACTIVE,
        "last_confirmed_at": NOW,
    }
    defaults.update(overrides)
    return ProfileStatement.model_validate(defaults)


class TestFingerprintSimilarity:
    def test_identical_fingerprints_score_one(self) -> None:
        one = Fingerprint(languages=["dart"], frameworks=["flutter"], dependencies=["riverpod"])
        assert one.similarity(one) == pytest.approx(1.0)

    def test_disjoint_fingerprints_score_zero(self) -> None:
        one = Fingerprint(languages=["dart"], dependencies=["riverpod"])
        two = Fingerprint(languages=["rust"], dependencies=["tokio"])
        assert one.similarity(two) == 0.0

    def test_dependency_overlap_dominates_language_overlap(self) -> None:
        """Two Python repos sharing nothing should rank below two sharing a stack."""
        base = Fingerprint(languages=["python"], dependencies=["torch", "timm", "albumentations"])
        same_deps = Fingerprint(
            languages=["python"], dependencies=["torch", "timm", "albumentations"]
        )
        same_language_only = Fingerprint(languages=["python"], dependencies=["fastapi", "redis"])
        assert base.similarity(same_deps) > base.similarity(same_language_only)

    def test_empty_components_are_dropped_rather_than_counted_as_misses(self) -> None:
        """A project that declares no frameworks is not punished for it."""
        with_frameworks = Fingerprint(languages=["python"], dependencies=["torch"])
        also_none = Fingerprint(languages=["python"], dependencies=["torch"])
        assert with_frameworks.similarity(also_none) == pytest.approx(1.0)

    def test_similarity_is_symmetric(self) -> None:
        one = Fingerprint(languages=["dart"], dependencies=["riverpod", "go_router"])
        two = Fingerprint(languages=["dart"], dependencies=["riverpod"])
        assert one.similarity(two) == pytest.approx(two.similarity(one))

    def test_empty_fingerprints_do_not_divide_by_zero(self) -> None:
        assert Fingerprint().similarity(Fingerprint()) == 0.0
        assert Fingerprint().is_empty is True

    def test_shares_any_matches_across_all_three_sets(self) -> None:
        fingerprint = Fingerprint(
            languages=["dart"], frameworks=["flutter"], dependencies=["supabase_flutter"]
        )
        assert fingerprint.shares_any(frozenset({"flutter"})) is True
        assert fingerprint.shares_any(frozenset({"supabase_flutter"})) is True
        assert fingerprint.shares_any(frozenset({"tensorflow"})) is False

    def test_inputs_are_lowercased_and_deduplicated(self) -> None:
        fingerprint = Fingerprint(languages=["Python", "python", " PYTHON "])
        assert fingerprint.languages == ("python",)


class TestProfileLifecycle:
    def test_ttl_defaults_come_from_the_category(self) -> None:
        for category, days in DEFAULT_TTL_DAYS.items():
            assert statement(category=category).ttl_days == days

    def test_an_explicit_ttl_is_respected(self) -> None:
        assert statement(ttl_days=30).ttl_days == 30

    def test_confidence_is_untouched_inside_the_ttl_window(self) -> None:
        fresh = statement()
        assert fresh.effective_confidence(NOW + timedelta(days=89)) == pytest.approx(1.0)
        assert fresh.is_due_for_reconfirmation(NOW + timedelta(days=89)) is False

    def test_one_ttl_past_due_decays_but_still_serves(self) -> None:
        stale = statement()
        at = NOW + timedelta(days=95)
        assert stale.effective_confidence(at) == pytest.approx(0.8)
        assert stale.is_due_for_reconfirmation(at) is True
        assert stale.is_servable(at) is True

    def test_two_ttls_past_due_decays_further(self) -> None:
        at = NOW + timedelta(days=185)
        assert statement().effective_confidence(at) == pytest.approx(0.5)
        assert statement().is_servable(at) is True

    def test_three_ttls_past_due_goes_dormant_and_stops_being_served(self) -> None:
        at = NOW + timedelta(days=275)
        stale = statement()
        assert stale.should_go_dormant(at) is True
        assert stale.effective_confidence(at) == 0.0
        assert stale.is_servable(at) is False

    def test_only_active_statements_are_servable(self) -> None:
        for status in (
            StatementStatus.PROPOSED,
            StatementStatus.SUPERSEDED,
            StatementStatus.DORMANT,
            StatementStatus.REJECTED,
        ):
            assert statement(status=status).is_servable(NOW) is False

    def test_refreshing_the_confirmation_restores_full_confidence(self) -> None:
        stale = statement()
        at = NOW + timedelta(days=200)
        assert stale.effective_confidence(at) == pytest.approx(0.5)
        refreshed = stale.model_copy(update={"last_confirmed_at": at})
        assert refreshed.effective_confidence(at) == pytest.approx(1.0)

    def test_scope_label_is_rendered_for_keep_both_statements(self) -> None:
        scoped = statement(scope={"when": "prototypes"})
        assert scoped.scope_label() == "when: prototypes"
        assert statement().scope_label() == ""


class TestEpisodicRecord:
    def _record(self, **overrides: object) -> EpisodicRecord:
        defaults: dict[str, object] = {
            "project_id": uuid4(),
            "type": RecordType.DECISION,
            "what": "Chose ONNX",
            "source_type": SourceType.COMMIT,
            "source_url": "fixture://a",
            "occurred_at": NOW,
            "confidence": 0.8,
            "is_inference": False,
        }
        defaults.update(overrides)
        return EpisodicRecord.model_validate(defaults)

    def test_content_hash_is_stable_and_identity_bearing(self) -> None:
        project = uuid4()
        one = self._record(project_id=project)
        two = self._record(project_id=project)
        assert one.content_hash == two.content_hash
        assert one.content_hash != self._record(project_id=project, what="Other").content_hash

    def test_recency_weight_halves_at_the_half_life(self) -> None:
        record = self._record()
        at = NOW + timedelta(days=240)
        assert record.recency_weight(at, half_life_days=240) == pytest.approx(0.5)
        assert record.recency_weight(NOW, half_life_days=240) == pytest.approx(1.0)

    def test_future_records_are_not_boosted_above_one(self) -> None:
        record = self._record(occurred_at=NOW + timedelta(days=30))
        assert record.recency_weight(NOW) == pytest.approx(1.0)

    def test_searchable_text_covers_every_free_text_field(self) -> None:
        record = self._record(
            why="no GPU on target", outcome="120ms per frame", rejected_alternatives=["torchscript"]
        )
        text = record.searchable_text
        assert "Chose ONNX" in text
        assert "no GPU on target" in text
        assert "120ms per frame" in text
        assert "torchscript" in text

    def test_entities_are_normalised(self) -> None:
        assert self._record(entities=["ONNX", " onnx ", "CPU"]).entities == ("cpu", "onnx")


class TestContextBundle:
    def _bundle(self) -> ContextBundle:
        return ContextBundle(
            project_key="sprout",
            profile=(
                ServedStatement(
                    id=uuid4(),
                    statement="Prefers Supabase for backend-as-a-service",
                    category=Category.TOOL_PREFERENCE,
                    confidence=0.9,
                ),
            ),
            episodic=(
                ServedRecord(
                    id=uuid4(),
                    project_key="spendwise",
                    type=RecordType.DECISION,
                    what="Adopted Supabase instead of Firebase",
                    why="row level security removed client-side permission code",
                    outcome="shipped",
                    occurred_at=datetime(2026, 2, 11, tzinfo=UTC),
                    source_url="fixture://spendwise/commit/4c1b9ae",
                    confidence=0.91,
                    is_inference=False,
                    cross_project=True,
                ),
            ),
            profile_tokens=40,
            episodic_tokens=120,
        )

    def test_empty_bundle_renders_to_nothing_at_all(self) -> None:
        empty = ContextBundle(gate_reason="trivial prompt")
        assert empty.is_empty is True
        assert empty.render() == ""

    def test_render_includes_provenance_for_every_record(self) -> None:
        rendered = self._bundle().render()
        assert "fixture://spendwise/commit/4c1b9ae" in rendered
        assert "spendwise" in rendered
        assert "## ProjectMind context" in rendered

    def test_inferred_reasons_are_labelled_as_inferred(self) -> None:
        bundle = self._bundle()
        inferred = bundle.episodic[0].model_copy(update={"is_inference": True})
        assert "inferred reason" in bundle.model_copy(update={"episodic": (inferred,)}).render()
        assert "inferred reason" not in bundle.render()

    def test_totals_and_counts(self) -> None:
        bundle = self._bundle()
        assert bundle.total_tokens == 160
        assert bundle.cross_project_count == 1
