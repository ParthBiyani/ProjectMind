"""What a connector hands to the indexer.

A connector's whole job is to turn one source (a file, a page, a transcript) into
a `SourceDocument`: its identity, a fingerprint of its content and a list of
citable chunks. Everything downstream (storage, search, serving) works from
these and does not care where they came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SourceChunk:
    """One citable slice. Lines are 1-based and inclusive; 0 means not line-based."""

    text: str
    heading: str = ""
    start_line: int = 0
    end_line: int = 0
    locator: str = ""


@dataclass(slots=True)
class SourceDocument:
    uri: str
    source_type: str
    project_key: str
    title: str
    mtime: datetime
    content_hash: str
    chunks: list[SourceChunk] = field(default_factory=list)
    #: Chunks dropped because they looked like they contained a credential.
    withheld: int = 0
