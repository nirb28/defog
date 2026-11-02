"""Password hashing, opaque tokens, token encryption and the breached-password check."""

import hashlib
import hmac
import json
import logging
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from . import http

log = logging.getLogger(__name__)

_hasher = PasswordHasher()  # Argon2id with the library's recommended parameters
# Verified against when an email has no account, so a login takes the same time either way.
_DUMMY_HASH = _hasher.hash("defog-timing-equaliser")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if not password_hash:
        _safe_verify(_DUMMY_HASH, password)  # spend the same time as a real check
        return False
    return _safe_verify(password_hash, password)


def _safe_verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return False


def new_token() -> str:
    """A URL-safe random token with 256 bits of entropy."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are high-entropy, so a plain SHA-256 is enough (no salt or slow hash needed)."""
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


# --- Encryption of third-party tokens at rest -------------------------------------------


class TokenCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, data: dict) -> str:
        return self._fernet.encrypt(json.dumps(data).encode()).decode()

    def decrypt(self, blob: str) -> dict:
        try:
            return json.loads(self._fernet.decrypt(blob.encode()))
        except InvalidToken as exc:
            raise ValueError(
                "Stored tokens can't be decrypted (was TOKEN_ENCRYPTION_KEY changed?)"
            ) from exc


def get_cipher() -> TokenCipher:
    from .config import get_settings

    return TokenCipher(get_settings().effective_token_key)


# --- Breached passwords (Have I Been Pwned, k-anonymity range API) ----------------------

PWNED_RANGE_URL = "https://api.pwnedpasswords.com/range/"


def is_breached_password(password: str) -> bool:
    """True if the password appears in a known breach.

    Only the first 5 characters of the password's SHA-1 hash leave the server. If the
    service can't be reached, signup isn't blocked (the check is logged and skipped).
    """
    digest = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 - required by the API
    prefix, suffix = digest[:5], digest[5:]
    try:
        with http.client(timeout=5) as client:
            res = client.get(PWNED_RANGE_URL + prefix, headers={"Add-Padding": "true"})
            res.raise_for_status()
    except Exception as exc:  # network trouble must not block signups
        log.warning("Breached-password check skipped: %s", exc)
        return False
    for line in res.text.splitlines():
        candidate, _, count = line.partition(":")
        if candidate.strip() == suffix and count.strip() not in ("", "0"):
            return True
    return False
