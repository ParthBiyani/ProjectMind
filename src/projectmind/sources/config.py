"""Which folders are knowledge sources.

`~/.projectmind/sources.yaml` lists the roots to read. A root is a folder; each
of its children (at `depth`) is treated as one project, whether or not it is a
git repository. A new project or competition folder dropped under a root is
therefore picked up on the next scan with no configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

FILENAME = "sources.yaml"
KINDS = ("project", "competition", "work", "learning", "career", "transcripts")


@dataclass(frozen=True, slots=True)
class RootConfig:
    path: Path
    kind: str = "project"
    #: How many levels below the root a project folder sits. 1 means each child
    #: of the root is a project; 0 means the root itself is a single project.
    depth: int = 1

    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    @property
    def key(self) -> str:
        """Stable identity of the root in the store."""
        return self.path.as_posix()


@dataclass(slots=True)
class SourcesConfig:
    roots: list[RootConfig]

    @classmethod
    def load(cls, home: Path) -> SourcesConfig:
        path = home / FILENAME
        if not path.exists():
            return cls(roots=[])
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roots = [
            RootConfig(
                path=Path(str(entry["path"])).expanduser(),
                kind=str(entry.get("kind", "project")),
                depth=int(entry.get("depth", 1)),
            )
            for entry in raw.get("roots", [])
            if isinstance(entry, dict) and entry.get("path")
        ]
        return cls(roots=roots)

    def save(self, home: Path) -> Path:
        home.mkdir(parents=True, exist_ok=True)
        path = home / FILENAME
        payload = {
            "roots": [
                {"path": root.path.as_posix(), "kind": root.kind, "depth": root.depth}
                for root in self.roots
            ]
        }
        path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        return path

    def add(self, root: RootConfig) -> bool:
        """Add or update a root. True if it was new."""
        for index, existing in enumerate(self.roots):
            if existing.key == root.key:
                self.roots[index] = root
                return False
        self.roots.append(root)
        return True

    def remove(self, path: Path) -> bool:
        target = path.as_posix()
        kept = [root for root in self.roots if root.key != target]
        removed = len(kept) != len(self.roots)
        self.roots = kept
        return removed
