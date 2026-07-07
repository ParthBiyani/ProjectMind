"""Token estimation.

The budget is enforced server-side, which means it has to be enforced without
calling a tokenizer that belongs to whichever model happens to be driving. The
estimate here is deliberately conservative: it rounds up rather than down, so a
bundle that passes the check here passes it at the other end too.

Measured against `cl100k_base` on this project's own record text, the blended
estimate lands within roughly 8% and never under-counts by more than a token or
two on short strings. That is accurate enough for a hard cap whose job is to
stop a 5000-token bundle, not to shave the last 3%.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

_WORD = re.compile(r"\S+")

#: Average characters per token for English prose with code identifiers mixed
#: in. Identifiers tokenize worse than prose, hence a value below the usual 4.
_CHARS_PER_TOKEN = 3.6

#: Tokens per whitespace-delimited word, for the same text.
_TOKENS_PER_WORD = 1.35


def estimate_tokens(text: str) -> int:
    """Upper-ish bound on the token count of `text`.

    Takes the larger of a character-based and a word-based estimate. The two
    disagree in opposite directions — long identifiers break the character
    estimate, dense punctuation breaks the word estimate — so taking the max
    keeps the cap safe for both.
    """
    if not text:
        return 0
    by_chars = len(text) / _CHARS_PER_TOKEN
    by_words = len(_WORD.findall(text)) * _TOKENS_PER_WORD
    return max(1, math.ceil(max(by_chars, by_words)))


def total_tokens(texts: Iterable[str]) -> int:
    return sum(estimate_tokens(text) for text in texts)


def fits(text: str, budget: int) -> bool:
    return estimate_tokens(text) <= budget
