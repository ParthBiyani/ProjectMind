"""PDF, Word and PowerPoint documents.

The libraries are optional (`pip install 'projectmind[docs]'`). Text is read as
the author wrote it, page by page or slide by slide, so a citation can say
`page 12` or `slide 4`. Nothing is rendered, OCR'd or summarised.
"""

from __future__ import annotations

from pathlib import Path

from projectmind.sources.chunking import MAX_CHARS
from projectmind.sources.models import SourceChunk

MAX_PAGES = 400


class MissingDependencyError(RuntimeError):
    """The optional library for this format is not installed."""


def _pack(label: str, text: str, locator: str, heading: str = "") -> list[SourceChunk]:
    """Split one page or slide into chunks of reasonable size, same locator."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_CHARS:
        return [SourceChunk(text=text, heading=heading or label, locator=locator)]
    chunks: list[SourceChunk] = []
    current: list[str] = []
    size = 0
    for paragraph in text.split("\n"):
        if size + len(paragraph) > MAX_CHARS and current:
            chunks.append(
                SourceChunk(text="\n".join(current), heading=heading or label, locator=locator)
            )
            current, size = [], 0
        current.append(paragraph)
        size += len(paragraph) + 1
    if current:
        chunks.append(
            SourceChunk(text="\n".join(current), heading=heading or label, locator=locator)
        )
    return chunks


def read_pdf(path: Path) -> tuple[str, list[SourceChunk]]:
    try:
        from pypdf import PdfReader
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("pypdf") from exc
    reader = PdfReader(str(path))
    title = str((reader.metadata.title if reader.metadata else None) or path.stem)
    chunks: list[SourceChunk] = []
    for number, page in enumerate(reader.pages[:MAX_PAGES], start=1):
        chunks.extend(_pack(title, page.extract_text() or "", f"page {number}"))
    return title, chunks


def read_docx(path: Path) -> tuple[str, list[SourceChunk]]:
    try:
        from docx import Document
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("python-docx") from exc
    document = Document(str(path))
    title = document.core_properties.title or path.stem
    sections: list[tuple[str, list[str]]] = [("", [])]
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        if not text:
            continue
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        if style.lower().startswith("heading"):
            sections.append((text, []))
        else:
            sections[-1][1].append(text)
    for table in document.tables:
        rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        sections[-1][1].append("\n".join(rows))
    chunks: list[SourceChunk] = []
    for index, (heading, body) in enumerate(sections, start=1):
        chunks.extend(_pack(title, "\n".join(body), f"section {index}", heading))
    return str(title), chunks


def read_pptx(path: Path) -> tuple[str, list[SourceChunk]]:
    try:
        from pptx import Presentation
    except ModuleNotFoundError as exc:
        raise MissingDependencyError("python-pptx") from exc
    deck = Presentation(str(path))
    title = deck.core_properties.title or path.stem
    chunks: list[SourceChunk] = []
    for number, slide in enumerate(deck.slides, start=1):
        pieces: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                pieces.extend(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip())
        notes = slide.notes_slide.notes_text_frame.text.strip() if slide.has_notes_slide else ""
        if notes:
            pieces.append(f"Notes: {notes}")
        chunks.extend(_pack(title, "\n".join(pieces), f"slide {number}"))
    return str(title), chunks


READERS = {"pdf": read_pdf, "docx": read_docx, "pptx": read_pptx}
