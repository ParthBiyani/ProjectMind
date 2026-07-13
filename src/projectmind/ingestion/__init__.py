"""Turning repositories into episodic memory."""

from projectmind.ingestion.extractors import (
    DecisionExtractor,
    ExtractionContext,
    FailureExtractor,
    extract_all,
)
from projectmind.ingestion.git_source import Commit, LocalGitSource, discover_repositories
from projectmind.ingestion.github_source import GitHubSource, PullRequest
from projectmind.ingestion.pipeline import IngestionPipeline, IngestionReport, RepositoryResult

__all__ = [
    "Commit",
    "DecisionExtractor",
    "ExtractionContext",
    "FailureExtractor",
    "GitHubSource",
    "IngestionPipeline",
    "IngestionReport",
    "LocalGitSource",
    "PullRequest",
    "RepositoryResult",
    "discover_repositories",
    "extract_all",
]
