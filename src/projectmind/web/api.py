"""What the dashboard asks the daemon, answered from the service.

Every handler is a plain function of the service and a request, returning a status
and a JSON-ready dict, so the whole interface is testable without a socket. The
daemon does the HTTP: it checks the Host header and the bearer token before any of
this runs, and serves the static files (which hold no data) without a token.

Nothing here writes anything the CLI cannot: asking logs an interaction exactly as
the hook would (source `ui`), feedback and profile decisions go through the same
service methods, and a sync is the same background sync.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import UUID

from projectmind import __version__
from projectmind.models import StatementStatus
from projectmind.retrieval.evidence import Found
from projectmind.service import MemoryService
from projectmind.storage import HistoryFilter
from projectmind.storage.base import BundleLogEntry

Reply = tuple[int, dict[str, Any]]

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

#: Headers on every page of the interface. It loads nothing from anywhere else, so
#: it can say so, and it may not be framed by another site.
UI_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def static_file(path: str) -> tuple[bytes, str] | None:
    """A file of the interface by its path under /ui/, or None. Never leaves the folder."""
    name = path.strip("/") or "index.html"
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        return None
    suffix = Path(name).suffix
    if suffix not in STATIC_TYPES:
        return None
    resource = resources.files("projectmind.web").joinpath("static", name)
    if not resource.is_file():
        return None
    return resource.read_bytes(), STATIC_TYPES[suffix]


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _entry(entry: BundleLogEntry, *, full: bool = False) -> dict[str, Any]:
    served = len(entry.profile_ids) + len(entry.episodic_ids) + len(entry.evidence_ids)
    data: dict[str, Any] = {
        "id": str(entry.bundle_id),
        "created_at": _iso(entry.created_at),
        "project": entry.project_key,
        "task_type": entry.task_type,
        "source": entry.source,
        "prompt": entry.prompt_text,
        "prompt_words": entry.prompt_words,
        "served": served,
        "evidence": len(entry.evidence_ids),
        "records": len(entry.episodic_ids),
        "profile": len(entry.profile_ids),
        "tokens": entry.profile_tokens + entry.episodic_tokens + entry.evidence_tokens,
        "latency_ms": round(entry.latency_ms, 1),
        "reason": entry.gate_reason,
        "feedback": entry.feedback,
        "feedback_note": entry.feedback_note,
        "delivered": entry.delivered,
    }
    if full:
        data["rendered"] = entry.rendered_text
        data["trace"] = entry.trace
        data["cwd"] = entry.cwd
        data["embedder"] = entry.embedder
        data["version"] = entry.version
    return data


def _found(item: Found) -> dict[str, Any]:
    evidence = item.evidence
    return {
        "via": item.via,
        "similarity": round(item.similarity, 3),
        "citation": evidence.citation,
        "project": evidence.project_key,
        "source_type": evidence.source_type,
        "title": evidence.title,
        "heading": evidence.heading,
        "excerpt": evidence.excerpt,
    }


@dataclass
class WebApi:
    """The routes under /api/. `memory` is shared with the daemon; callers hold its lock."""

    memory: MemoryService
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    status: Callable[[], dict[str, Any]] = dict
    start_sync: Callable[[], bool] | None = None
    demo: dict[str, Path] | None = None

    @property
    def settings(self) -> Any:
        return self.memory.settings

    def _tidy(self, payload: Any) -> Any:
        """In the demo, show paths relative to the throwaway workspace, not a temp folder."""
        if self.demo is None:
            return payload
        prefix = self.demo["work"].parent.as_posix() + "/"
        raw = json.dumps(payload, default=str)
        for form in (prefix, prefix.replace("/", "\\\\")):
            raw = raw.replace(form, "")
        return json.loads(raw)

    # --- dispatch ----------------------------------------------------------
    def handle(self, method: str, path: str, body: bytes = b"") -> Reply:
        route, _, query = path.partition("?")
        params = dict(part.split("=", 1) for part in query.split("&") if "=" in part)
        parts = [part for part in route.split("/") if part][1:]  # drop "api"
        payload: dict[str, Any] = {}
        if body:
            try:
                loaded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return 400, {"error": "body is not JSON"}
            if not isinstance(loaded, dict):
                return 400, {"error": "body must be a JSON object"}
            payload = loaded
        key = (method, parts[0] if parts else "")
        handlers: dict[
            tuple[str, str], Callable[[list[str], dict[str, str], dict[str, Any]], Reply]
        ]
        handlers = {
            ("GET", "overview"): self.overview,
            ("POST", "ask"): self.ask,
            ("POST", "search"): self.search,
            ("GET", "history"): self.history,
            ("POST", "feedback"): self.feedback,
            ("GET", "knowledge"): self.knowledge,
            ("GET", "profile"): self.profile,
            ("POST", "profile"): self.decide,
            ("GET", "eval"): self.evaluation,
            ("GET", "tour"): self.tour,
            ("GET", "suggestions"): self.suggestions,
            ("POST", "sync"): self.sync,
        }
        handler = handlers.get(key)
        if handler is None:
            return 404, {"error": "not found"}
        status, reply = handler(parts[1:], params, payload)
        # Suggestions carry the real folders, which asking needs to resolve the project.
        return status, reply if key == ("GET", "suggestions") else self._tidy(reply)

    # --- views -------------------------------------------------------------
    def overview(self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]) -> Reply:
        store = self.memory.store
        statements = self.memory.profile.stats().by_status
        semantic = self.memory.evidence.semantic
        return 200, {
            "version": __version__,
            "demo": self.demo is not None,
            "home": str(self.settings.home),
            "started_at": _iso(self.started_at),
            "documents": store.count_documents(),
            "passages": store.count_chunks(),
            "records": store.count_records(),
            "projects": len(store.document_projects()),
            "profile": {str(status): count for status, count in statements.items()},
            "interactions": store.usage_stats().bundles,
            "semantic": {
                "enabled": bool(self.settings.semantic),
                "loaded": semantic.size if semantic is not None else 0,
                "threshold": self.settings.semantic_threshold,
            },
            "embedder": self.settings.embedding_provider,
            "history_mode": self.settings.history,
            "daemon": self.status(),
        }

    def ask(self, _parts: list[str], _params: dict[str, str], body: dict[str, Any]) -> Reply:
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            return 400, {"error": "ask a question"}
        project = str(body.get("project") or "").strip() or None
        started = time.perf_counter()
        bundle = self.memory.get_context(prompt, project or ".", source="ui")
        entry = self.memory.store.get_bundle(bundle.bundle_id)
        return 200, {
            "id": str(bundle.bundle_id),
            "project": bundle.project_key,
            "task_type": str(bundle.task_type) if bundle.task_type else None,
            "reason": bundle.gate_reason,
            "entities": list(bundle.entities),
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
            "tokens": {
                "profile": bundle.profile_tokens,
                "records": bundle.episodic_tokens,
                "evidence": bundle.evidence_tokens,
                "total": bundle.total_tokens,
                "cap": self.settings.total_token_cap,
            },
            "evidence": [item.model_dump(mode="json") for item in bundle.evidence],
            "records": [item.model_dump(mode="json") for item in bundle.episodic],
            "profile": [item.model_dump(mode="json") for item in bundle.profile],
            "rendered": bundle.render(),
            "trace": entry.trace if entry else {},
        }

    def search(self, _parts: list[str], _params: dict[str, str], body: dict[str, Any]) -> Reply:
        query = str(body.get("query") or "").strip()
        if not query:
            return 400, {"error": "search for something"}
        project = str(body.get("project") or "").strip() or None
        limit = max(1, min(int(body.get("limit") or 8), 20))
        started = time.perf_counter()
        found = self.memory.search_sources(query, project_key=project, limit=limit)
        return 200, {
            "results": [_found(item) for item in found],
            "semantic": self.memory.evidence.semantic is not None,
            "latency_ms": round((time.perf_counter() - started) * 1000.0, 1),
        }

    def history(self, parts: list[str], params: dict[str, str], _body: dict[str, Any]) -> Reply:
        if parts:
            try:
                bundle_id = UUID(parts[0])
            except ValueError:
                return 400, {"error": "bad id"}
            entry = self.memory.store.get_bundle(bundle_id)
            if entry is None:
                return 404, {"error": "no such interaction"}
            return 200, _entry(entry, full=True)
        served = {"yes": True, "no": False}.get(params.get("served", ""))
        limit = max(1, min(int(params.get("limit") or 100), 500))
        source = params.get("source") or None
        entries = self.memory.store.find_bundles(
            HistoryFilter(served=served, limit=limit, source=source)
        )
        return 200, {"items": [_entry(entry) for entry in entries]}

    def feedback(self, _parts: list[str], _params: dict[str, str], body: dict[str, Any]) -> Reply:
        try:
            bundle_id = UUID(str(body.get("id")))
        except ValueError:
            return 400, {"error": "bad id"}
        recorded = self.memory.feedback(
            bundle_id, useful=bool(body.get("useful")), note=str(body.get("note") or "")[:500]
        )
        return (200, {"ok": True}) if recorded else (404, {"error": "no such interaction"})

    def knowledge(self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]) -> Reply:
        from projectmind.sources.config import SourcesConfig

        by_type: dict[str, list[int]] = {}
        by_project: dict[str, list[int]] = {}
        for kind, project, documents, passages in self.memory.store.knowledge_summary():
            for bucket, name in ((by_type, kind), (by_project, project)):
                totals = bucket.setdefault(name, [0, 0])
                totals[0] += documents
                totals[1] += passages
        roots: list[dict[str, Any]] = []
        if self.demo is None:
            try:
                config = SourcesConfig.load(self.settings.home)
                roots = [
                    {"path": str(root.path), "kind": root.kind, "depth": root.depth}
                    for root in config.roots
                ]
            except (OSError, ValueError):
                roots = []

        def rows(bucket: dict[str, list[int]]) -> list[dict[str, Any]]:
            ordered = sorted(bucket.items(), key=lambda item: -item[1][1])
            return [{"name": k, "documents": v[0], "passages": v[1]} for k, v in ordered]

        return 200, {
            "by_type": rows(by_type),
            "by_project": rows(by_project)[:25],
            "projects": len(by_project),
            "roots": roots,
            "daemon": self.status(),
        }

    def profile(self, _parts: list[str], params: dict[str, str], _body: dict[str, Any]) -> Reply:
        wanted = params.get("status", "proposed")
        statements = self.memory.profile.all()
        if wanted != "all":
            statements = [item for item in statements if str(item.status) == wanted]
        statements.sort(key=lambda item: (-item.confidence, item.statement))
        return 200, {
            "items": [
                {
                    "id": str(item.id),
                    "statement": item.statement,
                    "category": str(item.category),
                    "confidence": round(item.confidence, 2),
                    "status": str(item.status),
                    "entities": list(item.entities),
                    "evidence": [ref.model_dump(mode="json") for ref in item.evidence_refs[:5]],
                    "created_at": _iso(item.created_at),
                }
                for item in statements
            ]
        }

    def decide(self, parts: list[str], _params: dict[str, str], _body: dict[str, Any]) -> Reply:
        if len(parts) != 2 or parts[1] not in {"approve", "reject"}:
            return 404, {"error": "not found"}
        try:
            statement_id = UUID(parts[0])
        except ValueError:
            return 400, {"error": "bad id"}
        statement = self.memory.profile.get(statement_id)
        if statement is None:
            return 404, {"error": "no such statement"}
        if parts[1] == "approve":
            try:
                updated = self.memory.profile.activate(statement_id)
            except ValueError as exc:
                return 409, {"error": str(exc)}
        else:
            updated = self.memory.profile.add(
                statement.model_copy(update={"status": StatementStatus.REJECTED})
            )
        return 200, {"id": str(updated.id), "status": str(updated.status)}

    def evaluation(
        self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]
    ) -> Reply:
        path = self.settings.home / "eval" / "last-real-report.json"
        if self.demo is not None or not path.is_file():
            return 200, {"real": None, "path": str(path)}
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 200, {"real": None, "path": str(path)}
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        return 200, {"real": report, "path": str(path), "measured_at": _iso(stamp)}

    def tour(self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]) -> Reply:
        if self.demo is None:
            return 404, {"error": "the tour runs only in demo mode (projectmind ui --demo)"}
        from projectmind.demo import run_demo

        steps = []
        for result in run_demo(self.memory, self.demo):
            step = result.step
            passed = (
                result.silent or result.skipped
                if step.expect_silence or "Rename" in step.prompt
                else not result.silent
            )
            steps.append(
                {
                    "title": step.title,
                    "prompt": step.prompt,
                    "project": step.where,
                    "shows": step.shows,
                    "expect_silence": step.expect_silence,
                    "skipped": result.skipped,
                    "reason": result.reason,
                    "citations": list(result.citations),
                    "excerpts": list(result.excerpts),
                    "tokens": result.tokens,
                    "latency_ms": result.latency_ms,
                    "passed": passed,
                }
            )
        return 200, {"steps": steps}

    def suggestions(
        self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]
    ) -> Reply:
        if self.demo is not None:
            from projectmind.demo import STEPS, project_dir

            return 200, {
                "items": [
                    {"prompt": step.prompt, "project": str(project_dir(self.demo, step.where)),
                     "label": step.title}
                    for step in STEPS
                ]
            }  # fmt: skip
        seen: set[str] = set()
        items: list[dict[str, str]] = []
        for entry in self.memory.store.find_bundles(HistoryFilter(served=True, limit=200)):
            text = (entry.prompt_text or "").strip()
            if not text or len(text) > 160 or text.lower() in seen:
                continue
            seen.add(text.lower())
            items.append(
                {"prompt": text, "project": entry.cwd or "", "label": entry.project_key or ""}
            )
            if len(items) >= 6:
                break
        return 200, {"items": items}

    def sync(self, _parts: list[str], _params: dict[str, str], _body: dict[str, Any]) -> Reply:
        if self.start_sync is None:
            return 409, {"error": "sync is not available here"}
        return 202, {"started": self.start_sync()}
