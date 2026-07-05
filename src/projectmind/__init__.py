"""ProjectMind: a cross-project engineering memory layer for coding agents."""

from projectmind.config import Settings, get_settings
from projectmind.models import (
    Category,
    ContextBundle,
    EpisodicRecord,
    EvidenceRef,
    Fingerprint,
    Origin,
    ProfileStatement,
    Project,
    RecordType,
    Resolution,
    ReviewItem,
    ReviewKind,
    ServedRecord,
    ServedStatement,
    SourceType,
    StatementStatus,
    TaskType,
)

__version__ = "0.0.1"

__all__ = [
    "Category",
    "ContextBundle",
    "EpisodicRecord",
    "EvidenceRef",
    "Fingerprint",
    "Origin",
    "ProfileStatement",
    "Project",
    "RecordType",
    "Resolution",
    "ReviewItem",
    "ReviewKind",
    "ServedRecord",
    "ServedStatement",
    "Settings",
    "SourceType",
    "StatementStatus",
    "TaskType",
    "__version__",
    "get_settings",
]
