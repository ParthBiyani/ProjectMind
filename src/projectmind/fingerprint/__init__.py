"""Project fingerprinting: cheap, deterministic, no model involved."""

from projectmind.fingerprint.cache import CacheLookup, FingerprintCache
from projectmind.fingerprint.manifests import (
    FRAMEWORK_MARKERS,
    IGNORED_DIRECTORIES,
    ManifestFacts,
    ManifestSummary,
    is_manifest,
    normalise_dependency,
    parse,
    summarise,
)
from projectmind.fingerprint.matching import ProjectMatch, compare, rank_siblings, similarity_index
from projectmind.fingerprint.scanner import ProjectScanner, ScanResult, scan

__all__ = [
    "FRAMEWORK_MARKERS",
    "IGNORED_DIRECTORIES",
    "CacheLookup",
    "FingerprintCache",
    "ManifestFacts",
    "ManifestSummary",
    "ProjectMatch",
    "ProjectScanner",
    "ScanResult",
    "compare",
    "is_manifest",
    "normalise_dependency",
    "parse",
    "rank_siblings",
    "scan",
    "similarity_index",
    "summarise",
]
