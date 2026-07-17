"""Walking a project and turning it into a fingerprint.

Cheap and deterministic, with no model involved, because this runs on the first
call for every project and has to be affordable. The walk is bounded in depth
and in file count, skips the directories that hold other people's code, and
stops early on anything that looks like a monorepo of vendored dependencies.

The output is the `manifest_hash`, which is what makes caching safe: it is a
content hash of every manifest found, so the cache invalidates exactly when the
dependency set could have changed and not on every unrelated edit.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from projectmind.fingerprint.manifests import (
    IGNORED_DIRECTORIES,
    ManifestSummary,
    is_manifest,
    summarise,
)
from projectmind.logging import get_logger
from projectmind.models import Fingerprint, utcnow

log = get_logger(__name__)

DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_FILES = 8_000
README_CHARS = 4_000
GIT_TIMEOUT = 2.0

#: Directories that hold data rather than code. Skipped outright: a manifest is
#: never inside one, and a dataset folder is what turns a 40 ms scan into a
#: three-second one. Measured on a local computer-vision repo, walking these
#: accounted for 24,800 of 25,000 files seen.
DATA_DIRECTORIES = frozenset(
    {
        "data",
        "datasets",
        "dataset",
        "raw",
        "raw_data",
        "images",
        "imgs",
        "annotations",
        "labels",
        "checkpoints",
        "weights",
        "runs",
        "wandb",
        "mlruns",
        "artifacts",
        "outputs",
        "logs",
        "samples",
    }
)

#: A domain needs this many distinct keyword hits before it is claimed. One hit
#: is noise: "model" appears in almost every README ever written.
MIN_DOMAIN_HITS = 2

#: At most this many hints. A project that looks like eight domains looks like
#: none of them.
MAX_DOMAIN_HINTS = 3

#: Domain vocabulary, matched against the project name and README on word
#: boundaries. Closed on purpose: an open extractor would tag every repository
#: "data" and "system" and the hint would stop discriminating between anything.
DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "machine-learning": ("machine learning", "model", "training", "dataset", "inference", "ml"),
    "computer-vision": ("detection", "segmentation", "image", "camera", "opencv", "yolo", "ocr"),
    "nlp": ("nlp", "language model", "text classification", "summariz", "transformer", "token"),
    "time-series": ("forecast", "time series", "backtest", "seasonal", "arima"),
    "trading": ("trading", "portfolio", "broker", "market", "ticker", "etf"),
    "healthcare": ("clinical", "patient", "medical", "diagnos", "health", "blood", "cardiac"),
    "education": ("course", "student", "placement", "exam", "learning path", "quiz"),
    "mobile": ("android", "ios", "mobile app", "flutter", "react native"),
    "web": ("website", "dashboard", "frontend", "landing page", "portfolio site"),
    "devtools": ("cli", "developer tool", "linter", "plugin", "extension", "agent"),
    "finance": ("expense", "budget", "invoice", "payment", "spend"),
    "iot": ("sensor", "arduino", "raspberry", "embedded", "firmware"),
    "audio": ("audio", "speech", "transcri", "voice", "whisper"),
    "robotics": ("robot", "slam", "actuator", "kinematic"),
}

README_NAMES = ("readme.md", "readme.rst", "readme.txt", "readme")


@dataclass(slots=True)
class ScanResult:
    """A fingerprint plus what produced it, for diagnostics."""

    fingerprint: Fingerprint
    manifests: ManifestSummary
    files_seen: int
    truncated: bool

    @property
    def is_useful(self) -> bool:
        return not self.fingerprint.is_empty


class ProjectScanner:
    """Turns a directory into a :class:`Fingerprint`."""

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_files: int = DEFAULT_MAX_FILES,
        read_git_remote: bool = True,
    ) -> None:
        self.max_depth = max_depth
        self.max_files = max_files
        self.read_git_remote = read_git_remote

    def scan(self, root: str | Path) -> ScanResult:
        path = Path(root).expanduser()
        if not path.is_dir():
            log.debug("cannot scan a non-directory", extra={"path": str(path)})
            return ScanResult(Fingerprint(), ManifestSummary(), 0, False)

        manifest_paths, shape, extensions, files_seen, truncated = self._walk(path)
        manifests = summarise(manifest_paths)
        remote = self._git_remote(path) if self.read_git_remote else None

        fingerprint = Fingerprint(
            languages=tuple(manifests.languages) or self._languages_from_extensions(extensions),
            frameworks=tuple(manifests.frameworks),
            dependencies=tuple(manifests.dependencies),
            domain_hints=self._domain_hints(path),
            manifest_hash=self._manifest_hash(path, manifests.files),
            manifest_files=tuple(sorted(str(file.relative_to(path)) for file in manifests.files)),
            directory_shape=shape,
            file_count=files_seen,
            git_remote=remote,
            computed_at=utcnow(),
        )
        return ScanResult(fingerprint, manifests, files_seen, truncated)

    # --- the walk ----------------------------------------------------------
    def _walk(self, root: Path) -> tuple[list[Path], dict[str, int], Counter[str], int, bool]:
        manifest_paths: list[Path] = []
        shape: Counter[str] = Counter()
        extensions: Counter[str] = Counter()
        files_seen = 0
        truncated = False

        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue

            for entry in entries:
                if files_seen >= self.max_files:
                    truncated = True
                    stack.clear()
                    break
                try:
                    if entry.is_dir():
                        name = entry.name.lower()
                        if (
                            name in IGNORED_DIRECTORIES
                            or name in DATA_DIRECTORIES
                            or entry.name.startswith(".")
                        ):
                            continue
                        if depth < self.max_depth:
                            stack.append((entry, depth + 1))
                        continue
                except OSError:
                    continue

                files_seen += 1
                if depth == 0:
                    shape["<root>"] += 1
                else:
                    top = entry.relative_to(root).parts[0]
                    shape[top] += 1
                suffix = entry.suffix.lower()
                if suffix:
                    extensions[suffix] += 1
                if is_manifest(entry):
                    manifest_paths.append(entry)

        if truncated:
            log.info(
                "stopped scanning early",
                extra={"path": str(root), "limit": self.max_files},
            )
        return manifest_paths, dict(shape.most_common(20)), extensions, files_seen, truncated

    # --- components --------------------------------------------------------
    @staticmethod
    def _languages_from_extensions(extensions: Counter[str]) -> tuple[str, ...]:
        """Fallback for projects with no manifest at all.

        `D:/Project Eris` is full of these: a folder of notebooks and scripts
        with nothing declaring what they need. Extensions are a weaker signal
        than a manifest but far better than an empty fingerprint.
        """
        by_extension = {
            ".py": "python",
            ".ipynb": "python",
            ".dart": "dart",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".js": "javascript",
            ".jsx": "javascript",
            ".rs": "rust",
            ".go": "go",
            ".java": "java",
            ".kt": "kotlin",
            ".rb": "ruby",
            ".cs": "csharp",
            ".cpp": "cpp",
            ".c": "c",
            ".swift": "swift",
            ".r": "r",
            ".sql": "sql",
        }
        counts: Counter[str] = Counter()
        for suffix, count in extensions.items():
            language = by_extension.get(suffix)
            if language:
                counts[language] += count
        # Only languages with a real presence, to avoid one stray file counting.
        threshold = max(2, sum(counts.values()) // 20)
        return tuple(sorted(name for name, count in counts.items() if count >= threshold))

    def _domain_hints(self, root: Path) -> tuple[str, ...]:
        """Domains the project name and README agree on.

        Requires several distinct keyword hits and caps the result. A single
        loose match makes every repository look like machine learning: on a
        local placement-prep app, one-hit matching claimed eight domains
        including robotics, from the words "robot" and "raspberry".
        """
        haystack = root.name.lower()
        readme = self._read_readme(root)
        if readme:
            haystack = f"{haystack}\n{readme.lower()}"

        scored: list[tuple[int, str]] = []
        for domain, keywords in DOMAIN_KEYWORDS.items():
            hits = sum(1 for keyword in keywords if _mentions(haystack, keyword))
            if hits >= MIN_DOMAIN_HITS:
                scored.append((hits, domain))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(sorted(domain for _, domain in scored[:MAX_DOMAIN_HINTS]))

    @staticmethod
    def _read_readme(root: Path) -> str | None:
        """Find the README whatever it is capitalised as.

        Guessing spellings does not work. The previous version tried
        `readme.md` and then `README.MD`, which finds `README.md` on Windows
        only because the filesystem is case-insensitive and finds nothing at
        all on Linux or macOS — so domain hints silently never worked there.
        Caught by CI on ubuntu, which is exactly what the matrix is for.
        Listing the directory once and matching lowercased is both correct and
        cheaper than four stat calls.
        """
        try:
            entries = {entry.name.lower(): entry for entry in root.iterdir() if entry.is_file()}
        except OSError:
            return None

        for name in README_NAMES:
            candidate = entries.get(name)
            if candidate is not None:
                try:
                    return candidate.read_text(encoding="utf-8", errors="replace")[:README_CHARS]
                except OSError:
                    return None
        return None

    @staticmethod
    def _manifest_hash(root: Path, manifests: list[Path]) -> str:
        relatives = []
        for manifest in manifests:
            try:
                relatives.append(str(manifest.relative_to(root)))
            except ValueError:
                relatives.append(str(manifest))
        return manifest_digest(root, relatives) or ""

    @staticmethod
    def _git_remote(root: Path) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() or None


def scan(root: str | Path, **options: object) -> Fingerprint:
    """Convenience wrapper: scan a directory and return just the fingerprint."""
    scanner = ProjectScanner(**options)  # type: ignore[arg-type]
    return scanner.scan(root).fingerprint


def manifest_digest(root: Path, relative_paths: Iterable[str]) -> str | None:
    """Content hash over a known set of manifests.

    Shared by the scanner and the cache so the two can never disagree about
    what a fingerprint's `manifest_hash` means. Returns None when a listed file
    has gone, which the cache reads as "rescan".

    Hashing paths alone would miss a dependency being added; hashing the whole
    tree would invalidate on every source edit. Manifest contents are exactly
    the input the fingerprint is derived from.
    """
    digest = hashlib.sha256()
    for relative in sorted(str(item).replace("\\", "/") for item in relative_paths):
        path = root / relative
        digest.update(relative.encode("utf-8"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            return None
    return digest.hexdigest()[:32]


def _mentions(haystack: str, keyword: str) -> bool:
    """Word-boundary match, so `ml` does not fire inside `html`."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}", haystack) is not None
