from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from projectmind.sources.config import RootConfig, SourcesConfig
from projectmind.sources.indexer import KnowledgeIndexer
from projectmind.sources.walker import ProjectResolver, classify, walk
from projectmind.storage import SqliteStore, Store

NOTES = """# Prostate MRI

## Score definition

The score is 10 * (0.55*Dice + 0.25*boundary_F1). Exact voxel matching, no tolerance band.

## What worked

Walk-forward style post-processing kept a single connected component per mask.
"""


@pytest.fixture
def store() -> Iterator[Store]:
    backend = SqliteStore(":memory:")
    backend.migrate()
    yield backend
    backend.close()


def write(path: Path, text: str, *, age_seconds: float = 7_200.0) -> Path:
    """A file whose modification time is safely in the past."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stamp = path.stat().st_mtime - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def build_root(tmp_path: Path) -> RootConfig:
    root = tmp_path / "Eris"
    write(root / "Prostate MRI" / "CLAUDE.md", NOTES)
    write(root / "Prostate MRI" / "experiments" / "exp_v4.py", "# v4: patch unet, CV dice 0.62\n")
    write(
        root / "Prostate MRI" / "working" / "train.log",
        "fold 1 dice: 0.6234\nfold 2 dice: 0.6411\n",
    )
    write(root / "Prostate MRI" / "dataset" / "labels.txt", "should never be read\n" * 50)
    write(root / "Prostate MRI" / ".env", "API_KEY=Zx81qLmN04aBc9QrTuV2wXy\n")
    write(root / "Prostate MRI" / "requirements.txt", "numpy\nscipy\n")
    write(
        root / "Albanian News" / "Claude.md",
        "# Albanian News\n\nMetric is chrF on recovered cards.\n" * 3,
    )
    return RootConfig(path=root, kind="competition", depth=1)


def indexed_uris(store: Store) -> set[str]:
    return {doc.uri for doc in store.list_documents(limit=1000)}


class TestWalker:
    def test_only_readable_kinds_are_candidates(self, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        names = {candidate.path.name for candidate in walk(root.path)}
        assert {"CLAUDE.md", "exp_v4.py", "train.log", "Claude.md"} <= names

    def test_datasets_secrets_and_manifests_are_never_candidates(self, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        names = {candidate.path.name for candidate in walk(root.path)}
        assert "labels.txt" not in names
        assert ".env" not in names
        assert "requirements.txt" not in names

    def test_result_files_are_only_read_from_result_folders(self, tmp_path: Path) -> None:
        loose = tmp_path / "notes" / "x.json"
        loose.parent.mkdir()
        loose.write_text('{"dice": 0.5}', encoding="utf-8")
        inside = tmp_path / "experiments" / "r.json"
        inside.parent.mkdir()
        inside.write_text('{"dice": 0.5}', encoding="utf-8")
        assert classify(loose, loose.stat().st_size) is None
        assert classify(inside, inside.stat().st_size) == "results"

    def test_oversized_and_empty_files_are_skipped(self, tmp_path: Path) -> None:
        empty = write(tmp_path / "empty.md", "")
        assert classify(empty, 0) is None
        assert classify(tmp_path / "big.md", 50_000_000) is None


class TestProjectResolution:
    def test_the_folder_at_depth_is_the_project(self, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        resolver = ProjectResolver(root)
        assert resolver.key_for(root.path / "Prostate MRI" / "CLAUDE.md") == "prostate-mri"
        assert (
            resolver.key_for(root.path / "Prostate MRI" / "working" / "train.log") == "prostate-mri"
        )

    def test_a_file_directly_under_the_root_belongs_to_the_root(self, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        assert ProjectResolver(root).key_for(root.path / "overview.md") == "eris"

    def test_a_nested_git_repository_is_its_own_project_with_the_git_key(
        self, tmp_path: Path
    ) -> None:
        root = RootConfig(path=tmp_path / "Work", kind="work", depth=1)
        repo = root.path / "PineLabs" / "assistant"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "https://github.com/acme/assistant.git",
            ],
            check=True,
            capture_output=True,
        )
        resolver = ProjectResolver(root)
        assert resolver.key_for(repo / "README.md") == "acme-assistant"
        assert resolver.key_for(root.path / "PineLabs" / "PLAN.md") == "pinelabs"


class TestScan:
    def test_a_scan_indexes_documents_with_chunks_and_project_keys(
        self, store: Store, tmp_path: Path
    ) -> None:
        root = build_root(tmp_path)
        report = KnowledgeIndexer(store).scan_root(root)
        assert report.error is None
        assert report.indexed == 4
        assert report.chunks >= 4
        assert store.count_documents(root.key) == 4
        hits = store.search_chunks(["voxel", "tolerance"])
        assert hits
        assert hits[0].document.project_key == "prostate-mri"
        assert hits[0].chunk.heading.endswith("Score definition")

    def test_nothing_from_datasets_or_secrets_reaches_the_index(
        self, store: Store, tmp_path: Path
    ) -> None:
        KnowledgeIndexer(store).scan_root(build_root(tmp_path))
        assert store.search_chunks(["should", "never", "read"]) == []
        assert store.search_chunks(["Zx81qLmN04aBc9QrTuV2wXy"]) == []

    def test_a_chunk_holding_a_credential_is_dropped_whole(
        self, store: Store, tmp_path: Path
    ) -> None:
        root = RootConfig(path=tmp_path / "r", depth=0)
        write(
            root.path / "notes.md",
            "# Setup\n\nUse the gateway. The key is token: ghp_" + "aB3dE6gH9" * 4 + " keep it.\n",
        )
        report = KnowledgeIndexer(store).scan_root(root)
        assert report.withheld >= 1
        assert store.search_chunks(["gateway"]) == []

    def test_an_unchanged_tree_is_not_reindexed(self, store: Store, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        indexer = KnowledgeIndexer(store)
        indexer.scan_root(root)
        again = indexer.scan_root(root)
        assert again.indexed == 0
        assert again.unchanged == 4

    def test_an_edited_file_is_reindexed_and_replaces_its_chunks(
        self, store: Store, tmp_path: Path
    ) -> None:
        root = build_root(tmp_path)
        indexer = KnowledgeIndexer(store)
        indexer.scan_root(root)
        write(
            root.path / "Prostate MRI" / "CLAUDE.md",
            "# Prostate MRI\n\nSwitched to a transformer decoder for the second round.\n" * 2,
        )
        again = indexer.scan_root(root)
        assert again.indexed == 1
        assert store.search_chunks(["voxel"]) == []
        assert store.search_chunks(["transformer"])

    def test_a_deleted_file_leaves_the_index(self, store: Store, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        indexer = KnowledgeIndexer(store)
        indexer.scan_root(root)
        (root.path / "Albanian News" / "Claude.md").unlink()
        again = indexer.scan_root(root)
        assert again.removed == 1
        assert store.search_chunks(["chrF"]) == []

    def test_a_file_still_being_written_is_left_for_the_next_pass(
        self, store: Store, tmp_path: Path
    ) -> None:
        root = RootConfig(path=tmp_path / "r", depth=0)
        write(
            root.path / "fresh.md", "# Fresh\n\nJust written, still being edited.\n", age_seconds=5
        )
        write(root.path / "old.md", "# Old\n\nWritten long ago and settled for good.\n")
        report = KnowledgeIndexer(store).scan_root(root)
        assert report.too_recent == 1
        assert report.indexed == 1
        assert indexed_uris(store) == {(root.path / "old.md").as_posix()}

    def test_a_missing_folder_changes_nothing(self, store: Store, tmp_path: Path) -> None:
        """An unplugged drive must not look like every file was deleted."""
        root = build_root(tmp_path)
        indexer = KnowledgeIndexer(store)
        indexer.scan_root(root)
        before = store.count_documents()
        gone = RootConfig(path=root.path, kind=root.kind, depth=root.depth)
        for path in sorted(root.path.rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        root.path.rmdir()
        report = indexer.scan_root(gone)
        assert report.error
        assert store.count_documents() == before

    def test_scanning_writes_nothing_inside_the_source(self, store: Store, tmp_path: Path) -> None:
        root = build_root(tmp_path)
        before = {p: p.stat().st_mtime_ns for p in root.path.rglob("*") if p.is_file()}
        names_before = {p.relative_to(root.path) for p in root.path.rglob("*")}
        KnowledgeIndexer(store).scan_root(root)
        assert {p.relative_to(root.path) for p in root.path.rglob("*")} == names_before
        assert {p: p.stat().st_mtime_ns for p in root.path.rglob("*") if p.is_file()} == before


class TestSourcesConfig:
    def test_roots_roundtrip_through_yaml(self, tmp_path: Path) -> None:
        config = SourcesConfig(roots=[])
        assert config.add(RootConfig(path=Path("D:/Project Eris"), kind="competition", depth=1))
        assert not config.add(RootConfig(path=Path("D:/Project Eris"), kind="competition", depth=2))
        config.save(tmp_path)
        loaded = SourcesConfig.load(tmp_path)
        assert [(r.path.as_posix(), r.kind, r.depth) for r in loaded.roots] == [
            ("D:/Project Eris", "competition", 2)
        ]

    def test_a_missing_file_is_no_roots(self, tmp_path: Path) -> None:
        assert SourcesConfig.load(tmp_path).roots == []

    def test_remove(self, tmp_path: Path) -> None:
        config = SourcesConfig(roots=[RootConfig(path=Path("D:/a"))])
        assert config.remove(Path("D:/a"))
        assert not config.remove(Path("D:/a"))
