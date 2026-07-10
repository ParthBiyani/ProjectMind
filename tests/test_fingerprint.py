from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from projectmind.fingerprint import manifests
from projectmind.fingerprint.cache import FingerprintCache
from projectmind.fingerprint.matching import compare, rank_siblings, similarity_index
from projectmind.fingerprint.scanner import ProjectScanner, manifest_digest
from projectmind.models import Fingerprint, Project, utcnow
from projectmind.storage import SqliteStore, Store


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def write(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestManifestParsing:
    def test_pubspec(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "pubspec.yaml",
            "name: spendwise\ndependencies:\n  flutter:\n    sdk: flutter\n"
            "  riverpod: ^2.5.0\n  supabase_flutter: ^2.0.0\ndev_dependencies:\n  build_runner: any\n",
        )
        facts = manifests.parse(path)
        assert facts is not None
        assert facts.language == "dart"
        assert set(facts.dependencies) == {
            "flutter",
            "riverpod",
            "supabase_flutter",
            "build_runner",
        }
        assert set(facts.frameworks) == {"flutter", "supabase"}

    def test_package_json_detects_typescript_from_the_dependency(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "package.json",
            json.dumps(
                {
                    "dependencies": {"next": "14", "react": "18"},
                    "devDependencies": {"typescript": "5"},
                }
            ),
        )
        facts = manifests.parse(path)
        assert facts is not None
        assert facts.language == "typescript"
        assert set(facts.frameworks) == {"nextjs", "react"}

    def test_package_json_detects_typescript_from_tsconfig(self, tmp_path: Path) -> None:
        write(tmp_path, "tsconfig.json", "{}")
        path = write(tmp_path, "package.json", json.dumps({"dependencies": {"react": "18"}}))
        facts = manifests.parse(path)
        assert facts is not None and facts.language == "typescript"

    def test_pyproject_reads_both_pep621_and_poetry(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "x"\ndependencies = ["fastapi>=0.110", "pydantic[email]"]\n'
            '[project.optional-dependencies]\ndev = ["pytest"]\n'
            '[tool.poetry.dependencies]\npython = "^3.11"\nredis = "^5"\n',
        )
        facts = manifests.parse(path)
        assert facts is not None
        assert set(facts.dependencies) == {"fastapi", "pydantic", "pytest", "redis"}
        assert "python" not in facts.dependencies, "the interpreter is not a dependency"

    def test_requirements_ignores_comments_flags_and_urls(self, tmp_path: Path) -> None:
        path = write(
            tmp_path,
            "requirements.txt",
            "# comment\n-r base.txt\ntorch==2.3.0\nultralytics>=8\n"
            "git+https://example.invalid/x.git\n\nopencv-python\n",
        )
        facts = manifests.parse(path)
        assert facts is not None
        assert set(facts.dependencies) == {"torch", "ultralytics", "opencv-python"}
        assert set(facts.frameworks) == {"pytorch", "ultralytics", "opencv"}

    def test_cargo_and_go(self, tmp_path: Path) -> None:
        cargo = write(tmp_path, "Cargo.toml", '[dependencies]\ntokio = "1"\naxum = "0.7"\n')
        assert manifests.parse(cargo) is not None
        assert set(manifests.parse(cargo).frameworks) == {"tokio", "axum"}  # type: ignore[union-attr]
        gomod = write(tmp_path, "go.mod", "module x\n\nrequire (\n\tgithub.com/a/b v1.2.3\n)\n")
        facts = manifests.parse(gomod)
        assert facts is not None and facts.language == "go"
        assert facts.dependencies == ("github.com/a/b",)

    def test_a_broken_manifest_yields_nothing_rather_than_raising(self, tmp_path: Path) -> None:
        assert manifests.parse(write(tmp_path, "package.json", "{ not json")) is None
        broken_yaml = write(tmp_path, "pubspec.yaml", "dependencies:\n  - [unclosed\n")
        assert manifests.parse(broken_yaml) is None

    def test_an_unrecognised_file_is_not_a_manifest(self, tmp_path: Path) -> None:
        assert manifests.parse(write(tmp_path, "notes.md", "hello")) is None
        assert manifests.is_manifest(tmp_path / "notes.md") is False

    def test_dependency_names_are_normalised(self) -> None:
        assert manifests.normalise_dependency("  Pydantic[email]>=2.7 ") == "pydantic"
        assert manifests.normalise_dependency("torch==2.3.0; sys_platform=='linux'") == "torch"

    def test_summarise_merges_several_manifests(self, tmp_path: Path) -> None:
        write(tmp_path, "requirements.txt", "fastapi\n")
        write(tmp_path / "web", "package.json", json.dumps({"dependencies": {"react": "18"}}))
        summary = manifests.summarise(
            [tmp_path / "requirements.txt", tmp_path / "web" / "package.json"]
        )
        assert summary.languages == {"python", "javascript"}
        assert summary.frameworks == {"fastapi", "react"}


class TestScanner:
    def test_a_python_project_is_recognised(self, tmp_path: Path) -> None:
        write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi", "redis"]\n')
        result = ProjectScanner(read_git_remote=False).scan(tmp_path)
        assert result.fingerprint.languages == ("python",)
        assert result.fingerprint.dependencies == ("fastapi", "redis")
        assert result.fingerprint.manifest_files == ("pyproject.toml",)
        assert result.is_useful

    def test_vendored_and_data_directories_are_skipped(self, tmp_path: Path) -> None:
        write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["torch"]\n')
        write(
            tmp_path / "node_modules" / "pkg",
            "package.json",
            json.dumps({"dependencies": {"left-pad": "1"}}),
        )
        write(tmp_path / "data", "requirements.txt", "pandas\n")
        for index in range(50):
            write(tmp_path / "data" / "images", f"{index}.txt", "x")
        result = ProjectScanner(read_git_remote=False).scan(tmp_path)
        assert result.fingerprint.dependencies == ("torch",)
        assert result.files_seen < 10, "the data directory should never have been walked"

    def test_extensions_are_the_fallback_when_there_is_no_manifest(self, tmp_path: Path) -> None:
        for index in range(6):
            write(tmp_path, f"script{index}.py", "print(1)\n")
        result = ProjectScanner(read_git_remote=False).scan(tmp_path)
        assert result.fingerprint.languages == ("python",)
        assert result.fingerprint.dependencies == ()

    def test_a_single_stray_file_does_not_claim_a_language(self, tmp_path: Path) -> None:
        for index in range(40):
            write(tmp_path, f"m{index}.py", "x")
        write(tmp_path, "one.rs", "fn main() {}")
        assert ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint.languages == (
            "python",
        )

    def test_domain_hints_need_more_than_one_keyword(self, tmp_path: Path) -> None:
        write(tmp_path, "README.md", "This project trains a model.")
        assert ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint.domain_hints == ()

        write(tmp_path, "README.md", "Trains a model on a dataset for inference at the edge.")
        hints = ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint.domain_hints
        assert "machine-learning" in hints

    def test_domain_hints_are_capped(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "README.md",
            "clinical patient medical diagnosis health blood "
            "trading portfolio broker market ticker etf "
            "course student exam quiz placement "
            "sensor arduino raspberry embedded firmware",
        )
        hints = ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint.domain_hints
        assert 0 < len(hints) <= 3

    def test_word_boundaries_stop_ml_matching_inside_html(self, tmp_path: Path) -> None:
        write(tmp_path, "README.md", "An html and xml project. More html here.")
        assert (
            "machine-learning"
            not in ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint.domain_hints
        )

    def test_scanning_a_missing_directory_returns_an_empty_fingerprint(
        self, tmp_path: Path
    ) -> None:
        result = ProjectScanner(read_git_remote=False).scan(tmp_path / "nope")
        assert result.fingerprint.is_empty
        assert result.files_seen == 0

    def test_the_file_budget_is_respected(self, tmp_path: Path) -> None:
        for index in range(40):
            write(tmp_path, f"f{index}.txt", "x")
        result = ProjectScanner(read_git_remote=False, max_files=10).scan(tmp_path)
        assert result.truncated is True
        assert result.files_seen <= 10


class TestManifestDigest:
    def test_the_digest_changes_with_content(self, tmp_path: Path) -> None:
        write(tmp_path, "pyproject.toml", "a")
        first = manifest_digest(tmp_path, ["pyproject.toml"])
        write(tmp_path, "pyproject.toml", "b")
        assert manifest_digest(tmp_path, ["pyproject.toml"]) != first

    def test_a_missing_file_makes_the_digest_none(self, tmp_path: Path) -> None:
        assert manifest_digest(tmp_path, ["gone.toml"]) is None

    def test_the_scanner_and_the_cache_agree(self, tmp_path: Path) -> None:
        """If these ever disagree, every cache lookup is a miss."""
        write(tmp_path, "pyproject.toml", '[project]\ndependencies = ["fastapi"]\n')
        write(tmp_path / "web", "package.json", json.dumps({"dependencies": {"react": "18"}}))
        fingerprint = ProjectScanner(read_git_remote=False).scan(tmp_path).fingerprint
        assert manifest_digest(tmp_path, fingerprint.manifest_files) == fingerprint.manifest_hash


class TestCache:
    def _project(self, tmp_path: Path) -> Path:
        root = tmp_path / "demo"
        write(root, "pyproject.toml", '[project]\ndependencies = ["fastapi", "redis"]\n')
        return root

    def test_first_lookup_misses_and_second_hits(self, store: Store, tmp_path: Path) -> None:
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False))
        root = self._project(tmp_path)
        first = cache.get("demo", root)
        assert first.missed and first.reason == "not cached"
        second = cache.get("demo", root)
        assert second.hit and second.reason == "cache hit"

    def test_changing_a_manifest_invalidates(self, store: Store, tmp_path: Path) -> None:
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False))
        root = self._project(tmp_path)
        cache.get("demo", root)
        write(
            root, "pyproject.toml", '[project]\ndependencies = ["fastapi", "redis", "supabase"]\n'
        )
        again = cache.get("demo", root)
        assert again.missed
        assert again.reason == "a manifest changed"
        assert "supabase" in again.fingerprint.dependencies

    def test_deleting_a_manifest_invalidates(self, store: Store, tmp_path: Path) -> None:
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False))
        root = self._project(tmp_path)
        cache.get("demo", root)
        (root / "pyproject.toml").unlink()
        again = cache.get("demo", root)
        assert again.missed
        assert "has gone" in again.reason

    def test_editing_source_does_not_invalidate(self, store: Store, tmp_path: Path) -> None:
        """The whole point: a code edit must not trigger a rescan."""
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False))
        root = self._project(tmp_path)
        cache.get("demo", root)
        write(root, "main.py", "print('changed')\n")
        assert cache.get("demo", root).hit is True

    def test_an_expired_entry_is_rescanned(self, store: Store, tmp_path: Path) -> None:
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False), ttl_hours=24)
        root = self._project(tmp_path)
        fresh = cache.get("demo", root).fingerprint
        stale = fresh.model_copy(update={"computed_at": utcnow() - timedelta(days=3)})
        store.put_cached_fingerprint("demo", stale)
        again = cache.get("demo", root)
        assert again.missed
        assert "old" in again.reason

    def test_invalidate_forces_a_recompute(self, store: Store, tmp_path: Path) -> None:
        cache = FingerprintCache(store, ProjectScanner(read_git_remote=False))
        root = self._project(tmp_path)
        cache.get("demo", root)
        cache.invalidate("demo")
        assert cache.get("demo", root).missed


class TestSimilarity:
    FLUTTER_A = Fingerprint(
        languages=["dart"],
        frameworks=["flutter"],
        dependencies=["riverpod", "supabase_flutter", "go_router"],
    )
    FLUTTER_B = Fingerprint(
        languages=["dart"],
        frameworks=["flutter"],
        dependencies=["riverpod", "supabase_flutter", "freezed"],
    )
    ML = Fingerprint(
        languages=["python"],
        frameworks=["pytorch"],
        dependencies=["torch", "timm", "albumentations"],
    )
    PY_ONLY_A = Fingerprint(languages=["python"])
    PY_ONLY_B = Fingerprint(languages=["python"])

    def test_two_flutter_projects_beat_a_flutter_and_an_ml_project(self) -> None:
        assert self.FLUTTER_A.similarity(self.FLUTTER_B) > self.FLUTTER_A.similarity(self.ML)

    def test_language_only_overlap_cannot_reach_a_perfect_score(self) -> None:
        """Found on real data: four unrelated Python repos all scored 1.000."""
        score = self.PY_ONLY_A.similarity(self.PY_ONLY_B)
        assert 0.0 < score <= 0.35, score

    def test_a_full_overlap_still_scores_one(self) -> None:
        assert self.FLUTTER_A.similarity(self.FLUTTER_A) == pytest.approx(1.0)

    def test_compare_breaks_the_score_down(self) -> None:
        breakdown = compare(self.FLUTTER_A, self.FLUTTER_B)
        assert breakdown.components["languages"] == pytest.approx(1.0)
        assert breakdown.components["frameworks"] == pytest.approx(1.0)
        assert 0.0 < breakdown.components["dependencies"] < 1.0
        assert "domain_hints" not in breakdown.components, "empty components are dropped"


class TestSiblingRanking:
    def _projects(self) -> list[Project]:
        return [
            Project(key="spendwise", name="SpendWise", fingerprint=TestSimilarity.FLUTTER_A),
            Project(key="tripmate", name="TripMate", fingerprint=TestSimilarity.FLUTTER_B),
            Project(key="pill", name="Pill Counting", fingerprint=TestSimilarity.ML),
            Project(key="empty", name="Empty", fingerprint=Fingerprint()),
        ]

    def test_the_closest_project_comes_first(self) -> None:
        matches = rank_siblings(
            TestSimilarity.FLUTTER_A, self._projects(), exclude_keys=("spendwise",)
        )
        assert matches[0].key == "tripmate"

    def test_the_caller_is_excluded(self) -> None:
        keys = [
            m.key
            for m in rank_siblings(
                TestSimilarity.FLUTTER_A, self._projects(), exclude_keys=("spendwise",)
            )
        ]
        assert "spendwise" not in keys

    def test_projects_with_no_fingerprint_are_skipped(self) -> None:
        keys = [m.key for m in rank_siblings(TestSimilarity.FLUTTER_A, self._projects())]
        assert "empty" not in keys

    def test_the_threshold_is_applied(self) -> None:
        matches = rank_siblings(TestSimilarity.FLUTTER_A, self._projects(), min_similarity=0.9)
        assert [m.key for m in matches] == ["spendwise"]

    def test_a_match_explains_itself(self) -> None:
        match = rank_siblings(
            TestSimilarity.FLUTTER_A, self._projects(), exclude_keys=("spendwise",)
        )[0]
        assert "riverpod" in match.shared_dependencies
        assert "flutter" in match.shared_frameworks
        assert "riverpod" in match.explain()

    def test_the_index_covers_every_project(self) -> None:
        index = similarity_index(self._projects(), min_similarity=0.0)
        assert set(index) == {"spendwise", "tripmate", "pill", "empty"}
        assert index["empty"] == [] or all(m.key != "empty" for m in index["empty"])
