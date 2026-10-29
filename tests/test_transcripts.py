from __future__ import annotations

import json
import os
from pathlib import Path

from projectmind.sources.config import RootConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.sources.transcripts import chunks_for, read_turns
from projectmind.storage import SqliteStore


def line(**fields: object) -> str:
    return json.dumps(fields)


def transcript(tmp_path: Path, cwd: str = "D:/Projects/Demo") -> Path:
    path = tmp_path / "projects" / "d--Projects-Demo" / "abc.jsonl"
    path.parent.mkdir(parents=True)
    records = [
        line(type="ai-title", aiTitle="Retry policy for the provider client"),
        line(type="queue-operation", operation="enqueue"),
        line(
            type="user",
            cwd=cwd,
            timestamp="2026-09-01T10:15:00Z",
            message={"role": "user", "content": "How should the client back off on 429s?"},
        ),
        line(
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
                    {"type": "text", "text": "Reading the client first."},
                ],
            },
        ),
        line(
            type="user",
            message={
                "role": "user",
                "content": [{"type": "tool_result", "content": "SECRET FILE CONTENTS"}],
            },
        ),
        line(
            type="assistant",
            message={
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Use exponential backoff with jitter, capped at 30s."}
                ],
            },
        ),
        line(
            type="user",
            timestamp="2026-09-01T10:40:00Z",
            message={
                "role": "user",
                "content": [
                    {"type": "text", "text": "<system-reminder>ignore me</system-reminder>"},
                    {"type": "text", "text": "Also make the cap configurable."},
                ],
            },
        ),
        line(
            type="assistant",
            message={
                "role": "assistant",
                "content": [{"type": "text", "text": "Done: PROVIDER_MAX_BACKOFF."}],
            },
        ),
        line(
            type="user", isSidechain=True, message={"role": "user", "content": "a sub-agent prompt"}
        ),
        line(type="user", message={"role": "user", "content": "[Request interrupted by user]"}),
        "not json at all",
    ]
    path.write_text("\n".join(records) + "\n", encoding="utf-8")
    return path


class TestReadTurns:
    def test_each_typed_message_starts_a_turn(self, tmp_path: Path) -> None:
        _, _, turns = read_turns(transcript(tmp_path))
        assert [turn.user for turn in turns] == [
            "How should the client back off on 429s?",
            "Also make the cap configurable.",
        ]

    def test_the_last_thing_the_assistant_said_is_the_reply(self, tmp_path: Path) -> None:
        _, _, turns = read_turns(transcript(tmp_path))
        assert turns[0].reply == "Use exponential backoff with jitter, capped at 30s."
        assert turns[1].reply == "Done: PROVIDER_MAX_BACKOFF."

    def test_tool_results_reasoning_wrappers_and_sidechains_are_ignored(
        self, tmp_path: Path
    ) -> None:
        title, cwd, turns = read_turns(transcript(tmp_path))
        everything = json.dumps([(t.user, t.reply) for t in turns])
        assert "SECRET FILE CONTENTS" not in everything
        assert "private reasoning" not in everything
        assert "ignore me" not in everything
        assert "sub-agent" not in everything
        assert "Request interrupted" not in everything
        assert title == "Retry policy for the provider client"
        assert cwd == "D:/Projects/Demo"

    def test_garbage_lines_do_not_stop_the_read(self, tmp_path: Path) -> None:
        assert len(read_turns(transcript(tmp_path))[2]) == 2


class TestChunks:
    def test_one_chunk_per_turn_with_a_locator(self, tmp_path: Path) -> None:
        title, _, chunks = chunks_for(transcript(tmp_path))
        assert title == "Retry policy for the provider client"
        assert len(chunks) == 2
        assert chunks[0].text.startswith("You: How should the client back off")
        assert "Reply: Use exponential backoff" in chunks[0].text
        assert chunks[0].locator == "turn 1, 2026-09-01 10:15"
        assert chunks[0].start_line == 0

    def test_a_turn_without_a_reply_is_still_kept(self, tmp_path: Path) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text(
            line(type="user", message={"role": "user", "content": "Remember this question"}) + "\n",
            encoding="utf-8",
        )
        _, _, chunks = chunks_for(path)
        assert chunks[0].text == "You: Remember this question"


class TestIndexing:
    def index(self, tmp_path: Path) -> SqliteStore:
        path = transcript(tmp_path)
        stamp = path.stat().st_mtime - 7_200
        os.utime(path, (stamp, stamp))
        store = SqliteStore(":memory:")
        store.migrate()
        KnowledgeIndexer(store).scan_root(
            RootConfig(path=tmp_path / "projects", kind="transcripts", depth=0)
        )
        return store

    def test_a_session_becomes_searchable_and_cites_the_turn(self, tmp_path: Path) -> None:
        store = self.index(tmp_path)
        hit = store.search_chunks(["backoff", "jitter"])[0]
        assert hit.document.source_type == "transcript"
        assert hit.document.title == "Retry policy for the provider client"
        assert hit.chunk.locator == "turn 1, 2026-09-01 10:15"

    def test_tool_output_never_reaches_the_index(self, tmp_path: Path) -> None:
        store = self.index(tmp_path)
        assert store.search_chunks(["SECRET", "FILE", "CONTENTS"]) == []

    def test_a_session_still_in_progress_is_left_alone(self, tmp_path: Path) -> None:
        path = transcript(tmp_path)  # modified just now
        store = SqliteStore(":memory:")
        store.migrate()
        report = KnowledgeIndexer(store).scan_root(
            RootConfig(path=tmp_path / "projects", kind="transcripts", depth=0)
        )
        assert report.too_recent == 1
        assert report.indexed == 0
        assert path.exists()
