from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from projectmind.evaluation import EvalSet
from projectmind.evaluation.fixtures import FixtureCorpus

REPO_ROOT = Path(__file__).resolve().parents[1]

# Thresholds are calibrated per embedder, so tests must not depend on which
# optional extras happen to be installed. Set at import, before any
# module-scoped fixture builds an embedder.
os.environ["PROJECTMIND_EMBEDDING_PROVIDER"] = "hashing"

# Tests must never read or write the real ProjectMind home (logs, database, state).
# Tests that need a particular home set their own; this is only the default.
os.environ.setdefault("PROJECTMIND_HOME", tempfile.mkdtemp(prefix="pm-tests-"))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def eval_set() -> EvalSet:
    return EvalSet.from_yaml(REPO_ROOT / "eval" / "queries.yaml")


@pytest.fixture(scope="session")
def corpus() -> FixtureCorpus:
    return FixtureCorpus.load(REPO_ROOT / "eval" / "fixtures" / "corpus.yaml")
