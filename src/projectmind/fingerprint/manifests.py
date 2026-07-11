"""Manifest parsing.

The dependency set is the strongest cross-project similarity signal available
without running a model, so this is where most of the fingerprint's value comes
from. A Flutter + Supabase + Riverpod project matches your other Flutter +
Supabase projects before any semantic search runs, which is exactly the
"I solved this before, elsewhere" case that per-repository tooling cannot serve.

Every parser is total: a malformed, truncated or unreadable manifest yields
nothing and is logged. A project with a broken `package.json` still gets a
fingerprint from its other manifests, because half a fingerprint is worth more
than an exception on the retrieval path.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from projectmind.logging import get_logger

log = get_logger(__name__)

#: Dependencies that identify a framework. The value is what goes into the
#: fingerprint's `frameworks` set, which is weighted more heavily than raw
#: dependencies because it survives version churn and package renames.
FRAMEWORK_MARKERS: dict[str, str] = {
    # mobile and desktop
    "flutter": "flutter",
    "react-native": "react-native",
    "expo": "react-native",
    # web
    "next": "nextjs",
    "nuxt": "nuxt",
    "react": "react",
    "vue": "vue",
    "svelte": "svelte",
    "@angular/core": "angular",
    "tailwindcss": "tailwindcss",
    # python services
    "fastapi": "fastapi",
    "flask": "flask",
    "django": "django",
    "litestar": "litestar",
    "streamlit": "streamlit",
    "gradio": "gradio",
    # ml
    "torch": "pytorch",
    "pytorch-lightning": "pytorch",
    "tensorflow": "tensorflow",
    "keras": "tensorflow",
    "jax": "jax",
    "scikit-learn": "scikit-learn",
    "xgboost": "gradient-boosting",
    "lightgbm": "gradient-boosting",
    "catboost": "gradient-boosting",
    "ultralytics": "ultralytics",
    "transformers": "transformers",
    "timm": "pytorch",
    "opencv-python": "opencv",
    "opencv-python-headless": "opencv",
    # agents and llm plumbing
    "langgraph": "langgraph",
    "langchain": "langchain",
    "llama-index": "llamaindex",
    "anthropic": "llm-api",
    "openai": "llm-api",
    "mcp": "mcp",
    # data and infra
    "sqlalchemy": "sqlalchemy",
    "psycopg": "postgres",
    "psycopg2-binary": "postgres",
    "asyncpg": "postgres",
    "pgvector": "pgvector",
    "redis": "redis",
    "celery": "celery",
    "supabase": "supabase",
    "supabase_flutter": "supabase",
    "supabase-js": "supabase",
    "@supabase/supabase-js": "supabase",
    "firebase_core": "firebase",
    "firebase-admin": "firebase",
    "cloud_firestore": "firebase",
    # rust / go
    "axum": "axum",
    "actix-web": "actix",
    "tokio": "tokio",
}

#: Directories that are never worth walking into.
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "target",
        ".dart_tool",
        ".gradle",
        ".idea",
        ".vscode",
        "vendor",
        "site-packages",
        ".next",
        ".nuxt",
        "coverage",
        "htmlcov",
    }
)

_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9._-]+)")
_GO_REQUIRE = re.compile(r"^\s*([\w./-]+)\s+v[\d]")
_GRADLE_DEP = re.compile(r"""['"]([\w.-]+):([\w.-]+):""")
_CSPROJ_PACKAGE = re.compile(r'PackageReference\s+Include="([^"]+)"')


@dataclass(frozen=True, slots=True)
class ManifestFacts:
    """What one manifest file says about a project."""

    path: Path
    language: str
    dependencies: tuple[str, ...] = ()

    @property
    def frameworks(self) -> tuple[str, ...]:
        found = {FRAMEWORK_MARKERS[dep] for dep in self.dependencies if dep in FRAMEWORK_MARKERS}
        return tuple(sorted(found))


@dataclass(slots=True)
class ManifestSummary:
    """Every manifest found under a project root, merged."""

    languages: set[str] = field(default_factory=set)
    dependencies: set[str] = field(default_factory=set)
    frameworks: set[str] = field(default_factory=set)
    files: list[Path] = field(default_factory=list)

    def absorb(self, facts: ManifestFacts) -> None:
        self.languages.add(facts.language)
        self.dependencies.update(facts.dependencies)
        self.frameworks.update(facts.frameworks)
        self.files.append(facts.path)

    @property
    def is_empty(self) -> bool:
        return not self.files


def normalise_dependency(name: str) -> str:
    """Strip extras, version specifiers and whitespace; lowercase the rest."""
    cleaned = name.strip().lower()
    for separator in ("[", "(", ";", "@", "=", ">", "<", "!", "~", " "):
        cleaned = cleaned.split(separator, 1)[0]
    return cleaned.strip().strip(",").strip()


def _clean_all(names: Iterable[str]) -> tuple[str, ...]:
    seen = {normalise_dependency(name) for name in names}
    return tuple(sorted(name for name in seen if name and not name.startswith("#")))


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.debug("could not read manifest", extra={"path": str(path), "error": str(exc)})
        return None


# --------------------------------------------------------------------------- #
# Individual parsers. Each returns None when the file says nothing useful.
# --------------------------------------------------------------------------- #


def parse_pubspec(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        log.debug("unparsable pubspec", extra={"path": str(path)})
        return None
    if not isinstance(data, dict):
        return None
    names: list[str] = []
    for section in ("dependencies", "dev_dependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            names.extend(str(key) for key in block)
    return ManifestFacts(path, "dart", _clean_all(names))


def parse_package_json(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        log.debug("unparsable package.json", extra={"path": str(path)})
        return None
    if not isinstance(data, dict):
        return None
    names: list[str] = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            names.extend(str(key) for key in block)
    dependencies = _clean_all(names)
    typed = "typescript" in dependencies or (path.parent / "tsconfig.json").exists()
    return ManifestFacts(path, "typescript" if typed else "javascript", dependencies)


def parse_pyproject(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        log.debug("unparsable pyproject.toml", extra={"path": str(path)})
        return None

    names: list[str] = []
    project = data.get("project", {})
    if isinstance(project, dict):
        names.extend(str(item) for item in project.get("dependencies", []) or [])
        optional = project.get("optional-dependencies", {})
        if isinstance(optional, dict):
            for group in optional.values():
                names.extend(str(item) for item in group or [])

    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict):
        block = poetry.get("dependencies")
        if isinstance(block, dict):
            names.extend(str(key) for key in block if str(key).lower() != "python")

    return ManifestFacts(path, "python", _clean_all(names))


def parse_requirements(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    names: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "git+", "http")):
            continue
        match = _REQUIREMENT.match(stripped)
        if match:
            names.append(match.group(1))
    return ManifestFacts(path, "python", _clean_all(names))


def parse_pipfile(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    names: list[str] = []
    for section in ("packages", "dev-packages"):
        block = data.get(section)
        if isinstance(block, dict):
            names.extend(str(key) for key in block)
    return ManifestFacts(path, "python", _clean_all(names))


def parse_conda_env(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    names: list[str] = []
    for item in data.get("dependencies", []) or []:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            for nested in item.values():
                if isinstance(nested, list):
                    names.extend(str(entry) for entry in nested)
    return ManifestFacts(path, "python", _clean_all(names))


def parse_cargo(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    names: list[str] = []
    for section in ("dependencies", "dev-dependencies", "build-dependencies"):
        block = data.get(section)
        if isinstance(block, dict):
            names.extend(str(key) for key in block)
    return ManifestFacts(path, "rust", _clean_all(names))


def parse_go_mod(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    names = [
        match.group(1) for match in (_GO_REQUIRE.match(line) for line in text.splitlines()) if match
    ]
    return ManifestFacts(path, "go", _clean_all(names))


def parse_gemfile(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    names = re.findall(r"""gem\s+['"]([\w.-]+)['"]""", text)
    return ManifestFacts(path, "ruby", _clean_all(names))


def parse_composer(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    names: list[str] = []
    for section in ("require", "require-dev"):
        block = data.get(section) if isinstance(data, dict) else None
        if isinstance(block, dict):
            names.extend(str(key) for key in block)
    return ManifestFacts(path, "php", _clean_all(names))


def parse_gradle(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    names = [f"{group}:{artifact}" for group, artifact in _GRADLE_DEP.findall(text)]
    language = "kotlin" if path.name.endswith(".kts") else "java"
    return ManifestFacts(path, language, _clean_all(names))


def parse_csproj(path: Path) -> ManifestFacts | None:
    text = _read(path)
    if text is None:
        return None
    return ManifestFacts(path, "csharp", _clean_all(_CSPROJ_PACKAGE.findall(text)))


#: Exact filenames, checked first.
PARSERS_BY_NAME: dict[str, Callable[[Path], ManifestFacts | None]] = {
    "pubspec.yaml": parse_pubspec,
    "package.json": parse_package_json,
    "pyproject.toml": parse_pyproject,
    "pipfile": parse_pipfile,
    "environment.yml": parse_conda_env,
    "environment.yaml": parse_conda_env,
    "cargo.toml": parse_cargo,
    "go.mod": parse_go_mod,
    "gemfile": parse_gemfile,
    "composer.json": parse_composer,
    "build.gradle": parse_gradle,
    "build.gradle.kts": parse_gradle,
}

#: Glob patterns, checked when no exact name matches.
PARSERS_BY_PATTERN: tuple[tuple[re.Pattern[str], Callable[[Path], ManifestFacts | None]], ...] = (
    (re.compile(r"^requirements.*\.txt$"), parse_requirements),
    (re.compile(r"^.*\.csproj$"), parse_csproj),
)

MANIFEST_NAMES = frozenset(PARSERS_BY_NAME)


def parser_for(path: Path) -> Callable[[Path], ManifestFacts | None] | None:
    name = path.name.lower()
    if name in PARSERS_BY_NAME:
        return PARSERS_BY_NAME[name]
    for pattern, parser in PARSERS_BY_PATTERN:
        if pattern.match(name):
            return parser
    return None


def is_manifest(path: Path) -> bool:
    return parser_for(path) is not None


def parse(path: Path) -> ManifestFacts | None:
    """Parse one manifest. Returns None for anything unrecognised or broken."""
    parser = parser_for(path)
    if parser is None:
        return None
    try:
        return parser(path)
    except Exception as exc:  # a bad manifest degrades the fingerprint, nothing more
        log.warning("manifest parser failed", extra={"path": str(path), "error": str(exc)})
        return None


def summarise(paths: Iterable[Path]) -> ManifestSummary:
    summary = ManifestSummary()
    for path in paths:
        facts = parse(path)
        if facts and (facts.dependencies or facts.language):
            summary.absorb(facts)
    return summary
