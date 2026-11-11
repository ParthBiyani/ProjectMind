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
from collections.abc import Callable
from dataclasses import dataclass

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


# --------------------------------------------------------------------------- #
# Redaction
# --------------------------------------------------------------------------- #
#
# Nothing sensitive is stored, shown or sent on. The sensitive *value* is replaced by a
# marker that says what kind it was ("[REDACTED:password]") and the rest of the text is
# kept, so a note about a login problem stays searchable without the login. The result
# is then scanned again; if anything still looks like a secret the text is dropped
# whole, so redaction can only ever make the outcome safer than dropping was.


@dataclass(frozen=True, slots=True)
class Redaction:
    """Text with its sensitive values replaced, and which kinds were replaced."""

    text: str
    kinds: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.kinds)


def _marker(kind: str) -> str:
    return f"[REDACTED:{kind}]"


_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----.*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
#: `scheme://user:password@host`: the password part only.
_URL_CREDENTIALS = re.compile(
    r"(?P<head>\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:)(?P<secret>[^\s/@]+)(?=@)"
)
_AUTH_HEADER = re.compile(
    r"(?i)(?P<head>\b(?:bearer|basic)\s+)(?P<secret>[A-Za-z0-9._~+/=\-]{16,})"
)
_PASSWORD_PHRASE = re.compile(
    r"(?i)(?P<head>\b(?:password|passwd|pwd|passcode|passphrase)\b\s*(?:is|=|:|->|=>)\s*"
    r"[\"'`]?)(?P<secret>[^\s\"'`,;<>]{3,})"
)
_LOGIN_PHRASE = re.compile(
    r"(?i)(?P<head>\b(?:login|log-in|username|user[ _-]?name|user[ _-]?id|userid|e-?mail)\b"
    r"\s*(?:is|=|:)\s*[\"'`]?)(?P<secret>[^\s\"'`,;<>]{3,})"
)
_ONE_TIME_CODE = re.compile(
    r"(?i)(?P<head>\b(?:otp|one[- ]time (?:password|code)|verification code|cvv|cvc|"
    r"security code)\b[^\n\d]{0,20})(?P<secret>\d{3,8})\b"
)
#: How far from a password a login name is still part of the same credential pair.
_PAIR_WINDOW = 160

#: Values after "password:" that describe a password instead of being one.
_NOT_A_PASSWORD = frozenset(
    {
        "str", "string", "int", "integer", "none", "null", "nil", "true", "false",
        "required", "optional", "hidden", "here", "masked", "redacted", "your", "yours",
        "the", "a", "an", "it", "that", "this", "not", "set", "used", "reset", "changed",
        "wrong", "incorrect", "invalid", "missing", "empty", "stored", "saved", "hash",
        "hashed", "field", "input", "type", "text", "value", "variable", "env",
        "environment", "secret", "password", "passwd", "pwd", "undefined", "unknown", "x",
        "xxx", "too", "short", "long", "weak", "strong", "same", "different", "correct",
        "valid", "expired",
    }
)  # fmt: skip


def _is_placeholder(value: str, names: frozenset[str] = frozenset()) -> bool:
    lowered = value.lower().strip("*._-")
    if not lowered or lowered in _NOT_A_PASSWORD or lowered in names:
        return True
    if value.startswith("[REDACTED") or _PLACEHOLDERS.match(value):
        return True
    # A code expression (`get_password()`, `config.password`, `values["x"]`), not a literal.
    return bool(re.match(r"^[A-Za-z_][\w.]*(?:[(\[]|\.[A-Za-z_])", value))


def _replace(text: str, pattern: re.Pattern[str], kind: str, found: list[str]) -> str:
    """Replace the `secret` group of every match unless it is only a placeholder."""

    def swap(match: re.Match[str]) -> str:
        head = match.group("head")
        names = frozenset(word.lower() for word in re.findall(r"[A-Za-z]+", head))
        if _is_placeholder(match.group("secret"), names):
            return match.group(0)
        found.append(kind)
        return head + _marker(kind)

    return pattern.sub(swap, text)


def _sub_identifier(
    text: str,
    pattern: re.Pattern[str],
    kind: str,
    found: list[str],
    *,
    digits: re.Pattern[str] | None = None,
    valid: Callable[[str], bool] | None = None,
) -> str:
    def swap(match: re.Match[str]) -> str:
        whole = match.group(0)
        target = digits.search(whole) if digits else None
        number = target.group(0) if target else whole
        if valid is not None and not valid(re.sub(r"\D", "", number)):
            return whole
        found.append(kind)
        return whole.replace(number, _marker(kind), 1)

    return pattern.sub(swap, text)


_AADHAAR_DIGITS = re.compile(r"[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}")
_SSN_DIGITS = re.compile(r"\d{3}-\d{2}-\d{4}")
_CARD_DIGITS = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def redact(text: str) -> Redaction:
    """Replace credentials, login details and personal identifiers with markers."""
    found: list[str] = []

    def keys(match: re.Match[str]) -> str:
        found.append("private-key")
        return _marker("private-key")

    def shapes(match: re.Match[str]) -> str:
        found.append("token")
        return _marker("token")

    out = _PRIVATE_KEY_BLOCK.sub(keys, text)
    out = _TOKEN_SHAPES.sub(shapes, out)
    out = _replace(out, _URL_CREDENTIALS, "password", found)
    out = _replace(out, _AUTH_HEADER, "token", found)

    # An assigned secret: keep the name, drop the value.
    def assigned(match: re.Match[str]) -> str:
        value = match.group("value")
        if _PLACEHOLDERS.match(value) or not _looks_random(value):
            return match.group(0)
        found.append("secret")
        return match.group(0).replace(value, _marker("secret"), 1)

    out = _ASSIGNED_SECRET.sub(assigned, out)

    # A password, and the login name written next to it.
    password_spans = [
        (m.start(), m.end())
        for m in _PASSWORD_PHRASE.finditer(out)
        if not _is_placeholder(m.group("secret"))
    ]
    out = _replace(out, _PASSWORD_PHRASE, "password", found)
    if password_spans:
        for start, end in reversed(password_spans):
            low, high = max(0, start - _PAIR_WINDOW), min(len(out), end + _PAIR_WINDOW)
            window = _replace(out[low:high], _LOGIN_PHRASE, "login", found)
            out = out[:low] + window + out[high:]

    out = _replace(out, _ONE_TIME_CODE, "one-time-code", found)

    out = _sub_identifier(out, _AADHAAR, "aadhaar", found)
    out = _sub_identifier(out, _AADHAAR_CONTEXT, "aadhaar", found, digits=_AADHAAR_DIGITS)
    out = _sub_identifier(out, _PAN, "pan", found)
    out = _sub_identifier(out, _SSN, "ssn", found, digits=_SSN_DIGITS)
    out = _sub_identifier(out, _CARD_GROUPED, "card", found, valid=_valid_card)
    out = _sub_identifier(out, _CARD_AMEX, "card", found, valid=_valid_card)
    out = _sub_identifier(out, _CARD_CONTEXT, "card", found, digits=_CARD_DIGITS, valid=_valid_card)

    return Redaction(out, tuple(dict.fromkeys(found)) if found else ())


def scrub(text: str) -> Redaction | None:
    """Redact, then check nothing sensitive is left. None means the text cannot be kept."""
    result = redact(text)
    return None if contains_secret(result.text) else result


def redact_text(text: str) -> str:
    """For anything leaving ProjectMind: redacted, or withheld if it cannot be made safe."""
    result = scrub(text)
    return result.text if result is not None else "[withheld: looked like a credential]"
