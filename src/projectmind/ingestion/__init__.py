"""Turning repositories into episodic memory."""

from projectmind.ingestion.git_source import (
    Commit,
    LocalGitSource,
    discover_repositories,
)

__all__ = ["Commit", "LocalGitSource", "discover_repositories"]
