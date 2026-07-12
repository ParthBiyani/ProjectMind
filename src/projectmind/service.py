"""The front door, transport-independent.

Everything that answers `get_context` lives here rather than in the MCP layer,
for one reason: the MCP server, the CLI and the eval harness must take exactly
the same code path. The moment retrieval logic leaks into a transport adapter,
the number the eval prints stops describing what the agent actually receives.

The path is: identify the project, read the prompt, ask the gate, fetch what the
gate allows, enforce the budget, log the decision. Every stage may return
nothing, and nothing is a correct answer.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from projectmind import entities as entity_matching
from projectmind.config import Settings, get_settings
from projectmind.fingerprint.cache import FingerprintCache
from projectmind.gate import budget as budgeting
from projectmind.gate.prompt_analysis import PromptAnalysis, classify
from projectmind.gate.rules import GateDecision, decide, describe
from projectmind.logging import get_logger
from projectmind.models import ContextBundle, Fingerprint, Project, ServedRecord, utcnow
from projectmind.profile.repository import ProfileRepository, ProfileSlice
from projectmind.storage import Store, open_store
from projectmind.storage.base import BundleLogEntry, UsageStats

log = get_logger(__name__)


class EpisodicRetriever(Protocol):
    """Whatever can answer an episodic query.

    Left as a protocol so the front door can be finished and measured before
    retrieval exists. Until one is attached, the gate may allow the episodic
    slice and the slice is simply empty, which the bundle reports honestly.
    """

    def retrieve(
        self,
        decision: GateDecision,
        *,
        project: Project | None,
        fingerprint: Fingerprint,
        limit: int,
    ) -> Sequence[ServedRecord]: ...


@dataclass(slots=True)
class ResolvedProject:
    """Who is asking."""

    key: str
    name: str
    root: Path | None
    git_remote: str | None
    fingerprint: Fingerprint
    record: Project | None = None

    @property
    def is_known(self) -> bool:
        return not self.fingerprint.is_empty


class MemoryService:
    """Reads memory and answers questions about it. Owns no transport."""

    def __init__(
        self,
        store: Store,
        settings: Settings | None = None,
        *,
        retriever: EpisodicRetriever | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.store = store
        self.profile = ProfileRepository(store, self.settings)
        self.fingerprints = FingerprintCache(store)
        self.retriever = retriever

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
        """Return the smallest bundle that improves the next action."""
        started = time.perf_counter()
        moment = now or utcnow()

        project = self.resolve_project(project_path)
        analysis = self.analyse(prompt, project.fingerprint)
        decision = decide(
            analysis,
            fingerprint=project.fingerprint,
            episodic_available=self.store.count_records() > 0,
            settings=self.settings,
        )

        bundle = self._assemble(
            decision,
            project,
            moment,
            ignore_profile=ignore_profile,
            max_tokens=max_tokens,
        )
        bundle = bundle.model_copy(
            update={"latency_ms": round((time.perf_counter() - started) * 1000.0, 3)}
        )
        if log_bundle:
            self._log(bundle, prompt)
        return bundle

    def _assemble(
        self,
        decision: GateDecision,
        project: ResolvedProject,
        moment: datetime,
        *,
        ignore_profile: bool,
        max_tokens: int | None,
    ) -> ContextBundle:
        analysis = decision.analysis

        if decision.skips_everything:
            return ContextBundle(
                generated_at=moment,
                project_key=project.key,
                task_type=analysis.task_type,
                entities=analysis.entities,
                gate_reason=describe(decision),
            )

        profile_slice = ProfileSlice()
        if decision.inject_profile and not ignore_profile:
            profile_slice = self.profile.select(
                fingerprint=project.fingerprint,
                entities=analysis.entities,
                now=moment,
                token_budget=self._profile_budget(max_tokens),
            )

        episodic = budgeting.BudgetReport((), 0)
        if decision.inject_episodic and self.retriever is not None:
            candidates = self.retriever.retrieve(
                decision,
                project=project.record,
                fingerprint=project.fingerprint,
                limit=self.settings.max_episodic_records * 3,
            )
            episodic = budgeting.fit_records(
                candidates,
                token_cap=self.settings.episodic_token_cap,
                max_records=self.settings.max_episodic_records,
            )
            episodic = budgeting.enforce_total(
                profile_slice.tokens, episodic, settings=self.settings
            )

        reason = describe(decision)
        if ignore_profile:
            reason = f"{reason}; profile suppressed by the caller"
        elif decision.inject_profile and profile_slice.is_empty:
            reason = f"{reason}; no profile statement cleared the relevance floor"
        if decision.inject_episodic and not episodic.kept:
            reason = f"{reason}; nothing in episodic memory cleared the bar"

        return ContextBundle(
            generated_at=moment,
            project_key=project.key,
            task_type=analysis.task_type,
            entities=analysis.entities,
            profile=profile_slice.served(),
            episodic=episodic.kept,
            profile_tokens=profile_slice.tokens,
            episodic_tokens=episodic.tokens,
            gate_reason=reason,
        )

    def _profile_budget(self, max_tokens: int | None) -> int:
        if max_tokens is None:
            return self.settings.profile_token_cap
        return min(self.settings.profile_token_cap, max(0, max_tokens))

    # --- supporting reads --------------------------------------------------
    def analyse(self, prompt: str, fingerprint: Fingerprint | None = None) -> PromptAnalysis:
        return classify(
            prompt,
            vocabulary=sorted(self.vocabulary()),
            fingerprint=fingerprint,
            settings=self.settings,
        )

    def vocabulary(self) -> frozenset[str]:
        """Every entity memory holds, profile and episodic alike.

        Closed-vocabulary matching means a hit is evidence that there is
        something to retrieve, rather than evidence that the prompt contained
        an English noun.
        """
        from_profile = (statement.entities for statement in self.profile.active())
        from_records = (record.entities for record in self.store.list_records())
        return entity_matching.build_vocabulary(*from_profile, *from_records)

    def extract_entities(self, prompt: str) -> tuple[str, ...]:
        return entity_matching.extract(prompt, self.vocabulary())

    def resolve_project(self, project_path: str | Path | None) -> ResolvedProject:
        """Identify the calling project, fingerprint it, record that it was seen."""
        if project_path is None:
            return ResolvedProject("unknown", "unknown", None, None, Fingerprint())

        root = Path(project_path).expanduser()
        fingerprint = self.fingerprints.get(project_key(root, None), root).fingerprint
        remote = fingerprint.git_remote
        key = project_key(root, remote)

        if remote and key != project_key(root, None):
            # The remote gives a better identity than the directory name, so
            # re-key the cache entry rather than keeping two of them.
            self.fingerprints.store.put_cached_fingerprint(key, fingerprint)

        stored = self.store.upsert_project(
            Project(
                key=key,
                name=root.name or key,
                root_path=str(root),
                git_remote=remote,
                fingerprint=fingerprint,
                last_active=utcnow(),
            )
        )
        return ResolvedProject(key, root.name or key, root, remote, fingerprint, stored)

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
    directory name, which is what most unversioned local folders look like.
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
