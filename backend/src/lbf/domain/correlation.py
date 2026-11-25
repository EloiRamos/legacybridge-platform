"""Correlation values and SKARVIK message ids (ADR-F05).

``correlation_value`` = ``"LB"`` + 10 Crockford-base32 characters encoding 50 random bits: exactly
12 characters, which fits HALDEN's ``ref`` field. It is a **correlation key, not an idempotency key**.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CORRELATION_PREFIX = "LB"
CORRELATION_LENGTH = 12
CORRELATION_RE = re.compile(r"^LB[0-9A-HJKMNP-TV-Z]{10}$")
MESSAGE_ID_RE = re.compile(r"^LB[0-9A-HJKMNP-TV-Z]{10}-\d{2}$")


def encode_crockford(value: int, length: int) -> str:
    if value < 0 or value >= 32**length:
        raise ValueError("value out of range")
    chars = []
    for _ in range(length):
        chars.append(CROCKFORD_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def new_correlation_value(randbits: Callable[[int], int] = secrets.randbits) -> str:
    return CORRELATION_PREFIX + encode_crockford(randbits(50), 10)


def is_correlation_value(text: str) -> bool:
    return bool(CORRELATION_RE.match(text))


def message_id(correlation_value: str, attempt_no: int) -> str:
    """SKARVIK message id ``<correlation>-<attempt:02d>`` (15 characters)."""
    if not is_correlation_value(correlation_value):
        raise ValueError(f"invalid correlation value {correlation_value!r}")
    if not 1 <= attempt_no <= 99:
        raise ValueError("attempt_no must be within 1..99")
    return f"{correlation_value}-{attempt_no:02d}"
