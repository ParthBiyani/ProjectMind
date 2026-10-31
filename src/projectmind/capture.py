"""What the browser extension may send, and what the daemon keeps of it.

The extension is a convenience and not a trust boundary: every rule it applies is
applied again here. A page is stored only if

* capture is switched on (`projectmind browser enable`),
* its domain is not on the never-capture list and is not this machine or the local
  network (pressing "Remember this page" does not override the list),
* it survives the credential scan (a chunk that looks like a secret is dropped),

and what is stored is the page's readable text, cut to a size limit, with its URL
stripped of query and fragment.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from projectmind.logging import get_logger
from projectmind.service import MemoryService
from projectmind.sources import chunking
from projectmind.sources.browser import BrowserConfig, clean_domain, clean_url, is_local
from projectmind.sources.secrets import contains_secret
from projectmind.storage import ChunkRecord, DocumentRecord

log = get_logger(__name__)

ROOT_KEY = "extension"
TABS_URI = "browser://open-tabs"
MAX_TEXT_CHARS = 50_000
MAX_TITLE_CHARS = 300
MAX_TABS = 80
MIN_TEXT_CHARS = 80


def _config(memory: MemoryService) -> BrowserConfig:
    return BrowserConfig.load(memory.settings.home)


def handle_config(memory: MemoryService, _: dict[str, Any]) -> dict[str, Any]:
    """The never-capture list the extension follows, so there is one source of truth."""
    config = _config(memory)
    return {
        "enabled": config.enabled,
        "deny": sorted(config.deny),
        "max_text_chars": MAX_TEXT_CHARS,
    }


def handle_deny(memory: MemoryService, event: dict[str, Any]) -> dict[str, Any]:
    """Add a site to the never-capture list (`add`) or take one off it (`remove`).

    Adding also deletes what was already captured from that site, so "never" covers
    the past as well as the future.
    """
    config = _config(memory)
    removed_documents = 0
    if event.get("add"):
        domain = clean_domain(str(event["add"]))
        if domain is None:
            return {"ok": False, "reason": "not a domain", "deny": sorted(config.deny)}
        config = config.denying(domain)
        removed_documents = forget_domain(memory, domain)
    elif event.get("remove"):
        domain = clean_domain(str(event["remove"]))
        if domain is None or domain not in config.deny:
            return {"ok": False, "reason": "not on the list", "deny": sorted(config.deny)}
        config = config.allowing(domain)
    else:
        return {"ok": False, "reason": "nothing to change", "deny": sorted(config.deny)}
    config.save(memory.settings.home)
    log.info("never-capture list changed", extra={"size": len(config.deny)})
    return {"ok": True, "deny": sorted(config.deny), "forgotten": removed_documents}


def forget_domain(memory: MemoryService, domain: str) -> int:
    """Delete every captured or history page from `domain` and its subdomains."""
    removed = 0
    for root in (ROOT_KEY, "browser"):
        for uri in list(memory.store.document_hashes(root)):
            host = (urlsplit(uri).hostname or "").removeprefix("www.")
            if host == domain or host.endswith("." + domain):
                memory.store.delete_document(uri)
                removed += 1
    return removed


def handle_page(memory: MemoryService, event: dict[str, Any]) -> dict[str, Any]:
    """Store one page's readable text. Returns what was done and why."""
    config = _config(memory)
    if not config.enabled:
        return {"stored": False, "reason": "capture is off (projectmind browser enable)"}

    url = clean_url(str(event.get("url") or ""))
    if url is None:
        return {"stored": False, "reason": "not a web page"}
    host = urlsplit(url).hostname or ""
    explicit = bool(event.get("explicit"))
    if is_local(host):
        return {"stored": False, "reason": "pages on this machine or network are never read"}
    if not config.permits(host):
        return {"stored": False, "reason": "the site is on the never-capture list"}

    text = str(event.get("text") or "")[:MAX_TEXT_CHARS].strip()
    if len(text) < MIN_TEXT_CHARS:
        return {"stored": False, "reason": "no readable text"}
    title = " ".join(str(event.get("title") or "").split())[:MAX_TITLE_CHARS] or url

    chunks = [
        chunk
        for chunk in chunking.chunk_text(text)
        if not contains_secret(f"{title}\n{chunk.text}")
    ]
    withheld = len(chunking.chunk_text(text)) - len(chunks)
    if not chunks:
        return {"stored": False, "reason": "everything looked like a credential"}

    fingerprint = hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()
    if memory.store.document_hashes(ROOT_KEY).get(url) == fingerprint:
        return {"stored": False, "reason": "unchanged"}

    now = datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%d")
    document = DocumentRecord(
        id=uuid4(),
        uri=url,
        source_type="web",
        root=ROOT_KEY,
        project_key="web",
        title=title,
        content_hash=fingerprint,
        mtime=now,
        indexed_at=now,
    )
    records = [
        ChunkRecord(
            id=uuid4(),
            document_id=document.id,
            ordinal=index,
            text=chunk.text,
            heading=title,
            locator=f"captured {stamp}",
        )
        for index, chunk in enumerate(chunks)
    ]
    memory.store.replace_document(document, records)
    log.info("page captured", extra={"host": host, "chunks": len(records), "explicit": explicit})
    return {"stored": True, "chunks": len(records), "withheld": withheld}


def handle_tabs(memory: MemoryService, event: dict[str, Any]) -> dict[str, Any]:
    """Replace the snapshot of what is open right now (never-capture sites left out)."""
    config = _config(memory)
    if not config.enabled:
        return {"stored": False, "reason": "capture is off (projectmind browser enable)"}

    lines: list[str] = []
    for tab in list(event.get("tabs") or [])[:MAX_TABS]:
        if not isinstance(tab, dict):
            continue
        url = clean_url(str(tab.get("url") or ""))
        if url is None or not config.permits(urlsplit(url).hostname or ""):
            continue
        title = " ".join(str(tab.get("title") or "").split())[:MAX_TITLE_CHARS] or url
        lines.append(f"{title} - {url}")

    if not lines:
        memory.store.delete_document(TABS_URI)
        return {"stored": False, "reason": "no readable tabs open"}

    body = "Open in the browser now:\n" + "\n".join(lines)
    if contains_secret(body):
        return {"stored": False, "reason": "looked like a credential"}
    fingerprint = hashlib.sha1(body.encode("utf-8"), usedforsecurity=False).hexdigest()
    if memory.store.document_hashes(ROOT_KEY).get(TABS_URI) == fingerprint:
        return {"stored": False, "reason": "unchanged"}

    now = datetime.now(UTC)
    document = DocumentRecord(
        id=uuid4(),
        uri=TABS_URI,
        source_type="web",
        root=ROOT_KEY,
        project_key="web",
        title="Open browser tabs",
        content_hash=fingerprint,
        mtime=now,
        indexed_at=now,
    )
    memory.store.replace_document(
        document,
        [
            ChunkRecord(
                id=uuid4(),
                document_id=document.id,
                ordinal=0,
                text=body,
                heading="Open browser tabs",
                locator=f"open at {now:%Y-%m-%d %H:%M}",
            )
        ],
    )
    return {"stored": True, "tabs": len(lines)}


HANDLERS = {
    "config": handle_config,
    "deny": handle_deny,
    "page": handle_page,
    "tabs": handle_tabs,
}
