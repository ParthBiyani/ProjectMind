"""Command output survives a pipe on Windows and long values are not cut off."""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from projectmind.cli._common import table, use_utf8_streams

TEXT = "memory · decided — why…"


def piped(encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, write_through=True)


def test_piped_output_is_utf8_whatever_the_code_page(monkeypatch: pytest.MonkeyPatch) -> None:
    out, err = piped("cp1252"), piped("cp437")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    use_utf8_streams()
    print(TEXT)
    print(TEXT, file=sys.stderr)
    assert out.buffer.getvalue().decode("utf-8").strip() == TEXT  # type: ignore[attr-defined]
    assert err.buffer.getvalue().decode("utf-8").strip() == TEXT  # type: ignore[attr-defined]


def test_a_stream_that_cannot_be_reconfigured_is_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    use_utf8_streams()  # no reconfigure(): must not raise


def test_long_values_fold_instead_of_ending_in_an_ellipsis() -> None:
    built = table("project", "prompt")
    built.add_row("parthbiyani-projectmind", "why did we drop the profit factor column")
    buffer = io.StringIO()
    Console(file=buffer, width=40, force_terminal=False).print(built)
    rendered = buffer.getvalue()
    assert "…" not in rendered
    # the name continues on the next line, beside the next column's own wrapped text
    assert "parthbiyani-project" in rendered
    assert "mind" in rendered.splitlines()[2]
