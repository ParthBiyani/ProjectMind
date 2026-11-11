"""Nothing sensitive is stored, shown or sent on: values are replaced, text is kept."""

from __future__ import annotations

from uuid import uuid4

import pytest

from projectmind.ingestion.pipeline import _redacted_record
from projectmind.models import (
    ContextBundle,
    EpisodicRecord,
    RecordType,
    ServedEvidence,
    SourceType,
    utcnow,
)
from projectmind.retrieval.evidence import Found, render_found
from projectmind.sources.secrets import contains_secret, redact, scrub

TOKEN = "ghp_" + "aB3dE6gH9" * 4


class TestWhatIsRedacted:
    @pytest.mark.parametrize(
        ("text", "gone", "kind"),
        [
            ("login: ann@example.org password: hunter2 for the portal", "hunter2", "password"),
            ("login: ann@example.org password: hunter2 for the portal", "ann@example.org", "login"),
            ("my password is Tr0ub4dor&3 today", "Tr0ub4dor&3", "password"),
            ("DB=postgres://admin:s3cr3tPass@db.internal:5432/app", "s3cr3tPass", "password"),
            ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "abcdefghij", "token"),
            ("card 4111 1111 1111 1111 exp 12/29", "4111 1111 1111 1111", "card"),
            ("credit card number 4111111111111111 on file", "4111111111111111", "card"),
            ("card 4111 1111 1111 1111 cvv 123", "cvv 123", "one-time-code"),
            ("Aadhaar: 2345-6789-0123", "2345-6789-0123", "aadhaar"),
            ("applicant PAN ABCDE1234F", "ABCDE1234F", "pan"),
            ("SSN 123-45-6789", "123-45-6789", "ssn"),
            ("export API_KEY=Zx81qLmN04aBc9QrTuV2wXy", "Zx81qLmN04aBc9QrTuV2wXy", "secret"),
            ("otp is 482913, do not share", "482913", "one-time-code"),
            (f"token: {TOKEN}", TOKEN, "token"),
            (
                "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----",
                "MIIabc",
                "private-key",
            ),
        ],
    )
    def test_the_value_is_replaced_and_the_kind_is_named(
        self, text: str, gone: str, kind: str
    ) -> None:
        result = redact(text)
        assert gone not in result.text.replace("[REDACTED:login]", "")
        assert f"[REDACTED:{kind}]" in result.text
        assert kind in result.kinds

    def test_the_words_around_a_secret_survive(self) -> None:
        result = scrub("The gateway needs token: " + TOKEN + " before it will start.")
        assert result is not None
        assert result.text == "The gateway needs token: [REDACTED:token] before it will start."

    def test_a_login_name_is_redacted_only_beside_a_password(self) -> None:
        assert redact("username: parthb").text == "username: parthb"
        paired = redact("username: parthb\npassword: Sup3rSecret!")
        assert "parthb" not in paired.text


class TestWhatIsLeftAlone:
    @pytest.mark.parametrize(
        "text",
        [
            "password: str = Field(...)",
            "password = get_password()",
            "password = os.environ['DB_PASSWORD']",
            "the password is required and must be hashed",
            "username: str",
            "epoch 12 loss 0.4521 dice 0.6234 step 1700000000 seed 42",
            "seed 4111111111111111 fold 3",
            "order id 4111 1111 1111 1112",
            "Why did we drop the profit factor column?",
            "api_key = os.getenv('KEY')",
        ],
    )
    def test_ordinary_text_and_code_are_not_touched(self, text: str) -> None:
        assert redact(text).text == text
        assert not redact(text).changed

    def test_redaction_markers_are_not_themselves_flagged(self) -> None:
        assert not contains_secret(redact("password: Tr0ub4dor&3 card 4111 1111 1111 1111").text)


class TestFailingClosed:
    def test_text_that_still_looks_secret_after_redaction_is_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import projectmind.sources.secrets as secrets

        monkeypatch.setattr(secrets, "redact", lambda text: secrets.Redaction(text))
        assert secrets.scrub(f"token: {TOKEN}") is None
        assert secrets.redact_text(f"token: {TOKEN}") == "[withheld: looked like a credential]"

    def test_scrub_returns_clean_text_unchanged(self) -> None:
        result = scrub("nothing to hide here")
        assert result is not None
        assert not result.changed


class TestWhatLeavesProjectMind:
    def test_rendered_context_is_redacted_even_if_the_index_was_not(self) -> None:
        evidence = ServedEvidence(
            chunk_id=uuid4(),
            project_key="demo",
            source_type="doc",
            title="Notes",
            excerpt=f"deploy with token: {TOKEN} then check the logs",
            citation="notes.md:1-2",
        )
        rendered = ContextBundle(evidence=(evidence,)).render()
        assert TOKEN not in rendered
        assert "[REDACTED:token]" in rendered
        assert "check the logs" in rendered

    def test_explicit_search_results_are_redacted_too(self) -> None:
        evidence = ServedEvidence(
            chunk_id=uuid4(),
            project_key="demo",
            source_type="doc",
            title="Notes",
            excerpt="my password is Tr0ub4dor&3 for the portal",
            citation="notes.md:1",
        )
        assert "Tr0ub4dor" not in render_found(Found(evidence=evidence, via="words"))

    def test_git_records_are_redacted_before_they_are_stored(self) -> None:
        record = EpisodicRecord(
            project_id=uuid4(),
            type=RecordType.DECISION,
            what="rotate the key",
            why=f"the old one, token: {TOKEN}, leaked",
            occurred_at=utcnow(),
            source_url="https://example.org/commit/1",
            source_type=SourceType.COMMIT,
            confidence=0.8,
            is_inference=False,
        )
        cleaned = _redacted_record(record)
        assert TOKEN not in cleaned.why
        assert "[REDACTED:token]" in cleaned.why
        assert cleaned.what == "rotate the key"
