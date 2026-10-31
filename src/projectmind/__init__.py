"""ProjectMind: a cross-project engineering memory layer for coding agents."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

__version__ = "0.3.0"

if TYPE_CHECKING:  # pragma: no cover - names resolved lazily at runtime
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

#: Public names and the module each lives in. They are loaded on first use, so
#: that `import projectmind.hook_entry` costs a few milliseconds instead of the
#: couple of hundred it takes to import the data models and settings. A hook runs
#: before every prompt; its start-up time is paid hundreds of times a day.
_EXPORTS = {
    "Settings": "projectmind.config",
    "get_settings": "projectmind.config",
    **dict.fromkeys(
        (
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
            "SourceType",
            "StatementStatus",
            "TaskType",
        ),
        "projectmind.models",
    ),
}

__all__ = ["__version__", *sorted(_EXPORTS)]


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'projectmind' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value
