"""
Credential resolution.

Secrets in `providers.toml` are stored as references, never raw values
(the `plain:` scheme exists for dev convenience but logs a warning).

Schemes:
    plain:<value>                — literal, dev only
    env:<VAR>                    — read from environment
    keyring:<service>:<user>     — system keyring (libsecret / KWallet / macOS)
    vault:<id>                   — encrypted local vault (~/.config/cogitum/vault.enc)
    file:<path>                  — entire file contents, stripped

The vault is AES-GCM with a key derived from a master password via Argon2id
(falls back to scrypt if argon2-cffi is not installed). Master password is
cached in-process for the session so the user types it once.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import secrets
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vault paths
# ---------------------------------------------------------------------------

from ..platform_paths import get_config_dir

_DEFAULT_CONFIG_DIR: Final = get_config_dir()

_VAULT_PATH: Final = _DEFAULT_CONFIG_DIR / "vault.enc"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CredentialError(RuntimeError):
    """Failed to resolve a credential reference."""


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

@dataclass
class CredentialResolver:
    """Resolves `secret_ref` strings into actual secret values.

    The resolver is stateful: it caches the unlocked vault and the
    interactive master password for the lifetime of the process.
    """
    vault_path: Path = _VAULT_PATH
    interactive: bool = True              # ask for master password if needed
    _vault_cache: dict[str, str] | None = None
    _master_password: str | None = None

    # ---- public API ------------------------------------------------------

    def resolve(self, ref: str) -> str:
        """Resolve a reference string to its raw secret value."""
        if not ref:
            raise CredentialError("empty credential reference")

        scheme, _, rest = ref.partition(":")
        scheme = scheme.lower()

        if scheme == "plain":
            warnings.warn(
                "plain: credential reference detected — fine for dev, but"
                " move secrets into env/keyring/vault for production.",
                stacklevel=2,
            )
            return rest

        if scheme == "env":
            value = os.environ.get(rest)
            if value is None:
                raise CredentialError(f"environment variable {rest!r} is not set")
            return value

        if scheme == "file":
            path = Path(rest).expanduser()
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise CredentialError(f"file:{path} unreadable: {e}") from e

        if scheme == "keyring":
            return self._resolve_keyring(rest)

        if scheme == "vault":
            return self._resolve_vault(rest)

        if scheme == "oauth":
            return self._resolve_oauth(rest)

        raise CredentialError(f"unknown credential scheme: {scheme!r}")

    # ---- oauth backend --------------------------------------------------

    def _resolve_oauth(self, provider_id: str) -> str:
        """Resolve oauth:<provider_id> — read access token, refresh if expired."""
        from cogitum.core.auth import storage as auth_storage
        from cogitum.core.auth.registry import REGISTRY as OAUTH_REGISTRY

        creds = auth_storage.get(provider_id)
        if creds is None:
            raise CredentialError(
                f"oauth:{provider_id} — no tokens found. "
                f"Run `cog setup` → Subscriptions to authenticate."
            )

        if creds.expired():
            # Try to refresh
            oauth_provider = OAUTH_REGISTRY.get(provider_id)
            if oauth_provider is None:
                raise CredentialError(
                    f"oauth:{provider_id} — token expired and no refresh provider registered"
                )
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context — schedule refresh
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        new_creds = pool.submit(
                            asyncio.run, oauth_provider.refresh(creds)
                        ).result(timeout=30)
                else:
                    new_creds = asyncio.run(oauth_provider.refresh(creds))
                auth_storage.set_(provider_id, new_creds)
                creds = new_creds
                logger.info("oauth:%s token refreshed successfully", provider_id)
            except Exception as e:
                raise CredentialError(
                    f"oauth:{provider_id} — token expired and refresh failed: {e}"
                ) from e

        return creds.access

    # ---- keyring backend -------------------------------------------------

    def _resolve_keyring(self, rest: str) -> str:
        try:
            import keyring  # type: ignore[import-not-found]
        except ImportError as e:
            raise CredentialError(
                "keyring: scheme requested but `keyring` package not installed"
                " (pip install keyring)"
            ) from e

        service, _, user = rest.partition(":")
        if not service or not user:
            raise CredentialError(
                f"keyring ref must be 'keyring:<service>:<user>', got {rest!r}"
            )

        value = keyring.get_password(service, user)
        if value is None:
            raise CredentialError(
                f"keyring entry not found: service={service!r} user={user!r}"
            )
        return value

    # ---- vault backend ---------------------------------------------------

    def _resolve_vault(self, key: str) -> str:
        if self._vault_cache is None:
            self._vault_cache = self._unlock_vault()

        if key not in self._vault_cache:
            raise CredentialError(f"vault entry not found: {key!r}")
        return self._vault_cache[key]

    def _unlock_vault(self) -> dict[str, str]:
        if not self.vault_path.exists():
            raise CredentialError(
                f"vault file not found at {self.vault_path}; run"
                f" `cog vault init` to create one."
            )

        password = self._get_master_password()
        blob = self.vault_path.read_bytes()
        try:
            return _decrypt_vault(blob, password)
        except Exception as e:
            self._master_password = None  # force re-prompt next time
            raise CredentialError(f"vault decrypt failed: {e}") from e

    def _get_master_password(self) -> str:
        if self._master_password:
            return self._master_password

        env_pw = os.environ.get("COGITUM_VAULT_PASSWORD")
        if env_pw:
            self._master_password = env_pw
            return env_pw

        if not self.interactive:
            raise CredentialError(
                "vault locked and no COGITUM_VAULT_PASSWORD set in non-interactive mode"
            )

        pw = getpass.getpass("Cogitum vault password: ")
        if not pw:
            raise CredentialError("empty vault password")
        self._master_password = pw
        return pw

    # ---- vault management (CLI) -----------------------------------------

    def vault_init(self, password: str) -> None:
        """Create an empty vault. Refuses to overwrite an existing file."""
        if self.vault_path.exists():
            raise CredentialError(f"vault already exists at {self.vault_path}")
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        blob = _encrypt_vault({}, password)
        self.vault_path.write_bytes(blob)
        self.vault_path.chmod(0o600)
        self._master_password = password
        self._vault_cache = {}

    def vault_set(self, key: str, value: str) -> None:
        if self._vault_cache is None:
            self._vault_cache = self._unlock_vault()
        self._vault_cache[key] = value
        self._flush_vault()

    def vault_unset(self, key: str) -> None:
        if self._vault_cache is None:
            self._vault_cache = self._unlock_vault()
        self._vault_cache.pop(key, None)
        self._flush_vault()

    def vault_keys(self) -> list[str]:
        if self._vault_cache is None:
            self._vault_cache = self._unlock_vault()
        return sorted(self._vault_cache.keys())

    def _flush_vault(self) -> None:
        assert self._vault_cache is not None
        password = self._get_master_password()
        blob = _encrypt_vault(self._vault_cache, password)
        # Atomic write: tmp + rename.
        tmp = self.vault_path.with_suffix(".enc.tmp")
        tmp.write_bytes(blob)
        tmp.chmod(0o600)
        tmp.replace(self.vault_path)


# ---------------------------------------------------------------------------
# AES-GCM vault format
# ---------------------------------------------------------------------------
#
#   header:  b"COG1"
#   1B kdf:  0x01 = argon2id, 0x02 = scrypt
#   1B reserved (0)
#   16B salt
#   12B nonce
#   ciphertext (AES-256-GCM, JSON of {key: value} dict)
#   16B tag (appended by AESGCM)
# ---------------------------------------------------------------------------

_HEADER = b"COG1"


def _derive_key(password: str, salt: bytes, kdf_id: int) -> bytes:
    if kdf_id == 0x01:
        try:
            from argon2.low_level import Type, hash_secret_raw
        except ImportError as e:  # pragma: no cover
            raise CredentialError(
                "argon2-cffi not installed; vault was created with argon2id."
                " pip install argon2-cffi"
            ) from e
        return hash_secret_raw(
            password.encode("utf-8"),
            salt,
            time_cost=3,
            memory_cost=64 * 1024,
            parallelism=4,
            hash_len=32,
            type=Type.ID,
        )
    if kdf_id == 0x02:
        import hashlib
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, dklen=32
        )
    raise CredentialError(f"unknown kdf id: {kdf_id}")


def _pick_kdf() -> int:
    try:
        import argon2  # noqa: F401  type: ignore[import-not-found]
        return 0x01
    except ImportError:
        return 0x02


def _encrypt_vault(data: dict[str, str], password: str) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as e:  # pragma: no cover
        raise CredentialError(
            "cryptography not installed (pip install cryptography)"
        ) from e

    kdf_id = _pick_kdf()
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(password, salt, kdf_id)
    aes = AESGCM(key)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ciphertext = aes.encrypt(nonce, plaintext, _HEADER)
    return _HEADER + bytes([kdf_id, 0]) + salt + nonce + ciphertext


def _decrypt_vault(blob: bytes, password: str) -> dict[str, str]:
    if not blob.startswith(_HEADER):
        raise CredentialError("vault header mismatch (corrupt or wrong file)")
    if len(blob) < len(_HEADER) + 2 + 16 + 12 + 16:
        raise CredentialError("vault file truncated")
    pos = len(_HEADER)
    kdf_id = blob[pos]
    pos += 2  # skip reserved
    salt = blob[pos:pos + 16]; pos += 16
    nonce = blob[pos:pos + 12]; pos += 12
    ciphertext = blob[pos:]

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _derive_key(password, salt, kdf_id)
    aes = AESGCM(key)
    plaintext = aes.decrypt(nonce, ciphertext, _HEADER)
    return json.loads(plaintext.decode("utf-8"))


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_default_resolver: CredentialResolver | None = None


def default_resolver() -> CredentialResolver:
    global _default_resolver
    if _default_resolver is None:
        _default_resolver = CredentialResolver()
    return _default_resolver


def resolve(ref: str) -> str:
    """Convenience: use the process-wide default resolver."""
    return default_resolver().resolve(ref)


__all__ = [
    "CredentialError",
    "CredentialResolver",
    "default_resolver",
    "resolve",
]


# Silence unused-import lint when modules are imported lazily.
_ = base64
