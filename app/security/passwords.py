import re
import unicodedata

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from app.security.errors import SecurityError

# Argon2id defaults: memory=64MiB, iterations=3, parallelism=4.
_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
_dummy = _hasher.hash("dummy-password-123456")


def normalize_username(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).strip().casefold()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", value):
        raise SecurityError("invalid_username", 422)
    return value


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128 or not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise SecurityError("password_requires_12_to_128_chars_letters_and_numbers", 422)
    return _hasher.hash(password)


def dummy_hash() -> str:
    return _dummy


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False
