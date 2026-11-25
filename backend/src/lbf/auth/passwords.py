"""Argon2id password hashing (argon2-cffi library defaults; ADR-F07)."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
_hasher = PasswordHasher()  # Argon2id with the library's default parameters

# A valid hash of a random value, used to equalize timing for unknown usernames.
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-credential")


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def parameters() -> dict[str, int]:
    return {
        "time_cost": _hasher.time_cost,
        "memory_cost_kib": _hasher.memory_cost,
        "parallelism": _hasher.parallelism,
        "hash_len": _hasher.hash_len,
        "salt_len": _hasher.salt_len,
    }
