"""Entity extraction against a known vocabulary.

Deliberately closed-vocabulary. An open-vocabulary extractor would find
"authentication" and "performance" in every prompt and the gate would fire on
all of them; matching only against entities that memory actually holds means a
hit is evidence that there is something to retrieve.

No model is involved. This runs on every call to the front door, so it has to
cost microseconds.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Mapping

_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.+#-]*")

#: Surface forms that should resolve to a canonical entity. Kept small and
#: hand-maintained: a synonym list that tries to be exhaustive becomes a source
#: of false matches, which is the expensive kind of error here.
ALIASES: Mapping[str, str] = {
    "postgresql": "postgres",
    "psql": "postgres",
    "pg": "postgres",
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "sklearn": "scikit-learn",
    "scikit": "scikit-learn",
    "torch": "pytorch",
    "yolo": "yolov8",
    "yolov5": "yolov8",
    "yolov11": "yolov8",
    "oom": "cuda",
    "out-of-memory": "cuda",
    "gpt": "llm",
    "openai": "llm",
    "claude": "llm",
    "anthropic": "llm",
    "nextjs": "nextjs",
    "next.js": "nextjs",
    "next": "nextjs",
    "tailwind": "tailwindcss",
    "dataloader": "dataloader",
    "num_workers": "dataloader",
    "k-fold": "kfold",
    "kfold": "kfold",
    "walkforward": "walk-forward",
    "onnxruntime": "onnx",
    "docker-compose": "docker-compose",
    "compose": "docker-compose",
}


def normalise(term: str) -> str:
    term = term.strip().lower().strip(".,;:!?()[]{}\"'`")
    return ALIASES.get(term, term)


def build_vocabulary(*groups: Iterable[str]) -> frozenset[str]:
    """Union of every entity memory knows about, normalised."""
    return frozenset(normalise(term) for group in groups for term in group if term.strip())


def candidate_terms(text: str) -> set[str]:
    """Unigrams, bigrams and de-punctuated variants worth looking up.

    Bigrams matter because plenty of real entities are two words once written
    naturally: "class weights", "data leakage", "test time augmentation".
    """
    tokens = _TOKEN.findall(text.lower())
    terms: set[str] = set()
    for token in tokens:
        terms.add(token)
        # `go_router` written as `go router`, `scikit-learn` as `scikit learn`
        terms.add(token.replace("_", "-"))
        terms.add(token.replace("-", "_"))
        terms.add(token.rstrip("s"))
    for left, right in itertools.pairwise(tokens):
        terms.add(f"{left} {right}")
        terms.add(f"{left}-{right}")
        terms.add(f"{left}_{right}")
    return terms


def extract(text: str, vocabulary: Iterable[str]) -> tuple[str, ...]:
    """Entities from `vocabulary` that `text` mentions, in a stable order."""
    known = {normalise(term): term for term in vocabulary if term.strip()}
    if not known:
        return ()
    found = {known[normalise(term)] for term in candidate_terms(text) if normalise(term) in known}
    return tuple(sorted(found))
