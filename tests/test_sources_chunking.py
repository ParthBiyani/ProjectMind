from __future__ import annotations

import json

from projectmind.sources.chunking import (
    MAX_CHARS,
    chunk_log,
    chunk_markdown,
    chunk_notebook,
    chunk_python,
    chunk_results_json,
    chunk_text,
)
from projectmind.sources.secrets import contains_secret

MARKDOWN = """# Prostate MRI

## Score definition

10 * (0.55*Dice + 0.25*boundary_F1 + 0.10*volume_agreement + 0.10*centroid_agreement)
Exact voxel matching, no tolerance band, so a one voxel shift costs boundary F1 heavily.

## Dataset findings

| Property | Value |
| --- | --- |
| connected components per mask | 1 for all 25 cases |
| 3-D holes | 0 for all 25 cases |

Consequences used: a single component post-process is safe on this data.

### Tiny

x
"""


def spans_are_exact(text: str, chunks: list) -> bool:  # type: ignore[type-arg]
    """Every chunk's text is exactly the lines its span claims."""
    lines = text.splitlines()
    return all(
        "\n".join(lines[c.start_line - 1 : c.end_line]).strip("\n") == c.text
        for c in chunks
        if c.start_line
    )


class TestMarkdown:
    def test_chunks_carry_the_heading_path(self) -> None:
        chunks = chunk_markdown(MARKDOWN)
        headings = {chunk.heading for chunk in chunks}
        assert "Prostate MRI > Score definition" in headings
        assert "Prostate MRI > Dataset findings" in headings

    def test_chunk_text_is_verbatim_and_the_span_is_exact(self) -> None:
        chunks = chunk_markdown(MARKDOWN)
        assert spans_are_exact(MARKDOWN, chunks)
        score = next(c for c in chunks if c.heading.endswith("Score definition"))
        assert "0.55*Dice" in score.text

    def test_a_table_stays_with_the_sentence_that_explains_it(self) -> None:
        chunks = chunk_markdown(MARKDOWN)
        findings = next(c for c in chunks if c.heading.endswith("Dataset findings"))
        assert "connected components" in findings.text
        assert "single component post-process" in findings.text

    def test_a_long_section_is_split_without_losing_or_inventing_lines(self) -> None:
        paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 80 for i in range(12))
        text = f"# Long\n\n{paragraphs}\n"
        chunks = chunk_markdown(text)
        assert len(chunks) > 1
        assert all(len(c.text) <= MAX_CHARS + 200 for c in chunks)
        assert spans_are_exact(text, chunks)
        joined = " ".join(c.text for c in chunks)
        assert all(f"Paragraph {i} " in joined for i in range(12))

    def test_headings_inside_a_code_fence_are_not_headings(self) -> None:
        fence = "```"
        text = (
            f"# Real\n\n{fence}\n# not a heading\nprint(1)\n{fence}\n\n"
            "After the fence there is more text here.\n"
        )
        assert {c.heading for c in chunk_markdown(text)} == {"Real"}

    def test_an_empty_document_has_no_chunks(self) -> None:
        assert chunk_markdown("") == []
        assert chunk_markdown("\n\n  \n") == []


class TestPlainText:
    def test_paragraphs_are_packed_with_line_spans(self) -> None:
        text = "first paragraph\nstill first\n\nsecond paragraph\n"
        chunks = chunk_text(text)
        assert spans_are_exact(text, chunks)
        assert chunks[0].start_line == 1


class TestPython:
    SOURCE = '''# Experiment v4: patch-based 3D unet, CV dice 0.62
# Tried 48^3 patches with heavy elastic augmentation.
"""Patch training for the prostate task."""
import os

SECRET_LOOKING = "not a chunk"


def train(volumes):
    """Train on random patches; returns the mean validation dice."""
    total = 0
    for volume in volumes:
        total += 1
    return total


def helper():
    return 1


class Model:
    """A small 3D unet with instance norm."""

    def forward(self, x):
        return x
'''

    def test_the_header_comment_and_docstring_become_one_chunk(self) -> None:
        chunks = chunk_python(self.SOURCE)
        module = chunks[0]
        assert module.heading == "module"
        assert "CV dice 0.62" in module.text
        assert "Patch training" in module.text

    def test_documented_definitions_are_chunked_and_undocumented_ones_are_not(self) -> None:
        headings = [c.heading for c in chunk_python(self.SOURCE)]
        assert "train" in headings
        assert "Model" in headings
        assert "helper" not in headings

    def test_function_bodies_are_not_indexed(self) -> None:
        joined = " ".join(c.text for c in chunk_python(self.SOURCE))
        assert "total += 1" not in joined
        assert "SECRET_LOOKING" not in joined

    def test_a_syntax_error_does_not_raise(self) -> None:
        assert isinstance(chunk_python("def broken(:\n  # header\n"), list)


class TestNotebook:
    def notebook(self) -> str:
        return json.dumps(
            {
                "cells": [
                    {"cell_type": "markdown", "source": ["# Nemotron v3\n", "Concise CoT run."]},
                    {
                        "cell_type": "code",
                        "source": ["score = evaluate(model)\n", "print(score)"],
                        "outputs": [
                            {"output_type": "stream", "text": ["public LB 0.8421\n"]},
                            {"output_type": "display_data", "data": {"image/png": "AAAA" * 500}},
                        ],
                    },
                    {"cell_type": "code", "source": ["x=1"], "outputs": []},
                ]
            }
        )

    def test_markdown_cells_and_code_with_text_outputs_are_chunked(self) -> None:
        chunks = chunk_notebook(self.notebook())
        assert [c.locator for c in chunks] == ["cell 1", "cell 2"]
        assert "public LB 0.8421" in chunks[1].text

    def test_image_outputs_are_never_read(self) -> None:
        assert "AAAA" not in " ".join(c.text for c in chunk_notebook(self.notebook()))

    def test_a_broken_notebook_is_empty_not_an_error(self) -> None:
        assert chunk_notebook("{not json") == []


class TestLogsAndResults:
    LOG = """starting run
epoch 1 loss 0.9123
epoch 2 loss 0.7011
some noise line
more noise

fold 3 dice: 0.6234
final cv score = 0.6411
"""

    def test_only_lines_that_report_a_result_are_kept(self) -> None:
        chunks = chunk_log(self.LOG)
        joined = " ".join(c.text for c in chunks)
        assert "dice: 0.6234" in joined
        assert "final cv score = 0.6411" in joined
        assert "more noise" not in joined

    def test_log_spans_point_at_the_real_lines(self) -> None:
        chunks = chunk_log(self.LOG)
        assert spans_are_exact(self.LOG, chunks)

    def test_a_log_with_no_results_has_no_chunks(self) -> None:
        assert chunk_log("hello\nworld\n") == []

    def test_a_results_json_is_flattened(self) -> None:
        chunks = chunk_results_json('{"cv": {"dice": 0.62, "folds": [0.6, 0.64]}, "model": "unet"}')
        assert "cv.dice: 0.62" in chunks[0].text
        assert "model: unet" in chunks[0].text

    def test_json_without_numbers_is_not_a_result(self) -> None:
        assert chunk_results_json('{"name": "x", "tags": ["a"]}') == []


class TestSecrets:
    def test_known_token_shapes_are_caught(self) -> None:
        assert contains_secret("key = sk-ant-api03-" + "a1B2c3D4" * 4)
        assert contains_secret("token: ghp_" + "aB3dE6gH9" * 4)
        assert contains_secret("AKIA" + "ABCDEFGHIJKLMNOP")
        assert contains_secret("-----BEGIN RSA PRIVATE KEY-----\nMIIE")

    def test_an_assigned_secret_with_a_random_value_is_caught(self) -> None:
        assert contains_secret('API_KEY = "Zx81qLmN04aBc9QrTuV2wXy"')
        assert contains_secret("password: Hunter2Hunter2Hunter2")

    def test_placeholders_and_prose_are_not_secrets(self) -> None:
        assert not contains_secret('api_key = "your-api-key-goes-here-please"')
        assert not contains_secret("API_KEY = os.environ['API_KEY']")
        assert not contains_secret("The token budget for the run was 2300 tokens per bundle.")
        assert not contains_secret("sha256 of the file was 9f86d081884c7d659a2feaa0c55ad015")
