"""Passwords for email sign-in: scrypt hashes (stdlib, memory-hard) and generated passwords.

Ported from voitta-robot-war (robotwar/server/passwords.py).

Stored as  scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>  so the cost can be raised later without
breaking old hashes. Hashing takes ~0.1 s and 32 MB; at most a few run at once, so a burst of
guesses cannot exhaust the server.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading

N, R, P, DKLEN = 2**15, 8, 1, 32
MAXMEM = 64 * 1024 * 1024
MAX_LEN = 200
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i: easy to read out and type
_gate = threading.BoundedSemaphore(4)
_DUMMY_SALT = secrets.token_bytes(16)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _scrypt(pw: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> bytes:
    with _gate:
        return hashlib.scrypt(pw.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN, maxmem=MAXMEM)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(_scrypt(pw, salt, N, R, P))}"


def verify(pw: str, stored: str | None) -> bool:
    """True if pw matches. With no stored hash it still does the same work, so a missing
    account and a wrong password take the same time."""
    if not stored:
        _scrypt(pw or "x", _DUMMY_SALT)
        return False
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        got = _scrypt(pw, base64.b64decode(salt), int(n), int(r), int(p))
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def problem(pw: str) -> str | None:
    """Why this password is not acceptable, or None. No length or strength rules: whoever sets it decides."""
    if not pw:
        return "Password is empty."
    if len(pw) > MAX_LEN:
        return f"Password is longer than {MAX_LEN} characters."
    return None


def generate() -> str:
    """Four groups of five from a 31-letter alphabet: about 99 bits, readable over the phone."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(5)) for _ in range(4))
