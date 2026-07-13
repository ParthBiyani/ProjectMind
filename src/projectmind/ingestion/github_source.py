"""Reading history from the GitHub API.

A secondary source. Local git covers everything on disk and needs no token;
this covers repositories that are not cloned here, and adds the one thing a
clone does not carry — pull request descriptions, which are where people
actually write down why.

Two safeguards matter more than the feature itself:

* **The exclusion list is checked before any request is made.** A repository
  named in `PROJECTMIND_EXCLUDED_REPOS` is never fetched, never parsed and
  never stored. Excluding after the fact is not excluding.
* **No content leaves the machine.** This module only reads. Nothing here posts,
  and the extraction model, when one is configured at all, is called by the
  pipeline rather than from inside the fetch.
"""

from __future__ import annotations

import fnmatch
import json
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from projectmind.config import Settings, get_settings
from projectmind.ingestion.git_source import Commit
from projectmind.logging import get_logger

log = get_logger(__name__)

API_ROOT = "https://api.github.com"
USER_AGENT = "projectmind"
PAGE_SIZE = 100
REQUEST_TIMEOUT = 20.0


class GitHubError(RuntimeError):
    """A request failed in a way the caller should know about."""


@dataclass(frozen=True, slots=True)
class PullRequest:
    """A merged pull request, which is a decision with a paragraph attached."""

    number: int
    title: str
    body: str
    merged_at: datetime
    html_url: str
    head_ref: str
    base_ref: str
    author: str

    def as_commit(self) -> Commit:
        """Adapt to `Commit` so the same extractors apply.

        A merged pull request is structurally a merge commit with a much better
        message, so it goes through the existing extractors rather than a
        parallel set that would drift from them.
        """
        return Commit(
            sha=f"pr-{self.number}",
            parents=("a", "b"),
            author_name=self.author,
            author_email="",
            authored_at=self.merged_at,
            refs=(),
            subject=f"Merge pull request #{self.number} from {self.head_ref}",
            body=f"{self.title}\n\n{self.body}".strip(),
        )


def is_excluded(name: str, patterns: Sequence[str]) -> bool:
    """Glob match on the repository name or `owner/name`."""
    lowered = name.lower()
    tail = lowered.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(lowered, pattern.lower()) or fnmatch.fnmatch(tail, pattern.lower())
        for pattern in patterns
    )


def parse_repo(remote: str) -> tuple[str, str] | None:
    """`owner, name` from any of the usual remote spellings."""
    cleaned = remote.strip().removesuffix(".git")
    for prefix in (
        "git@github.com:",
        "ssh://git@github.com/",
        "https://github.com/",
        "http://github.com/",
    ):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    else:
        return None
    parts = [part for part in cleaned.split("/") if part]
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


class GitHubSource:
    """Read-only access to one account's repositories."""

    def __init__(self, settings: Settings | None = None, *, token: str | None = None) -> None:
        self.settings = settings or get_settings()
        self.token = token or self.settings.github_token

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    def _get(self, path: str, **params: Any) -> Any:
        if not self.token:
            raise GitHubError("no GITHUB_TOKEN is set; local git ingestion needs none")

        query = "&".join(f"{key}={value}" for key, value in params.items() if value is not None)
        url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": USER_AGENT,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and "rate limit" in exc.read().decode("utf-8", "replace").lower():
                raise GitHubError("github rate limit reached; try again later") from exc
            raise GitHubError(f"github returned {exc.code} for {path}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GitHubError(f"could not reach github: {exc}") from exc

    def repositories(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Repositories the token can see, most recently pushed first."""
        payload = self._get(
            "/user/repos", per_page=min(PAGE_SIZE, limit), sort="pushed", affiliation="owner"
        )
        repos = payload if isinstance(payload, list) else []
        excluded = self.settings.excluded_repos
        kept = [repo for repo in repos if not is_excluded(str(repo.get("full_name", "")), excluded)]
        if len(kept) != len(repos):
            log.info("skipped excluded repositories", extra={"count": len(repos) - len(kept)})
        return kept[:limit]

    def merged_pull_requests(
        self,
        owner: str,
        name: str,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> list[PullRequest]:
        """Merged pull requests, newest first.

        Only merged ones. An open or closed-unmerged pull request is a proposal,
        not a decision, and remembering proposals as decisions is how memory
        starts telling you that you use things you rejected.
        """
        if is_excluded(f"{owner}/{name}", self.settings.excluded_repos):
            log.info("repository is excluded", extra={"repo": f"{owner}/{name}"})
            return []

        payload = self._get(
            f"/repos/{owner}/{name}/pulls",
            state="closed",
            per_page=min(PAGE_SIZE, limit),
            sort="updated",
            direction="desc",
        )
        found: list[PullRequest] = []
        for item in payload if isinstance(payload, list) else []:
            merged_at = item.get("merged_at")
            if not merged_at:
                continue
            merged = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00")).astimezone(UTC)
            if since and merged < since:
                continue
            found.append(
                PullRequest(
                    number=int(item.get("number", 0)),
                    title=str(item.get("title") or ""),
                    body=str(item.get("body") or "")[:4000],
                    merged_at=merged,
                    html_url=str(item.get("html_url") or ""),
                    head_ref=str((item.get("head") or {}).get("ref") or ""),
                    base_ref=str((item.get("base") or {}).get("ref") or ""),
                    author=str((item.get("user") or {}).get("login") or ""),
                )
            )
        return found[:limit]

    def iter_pull_requests(
        self, remotes: Sequence[str], *, limit_per_repo: int = 50
    ) -> Iterator[tuple[str, PullRequest]]:
        """Pull requests across several remotes, skipping anything excluded."""
        for remote in remotes:
            parsed = parse_repo(remote)
            if parsed is None:
                log.debug("not a github remote", extra={"remote": remote})
                continue
            owner, name = parsed
            try:
                for pull in self.merged_pull_requests(owner, name, limit=limit_per_repo):
                    yield f"{owner}/{name}", pull
            except GitHubError as exc:
                log.warning("skipping repository", extra={"repo": name, "error": str(exc)})
