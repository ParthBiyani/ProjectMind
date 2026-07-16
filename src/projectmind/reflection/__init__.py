"""The reflection loop: how memory maintains itself."""

from projectmind.reflection.contradiction import (
    Contradiction,
    DetectionStats,
    Proposal,
    check,
    detect,
    propose,
)
from projectmind.reflection.ttl import Support, TtlScan, find_support, scan

__all__ = [
    "Contradiction",
    "DetectionStats",
    "Proposal",
    "Support",
    "TtlScan",
    "check",
    "detect",
    "find_support",
    "propose",
    "scan",
]
