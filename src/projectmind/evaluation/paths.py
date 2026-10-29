"""Where the evaluation files live.

The package used to find `eval/` relative to its own location, which is only
right for an editable install of a checkout. Installed from a wheel, that path
points into site-packages and every `eval` command fails with a file-not-found.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_VAR = "PROJECTMIND_EVAL_DIR"


def eval_dir() -> Path:
    """The directory holding `queries.yaml` and `fixtures/`.

    In order: `PROJECTMIND_EVAL_DIR`, the checkout this package was imported
    from, then `./eval`. Raises with the places it looked when none has one.
    """
    candidates: list[Path] = []
    if override := os.environ.get(ENV_VAR):
        candidates.append(Path(override).expanduser())
    candidates.append(Path(__file__).resolve().parents[3] / "eval")
    candidates.append(Path.cwd() / "eval")

    for candidate in candidates:
        if (candidate / "queries.yaml").is_file():
            return candidate
    looked = ", ".join(str(item) for item in candidates)
    raise FileNotFoundError(
        f"no evaluation set found (looked in {looked}); "
        f"set {ENV_VAR} or run from a checkout of the repository"
    )
