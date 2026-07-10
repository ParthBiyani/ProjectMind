"""Project fingerprinting: cheap, deterministic, no model involved."""

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
from projectmind.fingerprint.scanner import ProjectScanner, ScanResult, scan

__all__ = [
    "FRAMEWORK_MARKERS",
    "IGNORED_DIRECTORIES",
    "ManifestFacts",
    "ManifestSummary",
    "ProjectScanner",
    "ScanResult",
    "is_manifest",
    "normalise_dependency",
    "parse",
    "scan",
    "summarise",
]
