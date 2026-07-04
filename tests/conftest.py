from __future__ import annotations

from pathlib import Path

import pytest

from projectmind.evaluation import EvalSet
from projectmind.evaluation.fixtures import FixtureCorpus

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def eval_set() -> EvalSet:
    return EvalSet.from_yaml(REPO_ROOT / "eval" / "queries.yaml")


@pytest.fixture(scope="session")
def corpus() -> FixtureCorpus:
    return FixtureCorpus.load(REPO_ROOT / "eval" / "fixtures" / "corpus.yaml")
