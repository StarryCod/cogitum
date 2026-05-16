"""Persistent env-var store: ~/.config/cogitum/secrets.env

When the user picks the 'env' backend in the wizard, we write the
secret here (chmod 0600). At process start we load this file into
os.environ before resolving any secret_refs, so keys survive across
restarts without the user editing ~/.bashrc.

The wizard still tells the user 'env:VAR' is the canonical reference
in providers.toml — but resolution looks at process env, which is now
auto-populated from this file.

Format: shell-compatible KEY=VALUE pairs, one per line. Quotes around
the value are stripped. Lines starting with # are comments.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _config_dir() -> Path:
    return Path(
        os.environ.get("COGITUM_CONFIG_DIR")
        or os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    ) / "cogitum"


SECRETS_PATH = _config_dir() / "secrets.env"


_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.rstrip("\n")
    if not line or line.lstrip().startswith("#"):
        return None
    m = _LINE_RE.match(line)
    if not m:
        return None
    name, value = m.group(1), m.group(2)
    # Strip surrounding quotes and unescape shell-quoted single quotes
    if len(value) >= 2 and value[0] == value[-1] == "'":
        value = value[1:-1]
        # Reverse the '"'"' escape sequence used for embedded single quotes
        value = value.replace("'\"'\"'", "'")
    elif len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return name, value


def load_secrets_into_environ(*, override: bool = False) -> int:
    """Read secrets.env and populate os.environ.

    Args:
        override: if False (default), keep values that are already set
                  in the real environment (so /home/.bashrc wins). If
                  True, overwrite.

    Returns:
        Number of variables loaded.
    """
    path = _config_dir() / "secrets.env"
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_line(line)
            if parsed is None:
                continue
            name, value = parsed
            if not override and name in os.environ:
                continue
            os.environ[name] = value
            count += 1
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
    return count


def save_secret(name: str, value: str) -> None:
    """Persist a single VAR=value pair to secrets.env.

    Replaces any existing line for the same VAR. Creates the file with
    mode 0600 if missing. Updates os.environ in the current process.
    """
    path = _config_dir() / "secrets.env"
    path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing lines, keeping comments and order
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []

    # Replace or append
    new_line = f'{name}={_quote(value)}'
    replaced = False
    out: list[str] = []
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] == name:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append(new_line)

    body = "\n".join(out).rstrip() + "\n"
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)

    # Update current process so the next load_mesh sees it immediately
    os.environ[name] = value


def remove_secret(name: str) -> bool:
    """Delete a secret from secrets.env. Returns True if it was present."""
    path = _config_dir() / "secrets.env"
    if not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    out = []
    removed = False
    for line in lines:
        parsed = _parse_line(line)
        if parsed and parsed[0] == name:
            removed = True
            continue
        out.append(line)
    if removed:
        body = "\n".join(out).rstrip() + ("\n" if out else "")
        path.write_text(body, encoding="utf-8")
        os.environ.pop(name, None)
    return removed


def list_secrets() -> dict[str, str]:
    """Return all stored secrets as a dict (values masked for safety).

    Mask format: 'abcd…wxyz' (first 4 + ellipsis + last 4 if long enough).
    """
    path = _config_dir() / "secrets.env"
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, value = parsed
        if len(value) > 12:
            masked = f"{value[:4]}…{value[-4:]}"
        else:
            masked = "***"
        out[name] = masked
    return out


def _quote(value: str) -> str:
    """Quote a value for shell-compatible env file."""
    if not value:
        return '""'
    # If contains shell metacharacters, single-quote
    if any(c in value for c in " \t\n#'\"$\\"):
        # Replace single quotes with '"'"' in single-quoted strings
        escaped = value.replace("'", "'\"'\"'")
        return f"'{escaped}'"
    return value
