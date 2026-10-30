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


#: Government identifiers and payment numbers. A knowledge base of someone's work
#: has no business holding these, and it is searched on every prompt.
#:
#: These are deliberately narrow. Experiment logs and result files are full of
#: digit runs, and a Luhn check alone passes about one in ten random ones, so a
#: bare run of digits is never treated as a card number: it has to be grouped the
#: way cards are printed, or sit next to a word that says it is one.
# Exactly three groups of four separated by spaces: not a window inside a longer run.
_AADHAAR = re.compile(r"(?<!\d)(?<!\d )[2-9]\d{3} \d{4} \d{4}(?! ?\d)")
_AADHAAR_CONTEXT = re.compile(
    r"(?i)(?:aadhaar|aadhar|\buid\b|\bvid\b)[^\n]{0,30}?(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)"
)
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_SSN = re.compile(r"(?i)(?:\bssn\b|social security)[^\n]{0,30}?(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_CARD_GROUPED = re.compile(r"(?<![\d-])(?:\d{4}[ -]){3}\d{4}(?![\d-])")
_CARD_AMEX = re.compile(r"(?<![\d-])3[47]\d{2}[ -]\d{6}[ -]\d{5}(?![\d-])")
_CARD_CONTEXT = re.compile(
    r"(?i)(?:card|visa|mastercard|amex|credit|debit|cvv|\bpan no)[^\n]{0,30}?"
    r"(?<!\d)(\d{13,19})(?!\d)"
)
_ISSUER = re.compile(r"^(?:4|5[1-5]|2[2-7]|3[47]|6011|65|35|60|81|82)")

#: File names of identity and financial documents. Skipped before they are opened.
SENSITIVE_NAME = re.compile(
    r"aadh?a+r|aadhaar|\bpan[ _-]?card\b|passport|voter[ _-]?(?:id|list|card)|"
    r"driving[ _-]?licen[cs]e|ration[ _-]?card|birth[ _-]?certificate|"
    r"\bkyc\b|\bssn\b|tax[ _-]?return|\bitr[ _-]?v?\b|form[ _-]?16|pay[ _-]?slip|salary[ _-]?slip|"
    r"bank[ _-]?statement|cheque|insurance[ _-]?policy|mark[ _-]?sheet|\bdomicile\b|"
    r"identity[ _-]?(?:card|proof)|national[ _-]?id|personal[ _-]?(?:docs?|documents?)",
    re.IGNORECASE,
)


def is_sensitive_name(name: str) -> bool:
    """Whether a file name says it is an identity or financial document."""
    return SENSITIVE_NAME.search(name) is not None


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _valid_card(digits: str) -> bool:
    return 13 <= len(digits) <= 19 and bool(_ISSUER.match(digits)) and _luhn(digits)


def contains_identifier(text: str) -> bool:
    """True if the text holds an Aadhaar, PAN, SSN or payment card number."""
    if _AADHAAR.search(text) or _AADHAAR_CONTEXT.search(text):
        return True
    if _PAN.search(text) or _SSN.search(text):
        return True
    for pattern in (_CARD_GROUPED, _CARD_AMEX):
        for match in pattern.finditer(text):
            if _valid_card(re.sub(r"\D", "", match.group())):
                return True
    return any(_valid_card(match.group(1)) for match in _CARD_CONTEXT.finditer(text))


def contains_secret(text: str) -> bool:
    """True if the text must be withheld: a credential, or a personal identifier."""
    if contains_identifier(text):
        return True
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
