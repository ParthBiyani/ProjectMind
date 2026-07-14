"""Turning commits into episodic records.

Two extractors, as the PRD specifies: decisions and failures. Both are
heuristic by default, because an extractor that needs an API key is an
extractor that does not run on a laptop with no network, and because a
deterministic pass is something you can read all the output of.

The hard rule is **precision over recall**. Most commits say nothing worth
remembering — "wip", "fix typo", "update deps" — and turning all of them into
records produces a memory layer whose every answer is noise. An extractor that
finds eight real decisions in a hundred commits is worth far more than one that
finds sixty maybes.

`is_inference` is set honestly. When a commit body states the reason, the `why`
is quoted and `is_inference` is False. When the reason is derived from the
shape of the commit, it is True, and the agent is told so.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from projectmind.entities import normalise
from projectmind.ingestion.git_source import Commit
from projectmind.logging import get_logger
from projectmind.models import EpisodicRecord, RecordType, SourceType

log = get_logger(__name__)

# --------------------------------------------------------------------------- #
# Signals
# --------------------------------------------------------------------------- #

#: Phrases that mark a stated rationale. The text after one of these is the
#: closest thing to a real "why" a commit ever contains.
REASON_MARKERS = (
    r"\bbecause\b",
    r"\bso that\b",
    r"\bin order to\b",
    r"\bthe reason\b",
    r"\bwhy:\s*",
    r"\brationale:\s*",
    r"\bmotivation:\s*",
    r"\bthis (?:lets|means|avoids|removes|fixes|stops)\b",
    r"\bturned out\b",
    r"\bit turns out\b",
)

#: Phrases that mark an alternative that was considered and dropped.
ALTERNATIVE_MARKERS = (
    r"instead of\s+(?P<value>[^.,;\n]{2,48})",
    r"rather than\s+(?P<value>[^.,;\n]{2,48})",
    r"in place of\s+(?P<value>[^.,;\n]{2,48})",
    r"over\s+(?P<value>[^.,;\n]{2,40})\s+because",
    r"dropped\s+(?P<value>[^.,;\n]{2,40})",
    r"moved off\s+(?P<value>[^.,;\n]{2,40})",
    r"replaced?\s+(?P<value>[^.,;\n]{2,40})\s+with",
)

#: Words that mark a commit as describing something that went wrong, rather
#: than something that was built.
FAILURE_WORDS = (
    r"\bbug\b",
    r"\bbroken\b",
    # "regression" is two different words. A regression *harness*, *test* or
    # *model* is not a failure, and in an ML repository it is usually the model.
    # Measured on a local speech repo, the bare pattern turned an eval-harness
    # feature commit into a 0.80-confidence failure record.
    r"\bregress(?:ion|ed)\b(?!\s+(?:test|tests|harness|suite|model|task|head|target))",
    r"\bcrash(?:es|ed|ing)?\b",
    r"\bleak(?:s|ed|ing|age)?\b",
    r"\bdeadlock\b",
    r"\brace condition\b",
    r"\bout of memory\b",
    r"\boom\b",
    r"\bnan\b",
    r"\bhang(?:s|ing)?\b",
    r"\btimeout\b",
    r"\bcorrupt\w*\b",
    r"\bwrong\b",
    r"\bincorrect\b",
    r"\bsilently\b",
    r"\bnever (?:fired|ran|worked)\b",
    r"\bwas not\b",
    r"\bfail(?:s|ed|ing|ure)\b",
    r"\bmismatch\b",
    r"\bedge case\b",
)

#: Commit subjects that are never worth a record, whatever else they contain.
NOISE_SUBJECTS = (
    r"^wip\b",
    r"^tmp\b",
    r"^temp\b",
    r"^\.+$",
    r"^merge branch '(?:main|master|develop)'",
    r"^update(?:d)? (?:readme|docs|changelog|dependencies|deps|lockfile)\.?$",
    r"^bump\b",
    r"^initial commit$",
    r"^first commit$",
    r"^\s*$",
    r"^format\b",
    r"^lint\b",
    r"^typo\b",
    r"^fix typo",
    r"^minor\b",
    r"^cleanup\.?$",
    r"^chore\(deps\)",
)

#: Conventional commit types that never carry a decision.
UNINTERESTING_TYPES = frozenset({"style", "chore", "ci", "build", "test", "docs"})

#: Conventional types whose author has already said this is not a failure.
AUTHOR_SAYS_NOT_A_FAILURE = frozenset({"feat", "docs", "style", "test", "chore", "ci", "build"})

#: A technical lexicon used when the project fingerprint is thin. Kept broad
#: but closed; entity extraction is only useful if a hit means something.
#:
#: Words that are also ordinary English are deliberately absent. "map",
#: "provider" and "regression" were all in here and all fired on prose: an
#: unrelated commit picked up "provider" from the phrase "the provider's API",
#: which is a false entity and therefore a false gate trigger downstream.
TECH_LEXICON: frozenset[str] = frozenset(
    {
        "alembic",
        "albumentations",
        "asyncio",
        "auth",
        "aws",
        "axum",
        "bert",
        "bloc",
        "bm25",
        "cache",
        "caching",
        "celery",
        "ci",
        "cnn",
        "colab",
        "convnext",
        "crnn",
        "cross-validation",
        "ctc",
        "cuda",
        "dataloader",
        "diarisation",
        "distilbert",
        "django",
        "docker",
        "docker-compose",
        "dropout",
        "embeddings",
        "fastapi",
        "fastembed",
        "ffmpeg",
        "firebase",
        "firestore",
        "flask",
        "flutter",
        "freezed",
        "gcp",
        "gradio",
        "graphql",
        "grpc",
        "gunicorn",
        "hnsw",
        "huggingface",
        "imbalance",
        "jwt",
        "kafka",
        "kfold",
        "keras",
        "kubernetes",
        "langchain",
        "langgraph",
        "lightgbm",
        "llm",
        "lstm",
        "migration",
        "mlflow",
        "mongodb",
        "mypy",
        "nextjs",
        "nginx",
        "numpy",
        "onnx",
        "opencv",
        "optuna",
        "pandas",
        "pgvector",
        "pinecone",
        "pipeline",
        "poetry",
        "postgres",
        "prisma",
        "prometheus",
        "pydantic",
        "pytest",
        "pytorch",
        "quantisation",
        "rabbitmq",
        "rag",
        "react",
        "redis",
        "riverpod",
        "roboflow",
        "ruff",
        "s3",
        "scikit-learn",
        "sentry",
        "shap",
        "smote",
        "sqlalchemy",
        "sqlite",
        "streamlit",
        "supabase",
        "tailwindcss",
        "tanstack-query",
        "tensorflow",
        "terraform",
        "tesseract",
        "timm",
        "tokio",
        "torch",
        "transformers",
        "trocr",
        "typescript",
        "ultralytics",
        "uv",
        "uvicorn",
        "vector-store",
        "walk-forward",
        "webpack",
        "whisper",
        "xgboost",
        "yolov8",
        "zod",
    }
)

#: Confidence floors. Nothing below `episodic_serve_confidence` (0.6) is ever
#: served, so an extractor producing 0.55 is producing something it does not
#: believe in, which is the correct way to express doubt.
CONFIDENCE_STATED_REASON = 0.85
CONFIDENCE_MERGE = 0.72
CONFIDENCE_CONVENTIONAL = 0.66
CONFIDENCE_REVERT = 0.88
CONFIDENCE_FIX_WITH_DETAIL = 0.8
CONFIDENCE_FIX_TERSE = 0.58


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    """What the extractor knows about the repository it is reading."""

    project_id: UUID
    project_key: str
    remote: str | None = None
    vocabulary: frozenset[str] = TECH_LEXICON

    def source_url(self, commit: Commit) -> str:
        """A link that survives being read six months later."""
        if self.remote:
            base = self.remote.removesuffix(".git")
            if base.startswith("http"):
                return f"{base}/commit/{commit.sha}"
        return f"git://{self.project_key}/commit/{commit.sha}"


def _search(patterns: Iterable[str], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match
    return None


def is_noise(commit: Commit) -> bool:
    """Commits that are never worth a record."""
    subject = commit.subject.strip().lower()
    if len(subject) < 8:
        return True
    return any(re.search(pattern, subject, re.IGNORECASE) for pattern in NOISE_SUBJECTS)


def extract_entities(commit: Commit, vocabulary: frozenset[str]) -> tuple[str, ...]:
    """Technical entities named by the message or implied by the files touched."""
    text = commit.message.lower()
    found = {term for term in vocabulary if re.search(rf"(?<![a-z0-9]){re.escape(term)}\b", text)}

    for path in commit.files:
        stem = path.replace("\\", "/").lower()
        for term in vocabulary:
            if f"/{term}" in stem or stem.startswith(term):
                found.add(term)
    return tuple(sorted(normalise(term) for term in found))


def stated_reason(commit: Commit) -> str | None:
    """The author's own words, when they gave a reason."""
    body = commit.body.strip()
    if not body:
        return None
    for line in body.splitlines():
        cleaned = line.strip().lstrip("-*# ").strip()
        if len(cleaned) < 12:
            continue
        if _search(REASON_MARKERS, cleaned):
            return cleaned[:400]
    # A multi-line body with no marker is still usually the rationale; the
    # first substantial paragraph is the best available guess at it.
    paragraphs = [block.strip() for block in body.split("\n\n") if len(block.strip()) >= 30]
    return paragraphs[0][:400].replace("\n", " ") if paragraphs else None


def rejected_alternatives(commit: Commit) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in ALTERNATIVE_MARKERS:
        for match in re.finditer(pattern, commit.message, re.IGNORECASE):
            value = (match.group("value") or "").strip().strip(".,;:")
            if 2 <= len(value) <= 48 and value.lower() not in {"it", "this", "that", "them"}:
                found.append(value)
    seen: list[str] = []
    for item in found:
        if item.lower() not in {existing.lower() for existing in seen}:
            seen.append(item)
    return tuple(seen[:4])


# --------------------------------------------------------------------------- #
# Decisions
# --------------------------------------------------------------------------- #


class DecisionExtractor:
    """Finds choices: what was adopted, and ideally why.

    Three shapes qualify, in descending order of confidence:

    1. a commit whose body states a reason,
    2. a merge of a named feature branch, which is a unit of work someone
       deliberately separated and then integrated,
    3. a `feat` or `refactor` commit that names a technical entity.

    Everything else is skipped. A `feat:` commit reading "add button" names no
    choice and remembering it would only dilute the ones that do.
    """

    name = "decisions"

    def extract(self, commit: Commit, context: ExtractionContext) -> EpisodicRecord | None:
        if is_noise(commit):
            return None
        if commit.is_revert:
            return None

        entities = extract_entities(commit, context.vocabulary)
        reason = stated_reason(commit)
        alternatives = rejected_alternatives(commit)
        commit_type = commit.commit_type

        if commit_type in UNINTERESTING_TYPES and not reason:
            return None

        if reason:
            confidence = CONFIDENCE_STATED_REASON
            is_inference = False
            why = reason
        elif commit.is_merge and commit.merged_branch:
            confidence = CONFIDENCE_MERGE
            is_inference = True
            why = f"integrated the {commit.merged_branch} branch as a unit of work"
        elif commit_type in {"feat", "refactor", "perf"} and entities and len(commit.summary) >= 24:
            confidence = CONFIDENCE_CONVENTIONAL
            is_inference = True
            why = f"adopted while working on {', '.join(entities[:3])}"
        else:
            # There used to be a fourth tier here that accepted any commit
            # naming an entity. Over a local speech repository it turned 52% of
            # commits into records reading "inferred from a change touching
            # pipeline", which is true, unfalsifiable and worth nothing. A
            # record that cannot be wrong cannot be useful.
            return None

        if alternatives:
            confidence = min(0.95, confidence + 0.05)

        return EpisodicRecord(
            project_id=context.project_id,
            type=RecordType.DECISION,
            what=commit.summary[:300],
            why=why,
            rejected_alternatives=alternatives,
            outcome="",
            entities=entities,
            source_type=SourceType.PULL_REQUEST if commit.is_merge else SourceType.COMMIT,
            source_url=context.source_url(commit),
            occurred_at=commit.authored_at,
            confidence=confidence,
            is_inference=is_inference,
        )


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


class FailureExtractor:
    """Finds what went wrong and what fixed it.

    Failures are the highest-value records in the system and the easiest to
    over-collect, because every repository is full of `fix:` commits that say
    nothing. A `fix` commit qualifies only when it describes the failure — a
    body, a failure word, or a subject long enough to actually contain a
    symptom. A revert always qualifies: someone undid work, which is a strong
    signal on its own.
    """

    name = "failures"

    def extract(self, commit: Commit, context: ExtractionContext) -> EpisodicRecord | None:
        if is_noise(commit) and not commit.is_revert:
            return None

        entities = extract_entities(commit, context.vocabulary)
        reason = stated_reason(commit)
        text = commit.message
        has_failure_word = _search(FAILURE_WORDS, text) is not None

        if commit.is_revert:
            reverted = commit.reverted_subject or commit.summary
            return EpisodicRecord(
                project_id=context.project_id,
                type=RecordType.REVERSAL,
                what=f"Reverted: {reverted}"[:300],
                why=reason or "the change was undone rather than fixed forward",
                rejected_alternatives=rejected_alternatives(commit),
                outcome="reverted",
                entities=entities,
                source_type=SourceType.REVERT,
                source_url=context.source_url(commit),
                occurred_at=commit.authored_at,
                confidence=CONFIDENCE_REVERT,
                is_inference=reason is None,
            )

        # The author already classified this commit. A `feat` is not a bug
        # report, and overriding the author's own label on the strength of one
        # matched word is how a feature ends up remembered as an outage.
        if commit.commit_type in AUTHOR_SAYS_NOT_A_FAILURE:
            return None

        if commit.commit_type != "fix" and not has_failure_word:
            return None

        detailed = bool(reason) or has_failure_word
        if not detailed and len(commit.summary) < 30:
            return None

        confidence = CONFIDENCE_FIX_WITH_DETAIL if detailed else CONFIDENCE_FIX_TERSE
        if reason:
            confidence = min(0.92, confidence + 0.05)

        return EpisodicRecord(
            project_id=context.project_id,
            type=RecordType.FAILURE,
            what=commit.summary[:300],
            why=reason or f"symptom described in the commit: {commit.subject[:160]}",
            rejected_alternatives=rejected_alternatives(commit),
            outcome="fixed in this commit",
            entities=entities,
            source_type=SourceType.COMMIT,
            source_url=context.source_url(commit),
            occurred_at=commit.authored_at,
            confidence=confidence,
            is_inference=reason is None,
        )


DEFAULT_EXTRACTORS: tuple[DecisionExtractor | FailureExtractor, ...] = (
    FailureExtractor(),
    DecisionExtractor(),
)


def extract_all(
    commits: Sequence[Commit],
    context: ExtractionContext,
    extractors: Sequence[DecisionExtractor | FailureExtractor] = DEFAULT_EXTRACTORS,
) -> list[EpisodicRecord]:
    """Run every extractor over every commit, first match wins per commit.

    Order matters: failures are tried before decisions, so a commit that fixes
    a bug is remembered as a bug rather than as a feature. One commit produces
    at most one record, because the same event recorded twice is the same event
    served twice.
    """
    records: list[EpisodicRecord] = []
    for commit in commits:
        for extractor in extractors:
            record = extractor.extract(commit, context)
            if record is not None:
                records.append(record)
                break
    return records
