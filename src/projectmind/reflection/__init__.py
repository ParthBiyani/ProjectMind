"""The reflection loop: how memory maintains itself."""

from projectmind.reflection.contradiction import (
    Contradiction,
    DetectionStats,
    Proposal,
    check,
    detect,
    propose,
    subject_entities,
)
from projectmind.reflection.graph import ReflectionLoop, ReflectionState
from projectmind.reflection.ttl import Support, TtlScan, find_support, scan

__all__ = [
    "Contradiction",
    "DetectionStats",
    "Proposal",
    "ReflectionLoop",
    "ReflectionState",
    "Support",
    "TtlScan",
    "check",
    "detect",
    "find_support",
    "propose",
    "scan",
    "subject_entities",
]
