"""Claude Code session transcripts, distilled to what was asked and answered.

A transcript is a long JSON-lines file, mostly tool calls and tool output. What
is worth remembering is small: what the user asked, and what the assistant said
back when it finished. That is what is kept, verbatim, one chunk per turn:

    You: <the user's message>
    Reply: <the assistant's closing message for that turn>

Everything else is ignored: tool calls, tool results, reasoning, file snapshots,
system reminders and IDE context wrappers. The chunk carries the timestamp and
turn number as its locator, so a reader can find the turn again in the file.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from projectmind.sources.models import SourceChunk
from projectmind.sources.walker import Candidate

MAX_USER_CHARS = 1_400
MAX_REPLY_CHARS = 1_400
MAX_TURNS = 400

#: Wrapper blocks the client adds around real user text.
_WRAPPERS = re.compile(
    r"<(?P<tag>system-reminder|ide_[a-z_]+|local-command-[a-z]+|command-[a-z]+|"
    r"user-prompt-submit-hook|task-notification|pasted_content[^>]*)>.*?</(?P=tag)>",
    re.DOTALL | re.IGNORECASE,
)
_NOISE_PREFIXES = ("[Request interrupted", "Caveat:", "<local-command", "Unknown command")


@dataclass(slots=True)
class Turn:
    number: int
    timestamp: str
    user: str
    reply: str = ""


def candidates(root: Path) -> Iterator[Candidate]:
    """Every transcript file under a transcripts root (`projects/<slug>/<id>.jsonl`)."""
    for directory, _, files in os.walk(root):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = Path(directory) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > 0:
                yield Candidate(path, "transcript", stat.st_size, stat.st_mtime, stat.st_mtime_ns)


def _clean(text: str) -> str:
    cleaned = _WRAPPERS.sub("", text).strip()
    return "" if cleaned.startswith(_NOISE_PREFIXES) else cleaned


def _user_text(message: dict[str, Any]) -> str:
    """The words the user typed, or empty for tool results and wrappers."""
    content = message.get("content")
    if isinstance(content, str):
        return _clean(content)
    if isinstance(content, list):
        pieces = [
            _clean(str(block.get("text", "")))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(piece for piece in pieces if piece)
    return ""


def _assistant_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")).strip()
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    return ""


def read_turns(path: Path) -> tuple[str, str | None, list[Turn]]:
    """(title, working directory, turns) from one transcript, streaming the file."""
    title = ""
    cwd: str | None = None
    turns: list[Turn] = []
    current: Turn | None = None

    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            if kind == "ai-title" and record.get("aiTitle"):
                title = str(record["aiTitle"])
                continue
            if kind not in {"user", "assistant"} or record.get("isSidechain"):
                continue
            cwd = cwd or record.get("cwd")
            message = record.get("message")
            if not isinstance(message, dict):
                continue

            if kind == "user":
                if record.get("isMeta"):
                    continue
                text = _user_text(message)
                if not text:
                    continue  # a tool result or a wrapper, not a person typing
                current = Turn(
                    number=len(turns) + 1,
                    timestamp=str(record.get("timestamp", ""))[:16].replace("T", " "),
                    user=text,
                )
                turns.append(current)
                if len(turns) >= MAX_TURNS:
                    break
            elif current is not None:
                said = _assistant_text(message)
                if said:
                    # The last thing said in a turn is its conclusion.
                    current.reply = said
    return title, cwd, turns


def chunks_for(path: Path) -> tuple[str, str | None, list[SourceChunk]]:
    """(title, working directory, one chunk per turn that has something to keep)."""
    title, cwd, turns = read_turns(path)
    chunks: list[SourceChunk] = []
    for turn in turns:
        user = turn.user[:MAX_USER_CHARS]
        body = f"You: {user}"
        if turn.reply:
            body += f"\n\nReply: {turn.reply[:MAX_REPLY_CHARS]}"
        chunks.append(
            SourceChunk(
                text=body,
                heading=title or "session",
                locator=f"turn {turn.number}, {turn.timestamp}".strip(", "),
            )
        )
    return title or path.stem, cwd, chunks
