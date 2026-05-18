"""
cogitum.core.mcp.security
~~~~~~~~~~~~~~~~~~~~~~~~~

Security helpers for the MCP client:

- ``filter_env``       — build a minimal env for stdio MCP subprocesses
                         (no API keys leak by default)
- ``resolve_secret``   — resolve ``vault:KEY`` and ``env:KEY`` references
                         from secrets.env / process env
- ``redact_secrets``   — strip credential-shaped substrings from error
                         messages before they hit the LLM
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

# ---------------------------------------------------------------------------
# Env filter
# ---------------------------------------------------------------------------

# Environment variables that are *always* safe to inherit.
_BASELINE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "TMPDIR", "PWD",
)


def filter_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """
    Build a sanitized environment for an MCP stdio subprocess.

    Inherits only PATH/HOME/USER/LANG/LC_*/TERM/SHELL/TMPDIR/XDG_* from the
    parent process. Anything else (API tokens, GH_TOKEN, ANTHROPIC_API_KEY,
    etc.) is dropped unless the caller explicitly passes it in ``extra``.

    Parameters
    ----------
    extra : mapping
        Server-specific env vars (already resolved via :func:`resolve_secret`
        if needed).
    """
    env: dict[str, str] = {}
    for k in _BASELINE_ENV:
        v = os.environ.get(k)
        if v is not None:
            env[k] = v
    for k, v in os.environ.items():
        if k.startswith("XDG_"):
            env[k] = v
    if extra:
        for k, v in extra.items():
            if v is not None:
                env[k] = str(v)
    return env


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


def _read_secrets_env() -> dict[str, str]:
    """Read secrets.env from the platform config dir into a dict (best-effort)."""
    from ..platform_paths import get_config_dir
    path = get_config_dir() / "secrets.env"
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_secret(value: str) -> str:
    """
    Resolve a config string that may be:

    - ``vault:KEY``  → look up ``KEY`` in ``~/.config/cogitum/secrets.env``
    - ``env:KEY``    → look up ``KEY`` in the process environment
    - anything else  → returned unchanged

    Missing keys raise :class:`KeyError` so the caller can surface a
    meaningful error rather than silently passing a literal "vault:foo"
    to a server.
    """
    if not isinstance(value, str):
        return value
    if value.startswith("vault:"):
        key = value[len("vault:"):]
        secrets = _read_secrets_env()
        if key not in secrets:
            raise KeyError(f"vault key {key!r} not found in secrets.env")
        return secrets[key]
    if value.startswith("env:"):
        key = value[len("env:"):]
        v = os.environ.get(key)
        if v is None:
            raise KeyError(f"env var {key!r} not set")
        return v
    return value


def resolve_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    """Resolve every value in a mapping via :func:`resolve_secret`."""
    out: dict[str, str] = {}
    for k, v in mapping.items():
        out[k] = resolve_secret(v)
    return out


# ---------------------------------------------------------------------------
# Secret redaction in error strings
# ---------------------------------------------------------------------------

_REDACT_PATTERNS = [
    # GitHub fine-grained / classic PATs
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "ghp_***"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github_pat_***"),
    # OpenAI-shaped keys
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b"), "sk-***"),
    # Anthropic
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"), "sk-ant-***"),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"), "Bearer ***"),
    # AWS
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    # generic key/token/password=value
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd)\s*[=:]\s*"
            r"[\"']?([A-Za-z0-9._\-]{6,})[\"']?"
        ),
        r"\1=***",
    ),
]


def redact_secrets(text: str) -> str:
    """Replace credential-shaped substrings with redacted placeholders."""
    if not isinstance(text, str):
        return text
    out = text
    for pat, repl in _REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out
