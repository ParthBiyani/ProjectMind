from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from projectmind.models import (
    Category,
    EpisodicRecord,
    EvidenceRef,
    Origin,
    ProfileStatement,
    RecordType,
    SourceType,
    StatementStatus,
)
from projectmind.profile import lifecycle

NOW = datetime(2026, 7, 1, tzinfo=UTC)


def statement(**overrides: object) -> ProfileStatement:
    defaults: dict[str, object] = {
        "statement": "Prefers Firebase for backend-as-a-service on mobile projects",
        "category": Category.TOOL_PREFERENCE,
        "confidence": 0.9,
        "status": StatementStatus.ACTIVE,
        "entities": ["firebase"],
        "last_confirmed_at": NOW,
        "created_at": NOW,
    }
    defaults.update(overrides)
    return ProfileStatement.model_validate(defaults)


def record(**overrides: object) -> EpisodicRecord:
    defaults: dict[str, object] = {
        "project_id": uuid4(),
        "type": RecordType.DECISION,
        "what": "Chose Supabase",
        "source_type": SourceType.COMMIT,
        "source_url": "fixture://a",
        "occurred_at": NOW - timedelta(days=10),
        "confidence": 0.9,
        "is_inference": False,
    }
    defaults.update(overrides)
    return EpisodicRecord.model_validate(defaults)


class TestActivation:
    def test_activation_requires_clearing_the_confidence_bar(self) -> None:
        with pytest.raises(lifecycle.TransitionError, match="below the activation bar"):
            lifecycle.activate(statement(confidence=0.7, status=StatementStatus.PROPOSED))

    def test_activation_sets_status_and_stamps_the_clock(self) -> None:
        proposed = statement(status=StatementStatus.PROPOSED, last_confirmed_at=NOW)
        active = lifecycle.activate(proposed, now=NOW + timedelta(days=5))
        assert active.status is StatementStatus.ACTIVE
        assert active.last_confirmed_at == NOW + timedelta(days=5)

    def test_a_superseded_statement_cannot_be_reactivated(self) -> None:
        with pytest.raises(lifecycle.TransitionError, match="cannot be reactivated"):
            lifecycle.activate(statement(status=StatementStatus.SUPERSEDED))


class TestSupersession:
    def test_supersede_links_both_directions_and_keeps_the_old_statement(self) -> None:
        old = statement()
        new = statement(
            statement="Prefers Supabase; moved off Firebase over pricing at scale",
            entities=["supabase"],
            status=StatementStatus.PROPOSED,
        )
        retired, replacement = lifecycle.supersede(old, new, now=NOW)
        assert retired.status is StatementStatus.SUPERSEDED
        assert retired.superseded_by == new.id
        assert replacement.status is StatementStatus.ACTIVE
        assert replacement.supersedes == old.id
        assert retired.statement == old.statement, "history must survive supersession"

    def test_supersession_clears_the_replacements_contradiction_state(self) -> None:
        old = statement()
        new = statement(
            statement="Prefers Supabase for backend-as-a-service",
            contradiction_count=4,
            contradiction_refs=["a", "b"],
            status=StatementStatus.PROPOSED,
        )
        _, replacement = lifecycle.supersede(old, new, now=NOW)
        assert replacement.contradiction_count == 0
        assert replacement.contradiction_refs == ()

    def test_a_statement_cannot_supersede_itself(self) -> None:
        one = statement()
        with pytest.raises(lifecycle.TransitionError, match="cannot supersede itself"):
            lifecycle.supersede(one, one)

    def test_double_supersession_is_refused(self) -> None:
        old = statement(status=StatementStatus.SUPERSEDED)
        with pytest.raises(lifecycle.TransitionError, match="already superseded"):
            lifecycle.supersede(old, statement(statement="Something else entirely here"))

    def test_keep_both_requires_a_scope_on_each_side(self) -> None:
        one, two = statement(), statement(statement="Prefers Supabase for shipping projects")
        with pytest.raises(lifecycle.TransitionError, match="scope qualifier"):
            lifecycle.keep_both(one, two, first_scope={}, second_scope={"when": "shipping"})

    def test_keep_both_leaves_both_active_and_distinguishable(self) -> None:
        one, two = statement(), statement(statement="Prefers Supabase for shipping projects")
        first, second = lifecycle.keep_both(
            one,
            two,
            first_scope={"when": "throwaway prototypes"},
            second_scope={"when": "anything shipping"},
            now=NOW,
        )
        assert first.status is second.status is StatementStatus.ACTIVE
        assert first.scope_label() != second.scope_label()

    def test_rejecting_a_proposal_refreshes_the_clock_and_clears_the_count(self) -> None:
        contested = statement(contradiction_count=3, contradiction_refs=["a", "b", "c"])
        cleared = lifecycle.reject_contradiction(contested, now=NOW + timedelta(days=30))
        assert cleared.contradiction_count == 0
        assert cleared.contradiction_refs == ()
        assert cleared.last_confirmed_at == NOW + timedelta(days=30)
        assert cleared.status is StatementStatus.ACTIVE


class TestContradictionThreshold:
    def test_one_contradiction_is_not_enough(self) -> None:
        verdict = lifecycle.evaluate_contradictions(statement(), [record()], now=NOW)
        assert bool(verdict) is False
        assert "1 of 3" in verdict.reason

    def test_two_contradictions_are_not_enough(self) -> None:
        verdict = lifecycle.evaluate_contradictions(
            statement(), [record(), record(what="Also Supabase")], now=NOW
        )
        assert bool(verdict) is False

    def test_three_inside_the_window_opens_a_proposal(self) -> None:
        records = [record(what=f"Chose Supabase {i}") for i in range(3)]
        verdict = lifecycle.evaluate_contradictions(statement(), records, now=NOW)
        assert bool(verdict) is True
        assert len(verdict.supporting) == 3

    def test_three_spread_beyond_the_window_do_not(self) -> None:
        """One experimental branch a year ago should not overturn a preference."""
        records = [
            record(what="a", occurred_at=NOW - timedelta(days=400)),
            record(what="b", occurred_at=NOW - timedelta(days=300)),
            record(what="c", occurred_at=NOW - timedelta(days=10)),
        ]
        verdict = lifecycle.evaluate_contradictions(statement(), records, now=NOW)
        assert bool(verdict) is False

    def test_a_single_explicit_reversal_is_enough(self) -> None:
        verdict = lifecycle.evaluate_contradictions(
            statement(), [record(type=RecordType.REVERSAL, what="Migrated off Firebase")], now=NOW
        )
        assert bool(verdict) is True
        assert verdict.reason == "an explicit reversal was recorded"

    def test_inactive_statements_are_never_proposed_against(self) -> None:
        records = [record(what=f"x{i}") for i in range(5)]
        verdict = lifecycle.evaluate_contradictions(
            statement(status=StatementStatus.DORMANT), records, now=NOW
        )
        assert bool(verdict) is False

    def test_noting_the_same_record_twice_does_not_double_count(self) -> None:
        one = record()
        noted = lifecycle.note_contradiction(statement(), one)
        assert noted.contradiction_count == 1
        assert lifecycle.note_contradiction(noted, one).contradiction_count == 1


class TestReconfirmationAndDecay:
    def test_passive_confirmation_attaches_the_evidence_that_caused_it(self) -> None:
        source = record(what="Used Firebase again on a new prototype")
        confirmed = lifecycle.confirm_from_record(statement(), source)
        assert confirmed.last_confirmed_at == source.occurred_at
        assert confirmed.evidence_refs[-1].url == source.source_url

    def test_confirming_the_same_evidence_twice_does_not_duplicate_it(self) -> None:
        evidence = EvidenceRef(kind=SourceType.COMMIT, url="fixture://a", label="a")
        once = lifecycle.confirm(statement(), evidence=evidence, now=NOW)
        twice = lifecycle.confirm(once, evidence=evidence, now=NOW)
        assert len(twice.evidence_refs) == len(once.evidence_refs)

    def test_confirming_a_dormant_statement_brings_it_back(self) -> None:
        revived = lifecycle.confirm(statement(status=StatementStatus.DORMANT), now=NOW)
        assert revived.status is StatementStatus.ACTIVE

    def test_decay_is_a_no_op_inside_the_window(self) -> None:
        outcome = lifecycle.apply_decay(statement(), now=NOW + timedelta(days=80))
        assert outcome.ttls_elapsed == 0
        assert outcome.went_dormant is False
        assert outcome.needs_review is False
        assert outcome.effective_confidence == pytest.approx(0.9)

    def test_one_ttl_over_flags_review_but_keeps_serving(self) -> None:
        outcome = lifecycle.apply_decay(statement(), now=NOW + timedelta(days=100))
        assert outcome.needs_review is True
        assert outcome.went_dormant is False
        assert outcome.effective_confidence == pytest.approx(0.9 * 0.8)

    def test_three_ttls_over_goes_dormant(self) -> None:
        outcome = lifecycle.apply_decay(statement(), now=NOW + timedelta(days=280))
        assert outcome.went_dormant is True
        assert outcome.statement.status is StatementStatus.DORMANT
        assert outcome.effective_confidence == 0.0

    def test_decay_leaves_non_active_statements_alone(self) -> None:
        outcome = lifecycle.apply_decay(
            statement(status=StatementStatus.SUPERSEDED), now=NOW + timedelta(days=900)
        )
        assert outcome.went_dormant is False
        assert outcome.statement.status is StatementStatus.SUPERSEDED

    def test_category_ttls_differ_so_abandonments_outlive_tool_preferences(self) -> None:
        at = NOW + timedelta(days=100)
        tool = lifecycle.apply_decay(statement(category=Category.TOOL_PREFERENCE), now=at)
        abandoned = lifecycle.apply_decay(
            statement(category=Category.ABANDONED, statement="Abandoned no-code builders entirely"),
            now=at,
        )
        assert tool.needs_review is True
        assert abandoned.needs_review is False


class TestSeedEvidence:
    def test_seed_evidence_is_labelled_manual(self) -> None:
        assert lifecycle.SEED_EVIDENCE.kind is SourceType.MANUAL
        assert "hand-written" in lifecycle.SEED_EVIDENCE.label


class TestOriginIsPreserved:
    def test_a_reflection_authored_replacement_keeps_its_origin(self) -> None:
        new = statement(
            statement="Prefers Supabase after moving off Firebase",
            origin=Origin.REFLECTION,
            status=StatementStatus.PROPOSED,
        )
        _, replacement = lifecycle.supersede(statement(), new, now=NOW)
        assert replacement.origin is Origin.REFLECTION
