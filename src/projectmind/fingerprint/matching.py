"""Ranking sibling projects by fingerprint similarity.

This is the mechanism behind the only claim this system makes that per-repo
tooling cannot: *you solved this before, somewhere else*. Before any embedding
is computed or any index is queried, a Flutter + Supabase + Riverpod project
already knows which of your other repositories look like it.

The ranking is a weighted Jaccard over four sets (see `Fingerprint.similarity`),
with dependencies dominating because the dependency set is the most specific
signal available for free. What this module adds on top is the *explanation* —
which dependencies were shared — because a similarity score nobody can inspect
is a similarity score nobody will trust.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from projectmind.models import SIMILARITY_WEIGHTS, Fingerprint, Project

#: Below this, two projects have essentially nothing in common and treating
#: them as neighbours would inject noise. Tuned in Phase 4 against the eval set.
DEFAULT_MIN_SIMILARITY = 0.10


@dataclass(frozen=True, slots=True)
class ProjectMatch:
    """A sibling project and why it matched."""

    project: Project
    similarity: float
    shared_dependencies: tuple[str, ...]
    shared_frameworks: tuple[str, ...]
    shared_languages: tuple[str, ...]

    @property
    def key(self) -> str:
        return self.project.key

    def explain(self) -> str:
        """One line a human can check."""
        parts: list[str] = []
        if self.shared_frameworks:
            parts.append("frameworks " + ", ".join(self.shared_frameworks[:3]))
        if self.shared_dependencies:
            shown = ", ".join(self.shared_dependencies[:4])
            extra = len(self.shared_dependencies) - 4
            parts.append(f"deps {shown}" + (f" +{extra}" if extra > 0 else ""))
        elif self.shared_languages:
            parts.append("language " + ", ".join(self.shared_languages))
        detail = "; ".join(parts) or "no overlapping components"
        return f"{self.key} ({self.similarity:.2f}) — {detail}"


@dataclass(frozen=True, slots=True)
class SimilarityBreakdown:
    """Per-component overlap, for `projectmind fingerprint compare`."""

    components: dict[str, float]
    total: float

    def rows(self) -> list[tuple[str, str, str]]:
        return [
            (name, f"{overlap:.3f}", f"{SIMILARITY_WEIGHTS.get(name, 0.0):.2f}")
            for name, overlap in sorted(self.components.items(), key=lambda kv: -kv[1])
        ]


def _shared(left: Sequence[str], right: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(frozenset(left) & frozenset(right)))


def compare(left: Fingerprint, right: Fingerprint) -> SimilarityBreakdown:
    """Component-wise overlap between two fingerprints."""
    components: dict[str, float] = {}
    for name in SIMILARITY_WEIGHTS:
        mine: frozenset[str] = frozenset(getattr(left, name))
        theirs: frozenset[str] = frozenset(getattr(right, name))
        union = mine | theirs
        if not union:
            continue
        components[name] = len(mine & theirs) / len(union)
    return SimilarityBreakdown(components, left.similarity(right))


def rank_siblings(
    target: Fingerprint,
    projects: Iterable[Project],
    *,
    exclude_keys: Iterable[str] = (),
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
    limit: int | None = None,
) -> list[ProjectMatch]:
    """Projects most like `target`, best first.

    The caller's own project belongs in `exclude_keys`: a project is perfectly
    similar to itself, which is true and useless.
    """
    excluded = {key for key in exclude_keys}
    matches: list[ProjectMatch] = []

    for project in projects:
        if project.key in excluded:
            continue
        other = project.fingerprint
        if other.is_empty:
            continue
        score = target.similarity(other)
        if score < min_similarity:
            continue
        matches.append(
            ProjectMatch(
                project=project,
                similarity=round(score, 4),
                shared_dependencies=_shared(target.dependencies, other.dependencies),
                shared_frameworks=_shared(target.frameworks, other.frameworks),
                shared_languages=_shared(target.languages, other.languages),
            )
        )

    matches.sort(key=lambda match: (-match.similarity, match.key))
    return matches[:limit] if limit else matches


def similarity_index(
    projects: Sequence[Project],
    *,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> dict[str, list[ProjectMatch]]:
    """Every project's neighbours. Used by `projectmind fingerprint map`.

    O(n²) on purpose: with a few dozen local repositories that is microseconds,
    and anything cleverer would be optimising the wrong thing.
    """
    return {
        project.key: rank_siblings(
            project.fingerprint,
            projects,
            exclude_keys=(project.key,),
            min_similarity=min_similarity,
        )
        for project in projects
    }
