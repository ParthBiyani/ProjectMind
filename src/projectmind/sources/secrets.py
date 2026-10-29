"""Credentials must never reach the index.

The knowledge base reads whole project folders, and project folders contain
`.env` files, notebooks with a token pasted into a cell, and configs with keys
in them. The rule is blunt on purpose: a chunk that looks like it contains a
credential is **dropped**, not redacted. A redacted chunk still tells a reader
where the secret was, and a partial redaction is how secrets leak.

False positives cost one chunk of context. False negatives cost a credential in
a database that is searched on every prompt.
"""

from __future__ import annotations

import re

#: Shapes of well-known credentials. Specific prefixes, so ordinary prose and
#: hashes do not trip them.
_TOKEN_SHAPES = re.compile(
    r"""
    sk-ant-[A-Za-z0-9_\-]{20,}
    | sk-[A-Za-z0-9]{32,}
    | AIza[0-9A-Za-z_\-]{30,}
    | gh[pousr]_[A-Za-z0-9]{30,}
    | github_pat_[A-Za-z0-9_]{40,}
    | AKIA[0-9A-Z]{16}
    | xox[abprs]-[A-Za-z0-9\-]{20,}
    | hf_[A-Za-z0-9]{30,}
    | -----BEGIN\ (?:RSA\ |EC\ |OPENSSH\ |DSA\ |PGP\ )?PRIVATE\ KEY-----
    | eyJ[A-Za-z0-9_\-]{15,}\.eyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{10,}
    """,
    re.VERBOSE,
)

#: `name = value` where the name says it is a secret and the value looks like one.
_ASSIGNED_SECRET = re.compile(
    r"""
    (?:api[_-]?key|secret|token|passwd|password|private[_-]?key|access[_-]?key|auth)
    [A-Za-z0-9_\-]*\s*["']?\s*[:=]\s*["']?
    (?P<value>[A-Za-z0-9_\-/+=.]{16,})
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Values that name a secret rather than being one.
_PLACEHOLDERS = re.compile(
    r"^(?:your|my|the|example|sample|dummy|changeme|placeholder|xxx+|\*+|<|\$\{?|os\.|env|process\.)",
    re.IGNORECASE,
)


def contains_secret(text: str) -> bool:
    """True if the text looks like it holds a credential."""
    if _TOKEN_SHAPES.search(text):
        return True
    for match in _ASSIGNED_SECRET.finditer(text):
        value = match.group("value")
        if _PLACEHOLDERS.match(value):
            continue
        if _looks_random(value):
            return True
    return False


def _looks_random(value: str) -> bool:
    """Mixed character classes and no dictionary shape: a key, not a word."""
    classes = sum(
        (
            any(ch.islower() for ch in value),
            any(ch.isupper() for ch in value),
            any(ch.isdigit() for ch in value),
        )
    )
    return classes >= 2 and len(set(value)) >= 6
