"""A scripted tour of ProjectMind on a workspace built for the purpose.

Nothing here touches your real memory. The demo creates a small, entirely
fictional workspace in a temporary folder (three projects, a git history, a
recorded session and a captured web page), indexes it with the real code, and
asks seven questions through the real front door. It is also a regression test:
`run_demo` returns what each step did, and the test suite asserts it.

The seven questions are chosen to show the behaviour that matters most, including
the two that should produce nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from projectmind import capture
from projectmind.config import Settings
from projectmind.service import MemoryService
from projectmind.sources.browser import BrowserConfig
from projectmind.sources.config import RootConfig, SourcesConfig
from projectmind.storage import SqliteStore
from projectmind.sync import sync

HARBOR_NOTES = """# Harbor Vision

## Score definition

The scoring formula is 10 * (0.55*Dice + 0.25*boundary_F1 + 0.10*volume + 0.10*centroid).
Boundary F1 uses exact voxel matching, with no tolerance band.

## What worked

A patch-based 3D U-Net reached 0.62 cross-validated Dice. Keeping only the largest
connected component per mask added 0.03, because every training mask had exactly one.
"""

LEDGER_NOTES = """# Ledger Desk

## Indicators

The ADX block returns three series: plus DI, minus DI and ADX itself, as a tuple.
Use a period of 14 unless the strategy file says otherwise.
"""

ECHO_DECISIONS = """# Echo Notes: decisions

## Provider client

When the provider rate limits us (HTTP 429), the client backs off exponentially with
jitter, capped at 30 seconds, and gives up after five attempts. A fixed delay made every
worker retry together.
"""

SESSION = [
    {
        "type": "ai-title",
        "aiTitle": "Choosing the search index for notes",
    },
    {
        "type": "user",
        "cwd": "{ledger}",
        "timestamp": "2026-09-12T10:05:00Z",
        "message": {
            "role": "user",
            "content": "Should the notes search use a vector database or something simpler?",
        },
    },
    {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Use SQLite FTS5 with BM25. Exact technical terms are the most "
                    "reliable signal in these notes, it needs no server, and it can be "
                    "inspected with a single SQL query. Add vectors only if measurements "
                    "show a gap.",
                }
            ],
        },
    },
]

WEB_PAGE = (
    "Reciprocal rank fusion combines several ranked lists into one ordering. Each document "
    "receives the sum of 1/(k + rank) over the lists it appears in, so a document ranked well "
    "by several retrievers rises above one ranked first by only one. The constant k damps the "
    "influence of top ranks; a typical value is sixty."
)

COMMITS = [
    ("add the ADX block and the oscillator bars", "ta.py", "def adx(): pass\n"),
    (
        "drop the profit factor column from the variations table",
        "report.py",
        "COLUMNS = ['return', 'drawdown']\n",
    ),
]


@dataclass(frozen=True, slots=True)
class Step:
    title: str
    prompt: str
    where: str
    shows: str
    expect_silence: bool = False


STEPS = (
    Step("A mechanical edit", "Rename this variable to response", "ledger-desk",
         "trivial prompts are skipped before anything is searched"),
    Step("Your own notes, cited", "What is the scoring formula for the harbor vision task?",
         "harbor-vision", "an excerpt copied from your notes, with the file and line numbers"),
    Step("Your own git history", "Why did we drop the profit factor column?", "ledger-desk",
         "commits are searchable; a question about your project needs no keyword list"),
    Step("Knowledge from another project",
         "How should the client back off when the provider rate limits us?", "pulse-api",
         "something worked out in a different project, found because it is relevant"),
    Step("What was decided in a session",
         "What did we decide about the search index for the notes?", "ledger-desk",
         "a past conversation, cited by turn"),
    Step("What you read", "How does reciprocal rank fusion merge two result lists?", "pulse-api",
         "a page read in the browser, captured by the extension"),
    Step("Nothing to say", "Tune the Kubernetes ingress certificate rotation", "pulse-api",
         "no passage covers the question, so nothing is served", expect_silence=True),
)  # fmt: skip


@dataclass(slots=True)
class StepResult:
    step: Step
    skipped: bool
    reason: str
    citations: tuple[str, ...]
    excerpts: tuple[str, ...]
    tokens: int
    latency_ms: float

    @property
    def silent(self) -> bool:
        return not self.citations


def _write(path: Path, text: str, *, old: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if old:  # files younger than 30 minutes are deliberately not read
        stamp = path.stat().st_mtime - 7_200
        os.utime(path, (stamp, stamp))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def build_workspace(base: Path) -> dict[str, Path]:
    """The fictional workspace, on disk. Returns each project's directory."""
    work = base / "work"
    harbor = work / "harbor-vision"
    ledger = work / "ledger-desk"
    echo = work / "echo-notes"
    pulse = work / "pulse-api"
    _write(harbor / "CLAUDE.md", HARBOR_NOTES)
    _write(ledger / "NOTES.md", LEDGER_NOTES)
    _write(echo / "DECISIONS.md", ECHO_DECISIONS)
    pulse.mkdir(parents=True, exist_ok=True)

    ledger.mkdir(parents=True, exist_ok=True)
    _git(ledger, "init", "-q", "-b", "main")
    _git(ledger, "config", "user.email", "demo@example.invalid")
    _git(ledger, "config", "user.name", "Demo")
    _git(ledger, "config", "commit.gpgsign", "false")
    for subject, name, body in COMMITS:
        (ledger / name).write_text(body, encoding="utf-8")
        _git(ledger, "add", "-A")
        _git(
            ledger,
            "commit",
            "-q",
            "-m",
            f"fix: {subject}" if "drop" in subject else f"feat: {subject}",
        )

    sessions = base / "sessions" / "demo"
    sessions.mkdir(parents=True)
    lines = [
        json.dumps(record).replace("{ledger}", str(ledger).replace("\\", "/")) for record in SESSION
    ]
    _write(sessions / "session.jsonl", "\n".join(lines) + "\n")
    return {"work": work, "sessions": base / "sessions", "harbor": harbor, "ledger": ledger,
            "echo": echo, "pulse": pulse}  # fmt: skip


@contextmanager
def demo_memory() -> Iterator[tuple[MemoryService, dict[str, Path]]]:
    """A fully indexed throwaway ProjectMind over the fictional workspace."""
    base = Path(tempfile.mkdtemp(prefix="projectmind-demo-"))
    try:
        paths = build_workspace(base)
        home = base / "home"
        home.mkdir()
        BrowserConfig(enabled=True).save(home)
        SourcesConfig(
            roots=[
                RootConfig(path=paths["work"], kind="project", depth=1),
                RootConfig(path=paths["sessions"], kind="transcripts", depth=0),
            ]
        ).save(home)
        settings = Settings(
            home=home, db_url=None, authors=("demo@example.invalid",), embedding_provider="hashing"
        )
        store = SqliteStore(home / "demo.db")
        store.migrate()
        memory = MemoryService(store, settings)
        try:
            # browser=False: the demo must never read the real browser history.
            sync(store, settings, browser=False, min_age_seconds=1_800.0)
            capture.handle_page(
                memory,
                {
                    "url": "https://www.postgresql.org/docs/current/textsearch-rank.html?x=1",
                    "title": "Ranking search results",
                    "text": WEB_PAGE,
                },
            )
            yield memory, paths
        finally:
            memory.close()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def project_dir(paths: dict[str, Path], name: str) -> Path:
    return {
        "harbor-vision": paths["harbor"],
        "ledger-desk": paths["ledger"],
        "pulse-api": paths["pulse"],
        "echo-notes": paths["echo"],
    }[name]


def run_demo(memory: MemoryService, paths: dict[str, Path]) -> list[StepResult]:
    results: list[StepResult] = []
    for step in STEPS:
        started = time.perf_counter()
        bundle = memory.get_context(step.prompt, project_dir(paths, step.where), log_bundle=False)
        elapsed = (time.perf_counter() - started) * 1000.0
        # Show paths relative to the workspace; the temporary folder name is noise.
        prefix = paths["work"].parent.as_posix() + "/"
        citations = [item.citation.replace(prefix, "") for item in bundle.evidence] + [
            item.source_url for item in bundle.episodic
        ]
        excerpts = [item.excerpt for item in bundle.evidence] + [
            item.what for item in bundle.episodic
        ]
        results.append(
            StepResult(
                step=step,
                skipped="trivial" in bundle.gate_reason,
                reason=bundle.gate_reason,
                citations=tuple(citations),
                excerpts=tuple(excerpts),
                tokens=bundle.total_tokens,
                latency_ms=round(elapsed, 1),
            )
        )
    return results
