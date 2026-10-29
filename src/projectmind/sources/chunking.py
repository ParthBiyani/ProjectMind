"""Splitting sources into chunks that can be quoted and cited.

Each chunker keeps the original text of a chunk verbatim and records where it
came from (line span, or a locator such as `cell 7`). Nothing here summarises or
rewrites: what is later shown to an agent is the author's own words, and the
span lets a reader open the file at exactly that place.
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from collections.abc import Iterator
from typing import Any

from projectmind.sources.models import SourceChunk

MAX_CHARS = 1_800
MIN_CHARS = 60
MAX_CHUNKS_PER_CODE_FILE = 12
MAX_CHUNKS_PER_NOTEBOOK = 60
MAX_METRIC_CHUNKS = 40
MAX_JSON_LINES = 60
OUTPUT_CHARS = 600

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

#: A line that reports a measured result.
_METRIC = re.compile(
    r"(?i)\b(?:score|dice|iou|f1|auc|roc|rmse|mae|mse|loss|acc(?:uracy)?|precision|recall|"
    r"cv|lb|leaderboard|fold|epoch|val|valid|metric|ndcg|mrr|map|bleu|rouge|wer|cer)\b"
    r"[^\n]{0,60}?[:=\s]\s*-?\d+\.\d+"
)


# --------------------------------------------------------------------------- #
# Shared windowing
# --------------------------------------------------------------------------- #


def _window(
    lines: list[str], first_line: int, heading: str, *, max_chars: int = MAX_CHARS
) -> Iterator[SourceChunk]:
    """Pack paragraphs into chunks of about `max_chars`, keeping exact line spans.

    `first_line` is the 1-based number of `lines[0]`. A paragraph longer than the
    limit is split on line boundaries, never mid-line.
    """
    paragraphs: list[tuple[int, int]] = []  # inclusive index ranges into `lines`
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            if start is None:
                start = index
        elif start is not None:
            paragraphs.append((start, index - 1))
            start = None
    if start is not None:
        paragraphs.append((start, len(lines) - 1))

    def emit(a: int, b: int) -> SourceChunk | None:
        text = "\n".join(lines[a : b + 1]).strip("\n")
        if not text.strip():
            return None
        return SourceChunk(
            text=text, heading=heading, start_line=first_line + a, end_line=first_line + b
        )

    current: tuple[int, int] | None = None

    def size(span: tuple[int, int]) -> int:
        return sum(len(line) + 1 for line in lines[span[0] : span[1] + 1])

    for a, b in paragraphs:
        if size((a, b)) > max_chars:
            if current is not None and (chunk := emit(*current)):
                yield chunk
            current = None
            piece_start = a
            for index in range(a, b + 1):
                if size((piece_start, index)) > max_chars and index > piece_start:
                    if chunk := emit(piece_start, index - 1):
                        yield chunk
                    piece_start = index
            if chunk := emit(piece_start, b):
                yield chunk
            continue
        if current is None:
            current = (a, b)
        elif size((current[0], b)) <= max_chars:
            current = (current[0], b)
        else:
            if chunk := emit(*current):
                yield chunk
            current = (a, b)
    if current is not None and (chunk := emit(*current)):
        yield chunk


# --------------------------------------------------------------------------- #
# Markdown and plain text
# --------------------------------------------------------------------------- #


def chunk_markdown(text: str) -> list[SourceChunk]:
    """Chunks per heading section, labelled with the heading path (`A > B`)."""
    lines = text.splitlines()
    sections: list[tuple[str, int, list[str]]] = []  # (heading path, first line, body)
    stack: list[tuple[int, str]] = []
    path = ""
    body: list[str] = []
    body_start = 1
    in_fence = False

    def flush() -> None:
        if any(line.strip() for line in body):
            sections.append((path, body_start, body.copy()))

    for number, line in enumerate(lines, start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush()
            level, title = len(match.group(1)), match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            path = " > ".join(name for _, name in stack)
            body = [line]
            body_start = number
        else:
            body.append(line)
    flush()

    # A section that is only a heading or a sentence is folded into the next one,
    # so that "## Results" followed by a table is one citable thing.
    merged: list[tuple[str, int, list[str]]] = []
    carry: tuple[int, list[str]] | None = None
    for position, (heading, first, section_lines) in enumerate(sections):
        if carry is not None:
            section_lines = [*carry[1], *section_lines]
            first = carry[0]
            carry = None
        if sum(len(line) for line in section_lines) < MIN_CHARS and position < len(sections) - 1:
            carry = (first, section_lines)
            continue
        merged.append((heading, first, section_lines))
    if carry is not None:
        merged.append(("", carry[0], carry[1]))

    chunks: list[SourceChunk] = []
    for heading, first, section_lines in merged:
        chunks.extend(_window(section_lines, first, heading))
    return chunks


def chunk_text(text: str) -> list[SourceChunk]:
    return list(_window(text.splitlines(), 1, ""))


# --------------------------------------------------------------------------- #
# Python
# --------------------------------------------------------------------------- #


def chunk_python(text: str) -> list[SourceChunk]:
    """The header comments and docstrings: what the author said the code is for.

    Function bodies are not indexed. Experiment scripts describe themselves in a
    header comment or a docstring; the bodies are mostly noise for retrieval and
    are the largest source of accidental secrets.
    """
    lines = text.splitlines()
    chunks: list[SourceChunk] = []

    header_end = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            header_end = index + 1
            continue
        break
    header = "\n".join(lines[:header_end]).strip()

    try:
        with warnings.catch_warnings():
            # Old scripts are full of invalid escape sequences; that is the
            # script's business and not worth a warning on every scan.
            warnings.simplefilter("ignore")
            tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return chunk_text("\n".join(lines[: max(header_end, 40)]))[:MAX_CHUNKS_PER_CODE_FILE]

    module_doc = ast.get_docstring(tree)
    if module_doc or header:
        doc_node = tree.body[0] if tree.body else None
        end = (
            doc_node.end_lineno
            if isinstance(doc_node, ast.Expr)
            and isinstance(doc_node.value, ast.Constant)
            and isinstance(doc_node.value.value, str)
            and doc_node.end_lineno
            else header_end
        )
        body = "\n".join(lines[: max(end, header_end)]).strip()
        if body:
            chunks.append(
                SourceChunk(
                    text=body[: MAX_CHARS * 2],
                    heading="module",
                    start_line=1,
                    end_line=max(end, header_end),
                )
            )

    for node in tree.body:
        if len(chunks) >= MAX_CHUNKS_PER_CODE_FILE:
            break
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        doc = ast.get_docstring(node)
        if not doc:
            continue
        signature = lines[node.lineno - 1].strip()
        first = node.body[0]
        end = first.end_lineno or node.lineno
        chunks.append(
            SourceChunk(
                text=f"{signature}\n{doc}"[: MAX_CHARS * 2],
                heading=node.name,
                start_line=node.lineno,
                end_line=end,
            )
        )
    return chunks


# --------------------------------------------------------------------------- #
# Notebooks
# --------------------------------------------------------------------------- #


def _cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def _cell_output_text(cell: dict[str, Any]) -> str:
    """Text outputs only. Images and rich display data are never read."""
    pieces: list[str] = []
    for output in cell.get("outputs", []) or []:
        if output.get("output_type") == "stream":
            text = output.get("text", "")
            pieces.append("".join(text) if isinstance(text, list) else str(text))
        elif output.get("output_type") in {"execute_result", "display_data"}:
            plain = (output.get("data") or {}).get("text/plain")
            if plain:
                pieces.append("".join(plain) if isinstance(plain, list) else str(plain))
    return "\n".join(pieces).strip()


def chunk_notebook(text: str) -> list[SourceChunk]:
    try:
        notebook = json.loads(text)
    except ValueError:
        return []
    heading = ""
    chunks: list[SourceChunk] = []
    for number, cell in enumerate(notebook.get("cells", []), start=1):
        if len(chunks) >= MAX_CHUNKS_PER_NOTEBOOK:
            break
        source = _cell_source(cell).strip()
        locator = f"cell {number}"
        if cell.get("cell_type") == "markdown":
            for line in source.splitlines():
                if match := _HEADING.match(line):
                    heading = match.group(2).strip()
                    break
            if len(source) >= 20:
                chunks.append(
                    SourceChunk(text=source[: MAX_CHARS * 2], heading=heading, locator=locator)
                )
        elif cell.get("cell_type") == "code":
            head = "\n".join(source.splitlines()[:12])
            output = _cell_output_text(cell)[:OUTPUT_CHARS]
            if len(head) < 20 and not output:
                continue
            body = f"```python\n{head}\n```"
            if output:
                body += f"\n\nOutput:\n{output}"
            chunks.append(SourceChunk(text=body, heading=heading, locator=locator))
    return chunks


# --------------------------------------------------------------------------- #
# Logs and result files
# --------------------------------------------------------------------------- #


def chunk_log(text: str) -> list[SourceChunk]:
    """Only the lines that report a measured result, with a line of context."""
    lines = text.splitlines()
    hits = [index for index, line in enumerate(lines) if _METRIC.search(line)]
    if not hits:
        return []
    groups: list[list[int]] = [[hits[0]]]
    for index in hits[1:]:
        if index - groups[-1][-1] <= 3:
            groups[-1].append(index)
        else:
            groups.append([index])
    chunks: list[SourceChunk] = []
    for group in groups[:MAX_METRIC_CHUNKS]:
        a, b = max(0, group[0] - 1), min(len(lines) - 1, group[-1] + 1)
        while a < b and not lines[a].strip():
            a += 1
        while b > a and not lines[b].strip():
            b -= 1
        chunks.append(
            SourceChunk(
                text="\n".join(lines[a : b + 1])[:MAX_CHARS],
                heading="measured results",
                start_line=a + 1,
                end_line=b + 1,
            )
        )
    return chunks


def chunk_results_json(text: str) -> list[SourceChunk]:
    """A small results file, flattened to `path: value` lines."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    rows: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if len(rows) >= MAX_JSON_LINES:
            return
        if isinstance(value, dict):
            for key, inner in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), inner)
        elif isinstance(value, list):
            if value and all(isinstance(item, int | float) for item in value):
                preview = ", ".join(str(item) for item in value[:8])
                rows.append(f"{prefix}: [{preview}{', ...' if len(value) > 8 else ''}]")
            else:
                for index, inner in enumerate(value[:5]):
                    walk(f"{prefix}[{index}]", inner)
        elif isinstance(value, int | float | bool) or (
            isinstance(value, str) and len(value) <= 120
        ):
            rows.append(f"{prefix}: {value}")

    walk("", data)
    body = "\n".join(rows)
    if not any(re.search(r"\d", row.split(": ", 1)[-1]) for row in rows):
        return []
    return [SourceChunk(text=body[:MAX_CHARS], heading="results", locator="json")] if body else []
