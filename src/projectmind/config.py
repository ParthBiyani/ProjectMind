"""Settings.

Resolution order is environment, then `.env`, then the defaults here. Every
default is chosen so that a machine with no configuration at all still gets a
working, useful server: SQLite under the home directory, heuristic analysis, no
network calls.
"""

from __future__ import annotations

import functools
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HOME = Path.home() / ".projectmind"


class Settings(BaseSettings):
    """Runtime configuration for every component."""

    model_config = SettingsConfigDict(
        env_prefix="PROJECTMIND_",
        env_file=".env",
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

    # --- confidence bars, from PRD section 2 -------------------------------
    profile_activation_confidence: float = 0.85
    episodic_serve_confidence: float = 0.60

    # --- ranking -----------------------------------------------------------
    cross_project_boost: float = Field(
        default=1.25,
        description=(
            "Multiplier applied to records from other projects. Without it a "
            "mediocre same-repo result outranks a strong cross-repo one, which "
            "is the entire failure mode this system exists to avoid."
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
    excluded_repos: tuple[str, ...] = Field(
        default=(),
        description="Repository names or glob patterns that are never read or ingested.",
    )
    embedding_provider: str = Field(
        default="auto",
        description="auto | fastembed | hashing. `auto` uses fastembed when installed.",
    )
    embedding_dimensions: int = 384

    log_level: str = "INFO"

    @field_validator("home", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("excluded_repos", mode="before")
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


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached; call :func:`reset_settings` in tests."""
    return Settings()


def reset_settings() -> None:
    get_settings.cache_clear()
