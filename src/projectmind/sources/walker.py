"""Finding the files worth reading, and the project each belongs to.

The walker is deliberately a whitelist. It names the kinds of file that carry
what someone knows (notes, docs, scripts, notebooks, logs, small result files,
documents) and ignores everything else, rather than trying to list everything
that must be avoided. Datasets, model weights, images and build output are never
opened, whatever they are called.

Read-only throughout: it lists directories and stats files. It does not touch
version control and does not write anywhere.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from projectmind.sources.config import RootConfig

#: Directories that are never entered.
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", ".env", "__pycache__",
        ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", ".next", ".nuxt",
        ".gradle", ".cache", "cache", "dist", "build", "target", "site-packages", "vendor",
        "dataset", "datasets", "data", "images", "imgs", "frames", "wandb", "lightning_logs",
        "checkpoints", "weights", "tmp", "temp", ".dart_tool", "pods", "intermediates",
        "$recycle.bin", "system volume information",
    }
)  # fmt: skip

#: Result files are only read from directories whose name says they hold results.
RESULT_DIRS = frozenset(
    {"experiments", "experiment", "working", "results", "runs", "logs", "output", "outputs"}
)

#: File names that are noise or sensitive, whatever their extension.
SKIP_NAMES = (
    "package.json", "package-lock.json", "tsconfig*.json", "*.min.*", ".env*", "credentials*",
    "secret*", "*.pem", "*.key", "requirements*.txt", "license*", "*.lock", "composer.json",
    "pubspec.lock", "*.map", "*.d.ts",
)  # fmt: skip

SOURCE_TYPES: dict[str, str] = {
    ".md": "doc", ".markdown": "doc", ".txt": "doc", ".rst": "doc",
    ".py": "code", ".ipynb": "notebook", ".log": "log", ".json": "results",
    ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx",
}  # fmt: skip

#: Per-type size ceilings, in bytes.
MAX_BYTES = {
    "doc": 2_000_000, "code": 1_000_000, "notebook": 15_000_000, "log": 200_000_000,
    "results": 200_000, "pdf": 30_000_000, "docx": 20_000_000, "pptx": 30_000_000,
}  # fmt: skip

#: Logs can be huge and the result is usually at the end: only this much is read.
LOG_TAIL_BYTES = 1_000_000


@dataclass(frozen=True, slots=True)
class Candidate:
    path: Path
    source_type: str
    size: int
    mtime: float
    mtime_ns: int


def classify(path: Path, size: int) -> str | None:
    """The source type of a file, or None if it should not be read."""
    name = path.name.lower()
    if any(fnmatch(name, pattern) for pattern in SKIP_NAMES):
        return None
    source_type = SOURCE_TYPES.get(path.suffix.lower())
    if source_type is None or size == 0 or size > MAX_BYTES[source_type]:
        return None
    if source_type == "results" and not any(part.lower() in RESULT_DIRS for part in path.parts):
        return None
    return source_type


def walk(root: Path) -> Iterator[Candidate]:
    """Every readable candidate under `root`, without entering skipped folders."""
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name.lower() not in SKIP_DIRS:
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    path = Path(entry.path)
                    source_type = classify(path, stat.st_size)
                    if source_type:
                        yield Candidate(
                            path, source_type, stat.st_size, stat.st_mtime, stat.st_mtime_ns
                        )
            except OSError:
                continue


class ProjectResolver:
    """Which project a file belongs to.

    The nearest enclosing git repository wins, using the same key the git
    ingestion gives that repository, so a repository's commits and its documents
    are one project. Otherwise the folder `depth` levels under the root is the
    project, and a file directly under the root belongs to the root itself.
    """

    def __init__(self, root: RootConfig) -> None:
        self.root = root
        self._repo_keys: dict[Path, str | None] = {}

    def key_for(self, path: Path) -> str:
        from projectmind.service import slugify

        folder = path.parent
        try:
            relative = path.relative_to(self.root.path)
        except ValueError:
            return slugify(self.root.name)

        repo = self._nearest_repo(folder)
        if repo is not None:
            return repo

        parts = relative.parts[:-1]
        if self.root.depth > 0 and len(parts) >= self.root.depth:
            return slugify(parts[self.root.depth - 1])
        return slugify(self.root.name)

    def _nearest_repo(self, folder: Path) -> str | None:
        base = self.root.path
        chain: list[Path] = []
        current = folder
        result: str | None
        while True:
            chain.append(current)
            if current in self._repo_keys:
                result = self._repo_keys[current]
                break
            if (current / ".git").exists():
                result = self._repo_key(current)
                break
            if current == base or current.parent == current:
                result = None
                break
            current = current.parent
        # Every folder walked on the way up shares the answer found at the top.
        for visited in chain:
            self._repo_keys[visited] = result
        return result

    @staticmethod
    def _repo_key(repo: Path) -> str:
        from projectmind.ingestion.git_source import LocalGitSource
        from projectmind.service import project_key

        return project_key(repo, LocalGitSource(repo).remote())
