"""Caching fingerprints.

A fingerprint costs 20-300 ms to compute. That is affordable once per project
and not affordable on every prompt, so it is computed on first use and cached
until the thing it is derived from changes.

**Invalidation, and its one honest gap.** The cache stores the list of manifest
files it found and a content hash over them. Checking freshness re-hashes just
those files, which is a handful of small reads rather than a directory walk.
That detects every change to a manifest exactly.

What it cannot detect is a *new* manifest appearing somewhere the last walk
found none — spotting that needs another walk, which is the cost being avoided.
A time-to-live covers it: any entry older than `ttl_hours` is rescanned in full.
So a dependency change shows up immediately, and a newly-introduced manifest
shows up within a day, or straight away with `projectmind fingerprint refresh`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from projectmind.fingerprint.scanner import ProjectScanner, manifest_digest
from projectmind.logging import get_logger
from projectmind.models import Fingerprint, utcnow
from projectmind.storage import Store

log = get_logger(__name__)

DEFAULT_TTL_HOURS = 24


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """A fingerprint plus how it was obtained. `reason` is for `--explain`."""

    fingerprint: Fingerprint
    hit: bool
    reason: str

    @property
    def missed(self) -> bool:
        return not self.hit


class FingerprintCache:
    """Store-backed fingerprint cache with content-hash invalidation."""

    def __init__(
        self,
        store: Store,
        scanner: ProjectScanner | None = None,
        *,
        ttl_hours: int = DEFAULT_TTL_HOURS,
    ) -> None:
        self.store = store
        self.scanner = scanner or ProjectScanner()
        self.ttl = timedelta(hours=ttl_hours)

    def get(self, project_key: str, root: str | Path) -> CacheLookup:
        """Return a fingerprint for `root`, computing one only when needed."""
        path = Path(root).expanduser()
        cached = self.store.get_cached_fingerprint(project_key)

        if cached is not None:
            stored_hash, fingerprint = cached
            reason = self._staleness(fingerprint, stored_hash, path)
            if reason is None:
                return CacheLookup(fingerprint, True, "cache hit")
            log.info("fingerprint cache miss", extra={"project": project_key, "reason": reason})
            return CacheLookup(self.refresh(project_key, path), False, reason)

        return CacheLookup(self.refresh(project_key, path), False, "not cached")

    def refresh(self, project_key: str, root: str | Path) -> Fingerprint:
        """Recompute and store, unconditionally."""
        result = self.scanner.scan(root)
        self.store.put_cached_fingerprint(project_key, result.fingerprint)
        log.info(
            "fingerprint computed",
            extra={
                "project": project_key,
                "files": result.files_seen,
                "languages": list(result.fingerprint.languages),
                "dependencies": len(result.fingerprint.dependencies),
                "truncated": result.truncated,
            },
        )
        return result.fingerprint

    def invalidate(self, project_key: str) -> None:
        self.store.invalidate_fingerprint(project_key)

    # --- freshness ---------------------------------------------------------
    def _staleness(self, fingerprint: Fingerprint, stored_hash: str, root: Path) -> str | None:
        """Why the cached entry cannot be used, or None when it can."""
        age = utcnow() - fingerprint.computed_at
        if age > self.ttl:
            return f"entry is {age.days}d{age.seconds // 3600}h old"

        if not fingerprint.manifest_files:
            # Nothing to re-hash. The TTL above is the only guard for these, and
            # they are the projects most likely to gain a manifest later.
            return None

        current = manifest_digest(root, fingerprint.manifest_files)
        if current is None:
            return "a manifest that was there before has gone"
        if current != stored_hash:
            return "a manifest changed"
        return None
