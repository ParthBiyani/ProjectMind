"""The front door, transport-independent.

Everything that answers `get_context` lives here rather than in the MCP layer,
for one reason: the MCP server, the CLI and the eval harness must take exactly
the same code path. The moment retrieval logic leaks into a transport adapter,
the number the eval prints stops describing what the agent actually receives.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import UUID

from projectmind import entities as entity_matching
from projectmind.config import Settings, get_settings
from projectmind.logging import get_logger
from projectmind.models import ContextBundle, Fingerprint, Project, utcnow
from projectmind.profile.repository import ProfileRepository, ProfileSlice
from projectmind.storage import Store, open_store
from projectmind.storage.base import BundleLogEntry, UsageStats

log = get_logger(__name__)

_GIT_REMOTE_TIMEOUT = 2.0


@dataclass(slots=True)
class ResolvedProject:
    """Who is asking. In Phase 1 this is identity only; the fingerprint is empty."""

    key: str
    name: str
    root: Path | None
    git_remote: str | None
    fingerprint: Fingerprint

    @property
    def is_known(self) -> bool:
        return not self.fingerprint.is_empty


class MemoryService:
    """Reads memory and answers questions about it. Owns no transport."""

    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.store = store
        self.profile = ProfileRepository(store, self.settings)

    @classmethod
    def open(cls, settings: Settings | None = None) -> MemoryService:
        settings = settings or get_settings()
        return cls(open_store(settings), settings)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> MemoryService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- the front door ----------------------------------------------------
    def get_context(
        self,
        prompt: str,
        project_path: str | Path | None = None,
        *,
        ignore_profile: bool = False,
        max_tokens: int | None = None,
        now: datetime | None = None,
        log_bundle: bool = True,
    ) -> ContextBundle:
        """Return the smallest bundle that improves the next action.

        An empty bundle is a correct answer and is logged as one, because
        "chose not to inject" is a decision that has to be measurable.
        """
        started = time.perf_counter()
        moment = now or utcnow()
        project = self.resolve_project(project_path)
        prompt_entities = self.extract_entities(prompt)

        if ignore_profile:
            bundle = ContextBundle(
                generated_at=moment,
                project_key=project.key,
                entities=prompt_entities,
                gate_reason="profile suppressed by the caller",
            )
        else:
            slice_ = self.profile.select(
                fingerprint=project.fingerprint,
                entities=prompt_entities,
                now=moment,
                token_budget=self._profile_budget(max_tokens),
            )
            bundle = self._bundle_from_profile(slice_, project, prompt_entities, moment)

        bundle = bundle.model_copy(
            update={"latency_ms": round((time.perf_counter() - started) * 1000.0, 3)}
        )
        if log_bundle:
            self._log(bundle, prompt)
        return bundle

    def _bundle_from_profile(
        self,
        slice_: ProfileSlice,
        project: ResolvedProject,
        prompt_entities: tuple[str, ...],
        moment: datetime,
    ) -> ContextBundle:
        if slice_.is_empty:
            reason = (
                "no active profile statements"
                if slice_.considered == 0
                else "no profile statement cleared the relevance floor"
            )
        else:
            reason = f"profile slice: {len(slice_.statements)} of {slice_.considered} statements"
        return ContextBundle(
            generated_at=moment,
            project_key=project.key,
            profile=slice_.served(),
            profile_tokens=slice_.tokens,
            entities=prompt_entities,
            gate_reason=reason,
        )

    def _profile_budget(self, max_tokens: int | None) -> int:
        if max_tokens is None:
            return self.settings.profile_token_cap
        return min(self.settings.profile_token_cap, max(0, max_tokens))

    # --- supporting reads --------------------------------------------------
    def extract_entities(self, prompt: str) -> tuple[str, ...]:
        """Match the prompt against the entities memory actually holds."""
        vocabulary = entity_matching.build_vocabulary(
            *(statement.entities for statement in self.profile.active())
        )
        return entity_matching.extract(prompt, vocabulary)

    def resolve_project(self, project_path: str | Path | None) -> ResolvedProject:
        """Identify the calling project and record that it was seen.

        Phase 1 establishes identity only. Phase 2 attaches a real fingerprint
        computed from manifests; until then `fingerprint` is empty and profile
        selection falls back to stack-agnostic statements.
        """
        if project_path is None:
            return ResolvedProject("unknown", "unknown", None, None, Fingerprint())

        root = Path(project_path).expanduser()
        remote = _git_remote(root)
        key = project_key(root, remote)
        stored = self.store.get_project(key)
        fingerprint = stored.fingerprint if stored else Fingerprint(git_remote=remote)

        self.store.upsert_project(
            Project(
                key=key,
                name=root.name or key,
                root_path=str(root),
                git_remote=remote,
                fingerprint=fingerprint,
                last_active=utcnow(),
            )
        )
        return ResolvedProject(key, root.name or key, root, remote, fingerprint)

    # --- telemetry ---------------------------------------------------------
    def feedback(self, bundle_id: UUID, *, useful: bool, note: str = "") -> bool:
        recorded = self.store.record_feedback(bundle_id, useful=useful, note=note)
        if recorded:
            log.info("feedback recorded", extra={"bundle": str(bundle_id), "useful": useful})
        return recorded

    def stats(self, *, since: datetime | None = None) -> UsageStats:
        return self.store.usage_stats(since=since)

    def _log(self, bundle: ContextBundle, prompt: str) -> None:
        try:
            self.store.log_bundle(
                BundleLogEntry(
                    bundle_id=bundle.bundle_id,
                    created_at=bundle.generated_at,
                    project_key=bundle.project_key,
                    task_type=str(bundle.task_type) if bundle.task_type else None,
                    prompt_hash=hash_prompt(prompt),
                    prompt_words=len(prompt.split()),
                    profile_ids=tuple(item.id for item in bundle.profile),
                    episodic_ids=tuple(item.id for item in bundle.episodic),
                    cross_project_ids=tuple(
                        item.id for item in bundle.episodic if item.cross_project
                    ),
                    profile_tokens=bundle.profile_tokens,
                    episodic_tokens=bundle.episodic_tokens,
                    gate_reason=bundle.gate_reason,
                    latency_ms=bundle.latency_ms,
                    injected=not bundle.is_empty,
                )
            )
        except Exception as exc:
            # Telemetry is never allowed to break a retrieval.
            log.warning("could not log bundle", extra={"error": str(exc)})


def hash_prompt(prompt: str) -> str:
    """Stable digest of a prompt. The prompt itself is never stored."""
    return hashlib.sha256(prompt.strip().lower().encode("utf-8")).hexdigest()[:16]


def project_key(root: Path, git_remote: str | None) -> str:
    """A stable slug for a project.

    Derived from the git remote when there is one, so the same repository
    cloned to two directories is one project rather than two. Falls back to the
    directory name, which is what most of `D:/Project Eris` looks like.
    """
    if git_remote:
        cleaned = git_remote.removesuffix(".git").rstrip("/")
        tail = cleaned.rsplit("/", 2)[-2:]
        if len(tail) == 2 and tail[0]:
            return slugify(f"{tail[0]}-{tail[1]}")
        return slugify(cleaned.rsplit("/", 1)[-1])
    return slugify(root.name)


def slugify(value: str) -> str:
    cleaned = "".join(char if char.isalnum() else "-" for char in value.lower())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or "unknown"


def _git_remote(root: Path) -> str | None:
    """`git remote get-url origin`, or None. Never raises, never blocks long."""
    if not root.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_REMOTE_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    remote = result.stdout.strip()
    return remote or None
