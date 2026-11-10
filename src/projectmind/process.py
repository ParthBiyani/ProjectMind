"""Flags for child processes.

The daemon runs under pythonw, which has no console. Without CREATE_NO_WINDOW
Windows gives every console child (git, schtasks) its own window, which flashes
on screen and steals focus. The flag is 0 elsewhere, which subprocess accepts.
"""

from __future__ import annotations

import subprocess

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
