"""The ingestion pipeline.

Sources produce commits, extractors produce records, this puts them in the
store with a fingerprint attached and an embedding computed. It is idempotent:
records are keyed by a content hash, so running it again over a repository that
gained three commits adds three records rather than three hundred duplicates.

Nothing here decides what is relevant. Ingestion is deliberately generous about
what it stores and the gate is deliberately strict about what it serves, because
a record you did not store cannot be retrieved later when it turns out to
matter, while a record you stored and never serve costs a few kilobytes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from projectmind.config import Settings, get_settings
from projectmind.fingerprint.scanner import ProjectScanner
from projectmind.ingestion.extractors import (
    DEFAULT_EXTRACTORS,
    TECH_LEXICON,
    ExtractionContext,
    extract_all,
)
from projectmind.ingestion.git_source import LocalGitSource, discover_repositories
from projectmind.ingestion.github_source import GitHubError, GitHubSource, is_excluded
from projectmind.logging import get_logger
from projectmind.models import EpisodicRecord, Fingerprint, Project, utcnow
from projectmind.service import project_key
from projectmind.storage import Store
from projectmind.storage.embeddings import EmbeddingProvider, get_embedder

log = get_logger(__name__)


@dataclass(slots=True)
class RepositoryResult:
    """What one repository contributed."""

    key: str
    path: str
    commits_read: int = 0
    records_extracted: int = 0
    records_stored: int = 0
    skipped_reason: str = ""

    @property
    def yield_rate(self) -> float:
        return self.records_extracted / self.commits_read if self.commits_read else 0.0


@dataclass(slots=True)
class IngestionReport:
    """What a whole run contributed."""

    repositories: list[RepositoryResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None

    @property
    def commits_read(self) -> int:
        return sum(item.commits_read for item in self.repositories)

    @property
    def records_extracted(self) -> int:
        return sum(item.records_extracted for item in self.repositories)

    @property
    def records_stored(self) -> int:
        return sum(item.records_stored for item in self.repositories)

    @property
    def skipped(self) -> list[RepositoryResult]:
        return [item for item in self.repositories if item.skipped_reason]

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or utcnow()
        return (end - self.started_at).total_seconds()

    def summary(self) -> str:
        return (
            f"{len(self.repositories) - len(self.skipped)} repositories, "
            f"{self.commits_read} commits read, "
            f"{self.records_extracted} records extracted, "
            f"{self.records_stored} new"
        )


class IngestionPipeline:
    """Reads repositories and writes episodic memory."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        embedder: EmbeddingProvider | None = None,
        scanner: ProjectScanner | None = None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.embedder = embedder or get_embedder()
        self.scanner = scanner or ProjectScanner()

    # --- local -------------------------------------------------------------
    def ingest_repository(
        self,
        root: str | Path,
        *,
        authors: Sequence[str] = (),
        since: datetime | None = None,
        max_commits: int = 2_000,
    ) -> RepositoryResult:
        """Read one repository and store what the extractors find."""
        path = Path(root).expanduser()
        key = project_key(path, None)
        result = RepositoryResult(key=key, path=str(path))

        if is_excluded(path.name, self.settings.excluded_repos):
            result.skipped_reason = "excluded by configuration"
            log.info("skipping excluded repository", extra={"repo": path.name})
            return result

        source = LocalGitSource(path, authors=authors, since=since, max_commits=max_commits)
        if not source.is_repository():
            result.skipped_reason = "not a git repository"
            return result

        commits = source.commits()
        result.commits_read = len(commits)
        if not commits:
            result.skipped_reason = "no commits matched the filters"
            return result

        remote = source.remote()
        project = self._register_project(path, remote)
        result.key = project.key

        context = ExtractionContext(
            project_id=project.id,
            project_key=project.key,
            remote=remote,
            vocabulary=self._vocabulary_for(project),
        )
        records = extract_all(commits, context, DEFAULT_EXTRACTORS)
        result.records_extracted = len(records)
        result.records_stored = self._store(records)

        log.info(
            "ingested repository",
            extra={
                "repo": project.key,
                "commits": result.commits_read,
                "extracted": result.records_extracted,
                "stored": result.records_stored,
            },
        )
        return result

    def ingest_tree(
        self,
        root: str | Path,
        *,
        authors: Sequence[str] = (),
        since: datetime | None = None,
        max_depth: int = 2,
        max_repositories: int = 0,
    ) -> IngestionReport:
        """Ingest every repository under a directory."""
        report = IngestionReport()
        repositories = discover_repositories(
            root, max_depth=max_depth, exclude=self.settings.excluded_repos
        )
        if max_repositories:
            repositories = repositories[:max_repositories]

        for repository in repositories:
            report.repositories.append(
                self.ingest_repository(repository, authors=authors, since=since)
            )
        report.finished_at = utcnow()
        log.info("ingestion run complete", extra={"summary": report.summary()})
        return report

    # --- github ------------------------------------------------------------
    def ingest_github(
        self,
        remotes: Sequence[str],
        *,
        limit_per_repo: int = 50,
    ) -> IngestionReport:
        """Ingest merged pull requests for the given remotes."""
        report = IngestionReport()
        source = GitHubSource(self.settings)
        if not source.is_configured:
            log.info("github ingestion skipped, no token configured")
            report.finished_at = utcnow()
            return report

        by_repo: dict[str, RepositoryResult] = {}
        try:
            for full_name, pull in source.iter_pull_requests(
                remotes, limit_per_repo=limit_per_repo
            ):
                key = project_key(Path(full_name.split("/")[-1]), None)
                result = by_repo.setdefault(
                    key, RepositoryResult(key=key, path=f"github:{full_name}")
                )
                project = self._register_project(
                    Path(full_name.split("/")[-1]), f"https://github.com/{full_name}"
                )
                context = ExtractionContext(
                    project_id=project.id,
                    project_key=project.key,
                    remote=pull.html_url.rsplit("/pull/", 1)[0] if pull.html_url else None,
                    vocabulary=self._vocabulary_for(project),
                )
                result.commits_read += 1
                # The pull request page is a better citation than a synthesised
                # commit url, so it replaces whatever the extractor produced.
                records = [
                    record.model_copy(update={"source_url": pull.html_url or record.source_url})
                    for record in extract_all([pull.as_commit()], context, DEFAULT_EXTRACTORS)
                ]
                result.records_extracted += len(records)
                result.records_stored += self._store(records)
        except GitHubError as exc:
            log.warning("github ingestion stopped", extra={"error": str(exc)})

        report.repositories = list(by_repo.values())
        report.finished_at = utcnow()
        return report

    # --- shared ------------------------------------------------------------
    def _register_project(self, path: Path, remote: str | None) -> Project:
        """Make sure the project exists with the best fingerprint available."""
        key = project_key(path, remote)
        existing = self.store.get_project(key)
        fingerprint = self.scanner.scan(path).fingerprint if path.is_dir() else None

        if fingerprint is None or fingerprint.is_empty:
            # Nothing scannable here: a GitHub-only repository, or a directory
            # with no manifests. Keep what is already known rather than
            # replacing a real fingerprint with an empty one.
            if existing is not None:
                return existing
            fingerprint = fingerprint or Fingerprint(git_remote=remote)

        return self.store.upsert_project(
            Project(
                key=key,
                name=path.name or key,
                root_path=str(path) if path.is_dir() else None,
                git_remote=remote,
                fingerprint=fingerprint,
                last_active=utcnow(),
            )
        )

    def _vocabulary_for(self, project: Project) -> frozenset[str]:
        """The shared lexicon plus whatever this project actually depends on.

        A project's own dependency names are the most reliable entity source it
        has: they are declared, spelled consistently, and unambiguous.
        """
        return (
            TECH_LEXICON
            | frozenset(project.fingerprint.dependencies)
            | frozenset(project.fingerprint.frameworks)
        )

    def _store(self, records: Sequence[EpisodicRecord]) -> int:
        if not records:
            return 0
        embedded = self._embed(records)
        return self.store.add_records(embedded)

    def _embed(self, records: Sequence[EpisodicRecord]) -> list[EpisodicRecord]:
        try:
            vectors = self.embedder.embed([record.searchable_text for record in records])
        except Exception as exc:
            # No embeddings means lexical-only search, which is degraded but
            # working. It is not a reason to lose the records.
            log.warning("embedding failed, storing without vectors", extra={"error": str(exc)})
            return list(records)
        return [
            record.model_copy(update={"embedding": tuple(vector)})
            for record, vector in zip(records, vectors, strict=False)
        ]
