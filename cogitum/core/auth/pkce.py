"""PKCE — verifier + S256 challenge. Both base64url-encoded, no padding."""

from __future__ import annotations

import base64
import hashlib
import secrets


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """Return (verifier, challenge). Verifier is the secret to keep, challenge is
    what gets sent in the authorize URL."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def random_state(n_bytes: int = 16) -> str:
    return secrets.token_hex(n_bytes)
