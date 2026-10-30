"""Identity documents and personal identifiers must never enter the index.

This guard exists because they once did: a folder of family identity documents
was swept into a "career" source. The rule is now structural, not a habit.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.sources.secrets import contains_identifier, contains_secret, is_sensitive_name
from projectmind.sources.walker import walk
from projectmind.storage import SqliteStore


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - 7_200
    os.utime(path, (stamp, stamp))


class TestNames:
    @pytest.mark.parametrize(
        "name",
        [
            "Masked Aadhar Card (PART2004).pdf",
            "VB. PAN Card.pdf",
            "Passport.pdf",
            "Varun Voter ID Card.pdf",
            "Draft Voter List 79.pdf",
            "Driving License.pdf",
            "Class 10 Marksheet.pdf",
            "bank statement march.pdf",
            "salary slip oct.pdf",
            "Form 16 2025.pdf",
            "Personal Docs",
            "KYC.docx",
        ],
    )
    def test_identity_and_financial_documents_are_recognised(self, name: str) -> None:
        assert is_sensitive_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "CLAUDE.md",
            "dl_utils.py",
            "Deep Learning",
            "panel.py",
            "pandas_notes.md",
            "transport_layer.md",
            "leaderboard.csv",
            "README.md",
            "span_alignment.py",
        ],
    )
    def test_ordinary_names_are_not(self, name: str) -> None:
        assert not is_sensitive_name(name)


class TestContent:
    def test_spaced_aadhaar_numbers_are_caught(self) -> None:
        assert contains_identifier("number 2345 6789 0123 issued 2019")

    def test_other_aadhaar_spellings_need_the_word(self) -> None:
        assert contains_identifier("Aadhaar: 2345-6789-0123")
        assert contains_identifier("UID 234567890123")
        assert not contains_identifier("range 2345-6789-0123 in the table")
        assert not contains_identifier("id 234567890123 in the export")

    def test_pan_numbers_are_caught(self) -> None:
        assert contains_identifier("PAN: ABCDE1234F")

    def test_ssn_needs_its_name_next_to_it(self) -> None:
        assert contains_identifier("SSN 123-45-6789")
        assert not contains_identifier("build 123-45-6789 of the pipeline")

    def test_a_card_number_printed_like_a_card_is_caught(self) -> None:
        assert contains_identifier("4111 1111 1111 1111 exp 12/29")
        assert contains_identifier("4111-1111-1111-1111")
        assert contains_identifier("amex 3782 822463 10005")

    def test_a_bare_digit_run_is_never_a_card_number(self) -> None:
        """Logs and result files are full of these, and a Luhn check passes one in ten."""
        assert not contains_identifier("4111111111111111")
        assert not contains_identifier("seed 4111111111111111 fold 3")
        assert not contains_identifier("order id 4111 1111 1111 1112")  # fails Luhn

    def test_a_bare_run_next_to_the_word_card_is_caught(self) -> None:
        assert contains_identifier("credit card number 4111111111111111 on file")
        assert not contains_identifier("card 4111111111111112")  # fails Luhn

    def test_experiment_output_is_not_flagged(self) -> None:
        log = "\n".join(
            f"fold {i} dice 0.62{i} ids [{10**12 + i}, {2 * 10**12 + i}] t {1_700_000_000 + i}"
            for i in range(50)
        )
        assert not contains_secret(log)

    def test_ordinary_numbers_in_a_log_are_not_identifiers(self) -> None:
        text = "epoch 12 loss 0.4521 dice 0.6234 step 1700000000 seed 42 batch 128"
        assert not contains_identifier(text)
        assert not contains_secret(text)

    def test_the_withhold_rule_covers_identifiers_too(self) -> None:
        assert contains_secret("applicant PAN ABCDE1234F, phone on file")


class TestIndexing:
    def test_a_folder_of_identity_documents_is_never_walked(self, tmp_path: Path) -> None:
        write(tmp_path / "Personal Docs" / "Aadhar Card" / "notes.md", "# a\n\nsome text here\n")
        write(tmp_path / "Varun" / "VB. PAN Card.txt", "a text file\n")
        write(tmp_path / "work" / "plan.md", "# Plan\n\nBuild the thing carefully.\n")
        names = {c.path.name for c in walk(tmp_path)}
        assert names == {"plan.md"}

    def test_a_note_that_pastes_an_identifier_loses_only_that_chunk(self, tmp_path: Path) -> None:
        write(
            tmp_path / "p" / "NOTES.md",
            "# Notes\n\n## Forms\n\nMy PAN is ABCDE1234F, keep it for the filing.\n\n"
            "## Plan\n\nThe quarterly roadmap covers retrieval and evaluation work.\n",
        )
        store = SqliteStore(":memory:")
        store.migrate()
        report = KnowledgeIndexer(store).scan_root(RootConfig(path=tmp_path, depth=1))
        assert report.withheld >= 1
        assert store.search_chunks(["ABCDE1234F"]) == []
        assert store.search_chunks(["roadmap"])
        store.close()
