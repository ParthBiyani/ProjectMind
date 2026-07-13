"""Reading git history off the local disk.

No network, no token, no API rate limit. This is the primary source, not a
fallback: most of what a developer knows is in repositories that were never
pushed anywhere, and a memory layer that can only see GitHub cannot see those.

Everything here is read-only. `git log` is invoked with an explicit format and
parsed with unambiguous separators; nothing shells out to anything that could
modify a repository.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from projectmind.logging import get_logger

log = get_logger(__name__)

RECORD_SEPARATOR = "\x1e"
FIELD_SEPARATOR = "\x1f"
DEFAULT_MAX_COMMITS = 2_000
GIT_TIMEOUT = 60.0

_META_FORMAT = RECORD_SEPARATOR + FIELD_SEPARATOR.join(
    ["%H", "%P", "%an", "%ae", "%aI", "%D", "%s", "%b"]
)

_CONVENTIONAL = re.compile(
    r"^(?P<type>build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:\s*(?P<summary>.+)$",
    re.IGNORECASE,
)
_REVERT = re.compile(r'^revert\s+"?(?P<subject>.+?)"?$', re.IGNORECASE)
_MERGE_BRANCH = re.compile(
    r"^merge (?:branch|pull request #\d+ from|remote-tracking branch)\s+'?([^'\s]+)'?",
    re.IGNORECASE,
)
_TAG_REF = re.compile(r"tag:\s*([^,)]+)")


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit, with only the fields an extractor can actually use."""

    sha: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    authored_at: datetime
    refs: tuple[str, ...]
    subject: str
    body: str
    files: tuple[str, ...] = ()

    @property
    def short_sha(self) -> str:
        return self.sha[:8]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def is_root(self) -> bool:
        return not self.parents

    @property
    def message(self) -> str:
        return f"{self.subject}\n\n{self.body}".strip()

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(tag.strip() for ref in self.refs for tag in _TAG_REF.findall(ref))

    @property
    def conventional(self) -> tuple[str, str, str] | None:
        """(type, scope, summary) when the subject is a conventional commit."""
        match = _CONVENTIONAL.match(self.subject.strip())
        if not match:
            return None
        return (
            match.group("type").lower(),
            (match.group("scope") or "").lower(),
            match.group("summary").strip(),
        )

    @property
    def commit_type(self) -> str:
        conventional = self.conventional
        return conventional[0] if conventional else ""

    @property
    def summary(self) -> str:
        """The subject with any conventional prefix stripped."""
        conventional = self.conventional
        return conventional[2] if conventional else self.subject.strip()

    @property
    def reverted_subject(self) -> str | None:
        match = _REVERT.match(self.subject.strip())
        return match.group("subject").strip() if match else None

    @property
    def is_revert(self) -> bool:
        return self.reverted_subject is not None or self.commit_type == "revert"

    @property
    def merged_branch(self) -> str | None:
        match = _MERGE_BRANCH.match(self.subject.strip())
        if not match:
            return None
        branch = match.group(1).strip()
        return branch.removeprefix("origin/") or None

    @property
    def touched_directories(self) -> tuple[str, ...]:
        seen = {Path(name).parts[0] for name in self.files if name and len(Path(name).parts) > 1}
        return tuple(sorted(seen))


class GitError(RuntimeError):
    """Raised only by explicit callers; the source itself degrades quietly."""


def _run(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("git failed", extra={"root": str(root), "error": str(exc)})
        return None
    if result.returncode != 0:
        log.debug(
            "git returned non-zero",
            extra={"root": str(root), "args": args, "stderr": result.stderr[:200]},
        )
        return None
    return result.stdout


class LocalGitSource:
    """Commits from one repository on disk."""

    def __init__(
        self,
        root: str | Path,
        *,
        authors: Sequence[str] = (),
        since: datetime | None = None,
        max_commits: int = DEFAULT_MAX_COMMITS,
        include_files: bool = True,
    ) -> None:
        self.root = Path(root).expanduser()
        self.authors = tuple(author.lower() for author in authors)
        self.since = since
        self.max_commits = max_commits
        self.include_files = include_files

    # --- repository facts --------------------------------------------------
    def is_repository(self) -> bool:
        return (self.root / ".git").exists() and _run(
            self.root, "rev-parse", "--git-dir"
        ) is not None

    def remote(self) -> str | None:
        output = _run(self.root, "remote", "get-url", "origin")
        return output.strip() if output and output.strip() else None

    def commit_count(self) -> int:
        output = _run(self.root, "rev-list", "--count", "HEAD")
        try:
            return int((output or "0").strip())
        except ValueError:
            return 0

    def authors_seen(self) -> dict[str, int]:
        """Author emails and their commit counts, for picking a filter."""
        output = _run(self.root, "log", "--format=%ae", f"-n{self.max_commits}")
        counts: dict[str, int] = {}
        for line in (output or "").splitlines():
            email = line.strip().lower()
            if email:
                counts[email] = counts.get(email, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    # --- history -----------------------------------------------------------
    def commits(self) -> list[Commit]:
        """Every commit matching the filters, newest first.

        Two `git log` invocations rather than one: a commit body can contain
        anything, including the characters that would otherwise delimit the
        file list, so metadata and file names are read separately and joined on
        the sha. Guessing where a body ends is how ingestion silently loses
        half a repository.
        """
        if not self.is_repository():
            log.debug("not a git repository", extra={"root": str(self.root)})
            return []

        args = ["log", f"--format={_META_FORMAT}", f"-n{self.max_commits}"]
        if self.since:
            args.append(f"--since={self.since.date().isoformat()}")
        output = _run(self.root, *args)
        if output is None:
            return []

        commits = [
            parsed
            for chunk in output.split(RECORD_SEPARATOR)
            if chunk.strip()
            if (parsed := self._parse(chunk)) is not None
        ]
        if self.authors:
            commits = [c for c in commits if c.author_email.lower() in self.authors]
        if self.include_files and commits:
            files = self._files_by_sha()
            commits = [replace(commit, files=files.get(commit.sha, ())) for commit in commits]
        return commits

    def __iter__(self) -> Iterator[Commit]:
        return iter(self.commits())

    def _parse(self, chunk: str) -> Commit | None:
        fields = chunk.lstrip("\n").split(FIELD_SEPARATOR)
        if len(fields) < 8:
            return None
        sha, parents, name, email, authored, refs, subject, body = fields[:8]
        try:
            timestamp = datetime.fromisoformat(authored.strip())
        except ValueError:
            return None
        return Commit(
            sha=sha.strip(),
            parents=tuple(p for p in parents.split() if p),
            author_name=name.strip(),
            author_email=email.strip(),
            authored_at=timestamp,
            refs=tuple(part.strip() for part in refs.split(",") if part.strip()),
            subject=subject.strip(),
            body=body.strip(),
        )

    def _files_by_sha(self) -> dict[str, tuple[str, ...]]:
        args = [
            "log",
            f"--format={RECORD_SEPARATOR}%H",
            "--name-only",
            f"-n{self.max_commits}",
        ]
        if self.since:
            args.append(f"--since={self.since.date().isoformat()}")
        output = _run(self.root, *args)
        if output is None:
            return {}

        mapping: dict[str, tuple[str, ...]] = {}
        for chunk in output.split(RECORD_SEPARATOR):
            lines = [line.strip() for line in chunk.splitlines() if line.strip()]
            if not lines:
                continue
            mapping[lines[0]] = tuple(lines[1:])
        return mapping


def discover_repositories(
    root: str | Path,
    *,
    max_depth: int = 2,
    exclude: Sequence[str] = (),
) -> list[Path]:
    """Every git repository under `root`, without walking into their contents.

    Stops descending as soon as a `.git` directory is found, so a repository
    containing vendored submodules is one result rather than forty.
    """
    base = Path(root).expanduser()
    excluded = {pattern.lower() for pattern in exclude}
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if (directory / ".git").exists():
            if directory.name.lower() not in excluded:
                found.append(directory)
            return
        try:
            children = [child for child in directory.iterdir() if child.is_dir()]
        except OSError:
            return
        for child in sorted(children):
            if child.name.startswith(".") or child.name.lower() in excluded:
                continue
            walk(child, depth + 1)

    walk(base, 0)
    return found
