"""Run background work politely.

Scanning thousands of files is not urgent, and it shares the machine with an
editor and whatever agent is running. Lowering the priority of the process
keeps it out of the way; the work simply takes a little longer.
"""

from __future__ import annotations

import os
from typing import Any

_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def lower_priority() -> bool:
    """Drop this process below normal priority. Returns whether it worked."""
    try:
        nice: Any = getattr(os, "nice", None)
        if nice is not None:  # POSIX
            nice(10)
            return True
        import ctypes

        kernel32: Any = getattr(ctypes, "windll").kernel32  # noqa: B009 - absent off Windows
        handle = kernel32.GetCurrentProcess()
        return bool(kernel32.SetPriorityClass(handle, _BELOW_NORMAL_PRIORITY_CLASS))
    except (OSError, AttributeError):
        return False
