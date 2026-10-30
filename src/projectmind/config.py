"""Settings.

Resolution order is environment, then `.env`, then the defaults here. Every
default is chosen so that a machine with no configuration at all still gets a
working, useful server: SQLite under the home directory, heuristic analysis, no
network calls.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_HOME = Path.home() / ".projectmind"


class Settings(BaseSettings):
    """Runtime configuration for every component."""

    model_config = SettingsConfigDict(
        env_prefix="PROJECTMIND_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- locations ---------------------------------------------------------
    home: Path = Field(
        default=DEFAULT_HOME,
        description="Where the database, fingerprint cache and logs live.",
    )
    db_url: str | None = Field(
        default=None,
        description="A postgresql:// URL selects the Postgres backend. Unset means SQLite.",
    )

    # --- budget, from PRD section 6.3 --------------------------------------
    profile_token_cap: int = 800
    episodic_token_cap: int = 1500
    total_token_cap: int = 2300
    max_profile_statements: int = 15
    max_episodic_records: int = 5

    # --- evidence from the user's own files and notes ------------------------
    evidence_token_cap: int = Field(
        default=1_200, description="Most tokens of source excerpts served in one bundle."
    )
    max_evidence: int = 4
    evidence_coverage_floor: float = Field(
        default=0.6,
        description=(
            "A source excerpt is served only if it contains at least this share of the "
            "question's distinctive terms (weighted by how rare each term is)."
        ),
    )

    # --- confidence bars, from PRD section 2 -------------------------------
    profile_activation_confidence: float = 0.85
    episodic_serve_confidence: float = 0.60

    # --- ranking -----------------------------------------------------------
    cross_project_boost: float = Field(
        default=1.45,
        description=(
            "Multiplier applied to records from other projects. Without it a "
            "mediocre same-repo result outranks a strong cross-repo one, which "
            "is the entire failure mode this system exists to avoid. Swept "
            "against the eval set in Phase 4: 1.25 lost the cross-project "
            "target once the precision floors were tight enough to matter, "
            "1.70 met it but pushed false injection to 0.33."
        ),
    )
    recency_half_life_days: float = 240.0
    fingerprint_weight: float = Field(
        default=0.6,
        description="Share of the ranking score driven by fingerprint similarity.",
    )
    lexical_weight: float = 0.5

    # --- gate --------------------------------------------------------------
    trivial_prompt_max_words: int = 12
    min_entity_overlap: int = 1

    # --- reflection, from PRD section 5 ------------------------------------
    contradiction_window_days: int = 90
    contradiction_threshold: int = 3
    review_batch_interval_days: int = 30

    # --- optional external services ----------------------------------------
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PROJECTMIND_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
        description="Unlocks LLM extraction and reflection. Everything works without it.",
    )
    anthropic_model: str = "claude-haiku-4-5-20251001"
    github_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PROJECTMIND_GITHUB_TOKEN", "GITHUB_TOKEN"),
        description="Only needed for the GitHub source. Local repositories need nothing.",
    )

    # --- privacy -----------------------------------------------------------
    excluded_repos: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        description="Repository names or glob patterns that are never read or ingested.",
    )
    authors: Annotated[tuple[str, ...], NoDecode] = Field(
        default=(),
        description=(
            "Emails whose commits are yours (comma separated), in addition to git's "
            "user.email. Use it for old addresses and noreply addresses."
        ),
    )
    embedding_provider: str = Field(
        default="auto",
        description="auto | fastembed | hashing. `auto` uses fastembed when installed.",
    )
    embedding_dimensions: int = 384

    log_level: str = "INFO"

    daemon_port: int = Field(
        default=47615,
        description=(
            "Port for the local daemon. Fixed by default so the browser extension, which "
            "cannot read files, always knows where to find it. 0 picks a free one."
        ),
    )
    daemon_sync_minutes: float = Field(
        default=30.0,
        description="Minutes between the daemon's background syncs. 0 turns the scheduler off.",
    )

    history: Literal["full", "minimal", "off"] = Field(
        default="full",
        description=(
            "Interaction history. `full` keeps each prompt, the exact text served and "
            "the retrieval trace, locally, so answers can be audited and replayed. "
            "`minimal` keeps hashed metadata only. `off` keeps nothing. Projects "
            "matching PROJECTMIND_EXCLUDED_REPOS are always treated as `minimal`."
        ),
    )

    @field_validator("home", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("excluded_repos", "authors", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    # --- derived -----------------------------------------------------------
    @property
    def uses_postgres(self) -> bool:
        return bool(self.db_url and self.db_url.startswith(("postgres://", "postgresql://")))

    @property
    def sqlite_path(self) -> Path:
        return self.home / "projectmind.db"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def log_dir(self) -> Path:
        return self.home / "logs"

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    def ensure_home(self) -> Path:
        """Create the directory tree. Idempotent."""
        for directory in (self.home, self.cache_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self.home

    def describe_backend(self) -> str:
        return "postgres+pgvector" if self.uses_postgres else f"sqlite ({self.sqlite_path})"


def env_file() -> Path:
    """The one `.env` ProjectMind reads: the one in its own home.

    Never the working directory's. A hook and an MCP server run with whatever
    project the user happens to be in as their cwd, so reading `.env` from there
    would let any repository's `.env` redirect the database or switch settings,
    and would make the same installation behave differently per project.
    """
    home = os.environ.get("PROJECTMIND_HOME")
    return (Path(home).expanduser() if home else DEFAULT_HOME) / ".env"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached; call :func:`reset_settings` in tests."""
    # `_env_file` is a pydantic-settings init option that the static signature hides.
    return Settings(_env_file=env_file())  # type: ignore[call-arg]


def reset_settings() -> None:
    get_settings.cache_clear()
