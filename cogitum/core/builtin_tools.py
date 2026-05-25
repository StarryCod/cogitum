"""
cogitum.core.builtin_tools
~~~~~~~~~~~~~~~~~~~~~~~~~~
Built-in tools registered into the global REGISTRY.
Import this module once at startup to activate them.
"""
from __future__ import annotations

import asyncio
import contextvars
import ipaddress
import logging
import os
import re
import shlex
import socket
import subprocess
import sys as _sys
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urljoin

from cogitum.core.tools import tool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (used across fetch_url / browser / classifier).
# ---------------------------------------------------------------------------

# fetch_url limits — kept tight to bound LLM resource use and to make
# decompression-bomb DoS attacks ineffective.
_FETCH_MAX_BYTES = 5 * 1024 * 1024        # 5 MB hard cap on decoded body
_FETCH_MAX_REDIRECTS = 5                  # SSRF/loop quota
_FETCH_TIMEOUT_S = 20.0                   # per-request timeout (seconds)
_FETCH_CHUNK_SIZE = 64 * 1024             # streaming chunk size for cap check

# Browser interaction caps — surface large pages without nuking the LLM
# context. Kept as kwargs in browser() but documented here for visibility.
# Browser timeouts (ms): 30000 open, 10000 click/type/extract, 15000 reload,
# 5000 about:blank reset. Search call sites for the exact use.
_BROWSER_TEXT_CAP_CHARS = 8000
_BROWSER_LINKS_CAP = 200

# Process-wide singleton browser used by ``browse``. We share the
# Playwright instance + browser + (single) page across calls so an LLM
# can chain ``open`` → ``click`` → ``text`` without re-spawning Chromium
# every step. The flip side: two concurrent ``browse(...)`` calls would
# stomp on each other (one navigating while the other is reading text
# off the same Page). The lock below serialises the open/close sections
# so each call sees a consistent state. Tests can monkeypatch this dict
# directly.
_BROWSER_STATE: dict = {"browser": None, "page": None, "context": None, "_pw": None}
_BROWSER_LOCK = asyncio.Lock()

# Tempfile bookkeeping for the browse() screenshot helper. We use
# tempfile.mkstemp (atomic create, unique name, no race) instead of
# the deprecated mktemp(), then track every path in this set so a
# periodic / shutdown sweep can unlink them. Without the set the
# files leak across runs and pile up in /tmp until the OS reaps
# them — minutes to hours depending on the platform.
#
# TTL: cleanup_browser_tempfiles() unlinks any path older than
# _BROWSER_TEMPFILE_TTL_S seconds. Default 30 min covers the longest
# realistic agent task while still bounding disk use. Callers can
# also call cleanup_browser_tempfiles(max_age_s=0) at task end to
# wipe everything we created during that task.
_BROWSER_TEMPFILES: dict[str, float] = {}
_BROWSER_TEMPFILE_TTL_S: float = 1800.0


def _track_browser_tempfile(path: str) -> None:
    """Register a tempfile created by browse() for later cleanup."""
    import time as _t
    _BROWSER_TEMPFILES[path] = _t.monotonic()


def cleanup_browser_tempfiles(max_age_s: float | None = None) -> int:
    """Unlink tracked browser tempfiles older than ``max_age_s`` seconds.

    Returns the number of files removed. Best-effort: missing files
    and unlink errors are swallowed (the file may already be gone if
    the agent moved/consumed it). Pass ``max_age_s=0`` to wipe every
    tracked path regardless of age — used at agent-task teardown.
    """
    import os as _os
    import time as _t
    if max_age_s is None:
        max_age_s = _BROWSER_TEMPFILE_TTL_S
    now = _t.monotonic()
    removed = 0
    for path in list(_BROWSER_TEMPFILES):
        created = _BROWSER_TEMPFILES.get(path, now)
        if now - created < max_age_s:
            continue
        try:
            _os.unlink(path)
            removed += 1
        except OSError:
            pass
        _BROWSER_TEMPFILES.pop(path, None)
    return removed

# CGNAT (RFC 6598) — Python's ipaddress.is_private does NOT cover this.
# Many cloud providers (T-Mobile, Comcast, AWS NAT-instance VPC) route
# 100.64.0.0/10 to internal services, so leaving it unblocked exposes
# the agent to internal SSRF on those networks.
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

# ---------------------------------------------------------------------------
# Security: path sandbox
# ---------------------------------------------------------------------------

# Sensitive paths that should NEVER be read/written by the LLM.
# Per-platform — POSIX paths on Linux/macOS, registry hives + auth
# stores on Windows. Path comparisons are case-insensitive on Windows
# inside _is_sensitive() below.

_SENSITIVE_PATHS_POSIX = {
    "/etc/shadow", "/etc/passwd", "/etc/sudoers",
}

_SENSITIVE_PATHS_HOME_RELATIVE = {
    # Match against $HOME-relative path on any platform.
    ".ssh/authorized_keys", ".ssh/id_rsa", ".ssh/id_ed25519",
    ".gnupg", ".aws/credentials", ".config/gcloud",
    # Cogitum's own auth/provider stores — explicitly off-limits to the
    # LLM (send_media, read_file, etc.) so a malicious or naive plan
    # can't exfiltrate OAuth tokens or API keys via path traversal.
    ".config/cogitum/auth.json", ".config/cogitum/providers.toml",
    ".netrc",
}

_SENSITIVE_PATHS_WINDOWS = {
    # Auth stores Windows ships with. Forward-slash form for
    # comparison; we normalise input below.
    "windows/system32/config/sam",
    "windows/system32/config/security",
    "windows/system32/config/system",
    "windows/system32/lsass.exe",
}

_SENSITIVE_PREFIXES_POSIX = (
    "/proc/", "/sys/", "/dev/",
)

_SENSITIVE_PREFIXES_WINDOWS = (
    # Roots that should never be modified by the LLM.
    "windows/system32/",
    "windows/syswow64/",
    "$recycle.bin/",
    "system volume information/",
)

# Build the active sets at import time based on platform.
if _sys.platform == "win32":
    _SENSITIVE_PATHS = _SENSITIVE_PATHS_WINDOWS | _SENSITIVE_PATHS_HOME_RELATIVE
    _SENSITIVE_PREFIXES = _SENSITIVE_PREFIXES_WINDOWS
else:
    _SENSITIVE_PATHS = _SENSITIVE_PATHS_POSIX | _SENSITIVE_PATHS_HOME_RELATIVE
    _SENSITIVE_PREFIXES = _SENSITIVE_PREFIXES_POSIX


# Shells whose stdin should be treated as command execution.
_INTERACTIVE_SHELLS = ("bash", "sh", "zsh", "fish", "dash", "ksh")


def _pid_is_interactive_shell(pid: int) -> bool:
    """Best-effort check: is the running process at ``pid`` a shell?

    Used by ``classify_danger`` so that ``terminal(mode='background',
    command='write', pid=N, stdin_data='rm -rf /')`` is treated as
    command execution against the shell, not opaque data writing.

    We consult the in-process ``ProcessManager`` first (no syscalls), and
    fall back to ``/proc/<pid>/comm`` on Linux. Returns False on any
    error — the classifier still has the stdin_data deep-analysis path,
    so a False negative here doesn't open a hole, it just relaxes the
    floor from medium back to low for non-shell PIDs.
    """
    if not pid:
        return False
    try:
        from cogitum.core.process_manager import ProcessManager
        bp = ProcessManager.get().get_process(int(pid))
        if bp is not None:
            # Look at the spawn command — if it starts with bash/sh/zsh,
            # this is a shell.
            tokens = _tokenize(bp.command)
            if tokens:
                base = os.path.basename(tokens[0]).lower()
                if base in _INTERACTIVE_SHELLS:
                    return True
                # Skip env wrappers ``env FOO=bar bash``.
                if base in ("env", "sudo"):
                    for t in tokens[1:]:
                        if "=" in t and not t.startswith("/"):
                            continue
                        if os.path.basename(t).lower() in _INTERACTIVE_SHELLS:
                            return True
                        break
    except (OSError, ValueError):
        # F37 cluster: /proc parsing best-effort. Narrowed from
        # ``except Exception`` so logic bugs surface instead of being
        # masked. Anything else (TypeError, AttributeError) is a real
        # bug we want to see.
        log.debug("interactive-shell detection (cmdline) failed", exc_info=True)
    # Fallback: /proc/<pid>/comm on Linux.
    try:
        comm_path = Path("/proc") / str(int(pid)) / "comm"
        if comm_path.exists():
            comm = comm_path.read_text().strip().lower()
            if comm in _INTERACTIVE_SHELLS:
                return True
    except (OSError, ValueError):
        log.debug("interactive-shell detection (comm) failed", exc_info=True)
    return False


def _split_into_subcommands(cmd: str) -> list[str]:
    """Break a shell line into its sub-commands (best effort).

    Splits on top-level ``;``, ``|``, ``&&``, ``||``, newline. Also expands
    ``$(...)`` and backtick subshells into their own sub-commands so we
    can reason about the verb inside them.

    Quote-aware: separators inside ``'...'`` or ``"..."`` are NOT treated
    as command separators (so ``python -c "import shutil; shutil.rmtree('/')"``
    stays as one sub-command and the python-c check sees the full payload).

    This is a textual splitter — we don't try to be a proper shell parser.
    Good enough for danger classification; defence-in-depth.
    """
    pieces: list[str] = []

    # Pull out $(...) and `...` subshells first, recursively.
    # Skip over single/double-quoted regions so we don't latch onto a
    # backtick that lives inside a string literal.
    def _extract_subshells(s: str) -> tuple[str, list[str]]:
        subs: list[str] = []
        out = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            # Skip single-quoted region verbatim.
            if ch == "'":
                j = i + 1
                while j < n and s[j] != "'":
                    j += 1
                out.append(s[i : min(j + 1, n)])
                i = j + 1
                continue
            # Skip double-quoted region but still expand $() inside.
            # Bash actually does interpolate $() inside "...", so we keep
            # subshell extraction live but avoid splitting on ; or |.
            if ch == '"':
                out.append(ch)
                i += 1
                while i < n and s[i] != '"':
                    if s[i] == "\\" and i + 1 < n:
                        out.append(s[i])
                        out.append(s[i + 1])
                        i += 2
                        continue
                    if s[i] == "$" and i + 1 < n and s[i + 1] == "(":
                        depth = 1
                        j = i + 2
                        while j < n and depth > 0:
                            if s[j] == "(":
                                depth += 1
                            elif s[j] == ")":
                                depth -= 1
                            j += 1
                        inner = s[i + 2 : j - 1] if depth == 0 else s[i + 2 : j]
                        subs.append(inner)
                        i = j
                        out.append(" ")
                        continue
                    out.append(s[i])
                    i += 1
                if i < n:
                    out.append(s[i])
                    i += 1
                continue
            # $(...)
            if ch == "$" and i + 1 < n and s[i + 1] == "(":
                depth = 1
                j = i + 2
                while j < n and depth > 0:
                    if s[j] == "(":
                        depth += 1
                    elif s[j] == ")":
                        depth -= 1
                    j += 1
                inner = s[i + 2 : j - 1] if depth == 0 else s[i + 2 : j]
                subs.append(inner)
                i = j
                out.append(" ")
                continue
            # `...`
            if ch == "`":
                j = i + 1
                while j < n and s[j] != "`":
                    j += 1
                inner = s[i + 1 : j]
                subs.append(inner)
                i = j + 1
                out.append(" ")
                continue
            out.append(ch)
            i += 1
        return "".join(out), subs

    cleaned, subs = _extract_subshells(cmd)
    # Recurse for nested subshells.
    expanded_subs: list[str] = []
    for s in subs:
        c2, more = _extract_subshells(s)
        expanded_subs.append(c2)
        expanded_subs.extend(more)

    # Quote-aware split on top-level ;, |, &&, ||, newline. Anything
    # inside single or double quotes is preserved verbatim.
    def _split_quote_aware(s: str) -> list[str]:
        parts: list[str] = []
        buf: list[str] = []
        i = 0
        n = len(s)
        while i < n:
            ch = s[i]
            if ch == "'":
                buf.append(ch)
                i += 1
                while i < n and s[i] != "'":
                    buf.append(s[i])
                    i += 1
                if i < n:
                    buf.append(s[i])
                    i += 1
                continue
            if ch == '"':
                buf.append(ch)
                i += 1
                while i < n and s[i] != '"':
                    if s[i] == "\\" and i + 1 < n:
                        buf.append(s[i])
                        buf.append(s[i + 1])
                        i += 2
                        continue
                    buf.append(s[i])
                    i += 1
                if i < n:
                    buf.append(s[i])
                    i += 1
                continue
            # Two-char separators first.
            if ch == "&" and i + 1 < n and s[i + 1] == "&":
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch == "|" and i + 1 < n and s[i + 1] == "|":
                parts.append("".join(buf))
                buf = []
                i += 2
                continue
            if ch in ";|\n":
                parts.append("".join(buf))
                buf = []
                i += 1
                continue
            buf.append(ch)
            i += 1
        if buf:
            parts.append("".join(buf))
        return parts

    parts = _split_quote_aware(cleaned)
    pieces.extend(p.strip() for p in parts if p.strip())
    pieces.extend(p.strip() for p in expanded_subs if p.strip())
    return pieces


def _redirect_targets_disk(cmd: str) -> bool:
    """Detect ``> /dev/sd*``, ``>> /dev/nvme*``, and the ``>|`` clobber form.

    Writing to a raw block device wipes the partition table. Bash's
    ``>|`` (force-clobber even with ``noclobber``) is functionally
    identical so we cover it here.
    """
    return bool(re.search(
        r"(>>?|>\|)\s*/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z]|mmcblk\d+|vd[a-z])",
        cmd,
    ))


# Persistence-vector paths. Writing/appending to ANY of these lets a
# follow-up shell session run with elevated privilege or attacker-
# controlled config — classic LLM-rukozhopstvo persistence.
_PERSISTENCE_PATTERNS = (
    # cron
    r"/etc/cron\.d/[^\s>;&|]+",
    r"/etc/cron\.(hourly|daily|weekly|monthly)/[^\s>;&|]+",
    r"/var/spool/cron/[^\s>;&|]+",
    # sudoers
    r"/etc/sudoers(\.d/[^\s>;&|]+)?",
    # shell rc files (HOME-relative). We pre-expand ~ so both ~/.bashrc
    # and the literal expanded form match. Match plain ~/.X and $HOME/.X.
    r"(~|\$HOME)/\.bashrc",
    r"(~|\$HOME)/\.zshrc",
    r"(~|\$HOME)/\.profile",
    r"(~|\$HOME)/\.bash_profile",
    r"(~|\$HOME)/\.zprofile",
    r"(~|\$HOME)/\.zlogin",
    # SSH
    r"(~|\$HOME)/\.ssh/authorized_keys",
    r"(~|\$HOME)/\.ssh/config",
    # passwd/shadow
    r"/etc/passwd",
    r"/etc/shadow",
    # systemd unit files (system + user)
    r"/etc/systemd/system/[^\s>;&|]+",
    r"/etc/init\.d/[^\s>;&|]+",
    r"(~|\$HOME)/\.config/systemd/user/[^\s>;&|]+",
)

# Pre-compile the redirect regex: "> path" or ">> path" or ">| path".
_PERSISTENCE_RE = re.compile(
    r"(?:>>?|>\|)\s*(" + "|".join(_PERSISTENCE_PATTERNS) + r")"
)

# Also catch the same paths when given to ``tee`` (with or without -a).
_TEE_PERSISTENCE_RE = re.compile(
    r"\btee\b(?:\s+-[aA-Za-z]*)*\s+(" + "|".join(_PERSISTENCE_PATTERNS) + r")"
)


def _redirect_targets_persistence(cmd: str) -> bool:
    """Detect writes/appends to known persistence paths.

    Catches ``echo evil >> /etc/cron.d/x``, ``>> ~/.ssh/authorized_keys``,
    ``>> /etc/sudoers``, ``echo bad | tee /etc/cron.d/x``, etc.

    Both literal ``~`` and the ``$HOME`` form are matched without
    requiring shell expansion: we treat the textual occurrence as the
    user's intent.
    """
    if _PERSISTENCE_RE.search(cmd):
        return True
    if _TEE_PERSISTENCE_RE.search(cmd):
        return True
    return False


def _has_fork_bomb(cmd: str) -> bool:
    """Classic ``:(){ :|:& };:`` plus longer-named and reordered variants.

    A fork bomb is a function that pipes or backgrounds itself into
    itself — the textbook signature is::

        <name>() { <name> | <name> & }; <name>

    We accept multi-character identifiers (``bomb``, ``f``, ``:`` —
    bash allows ``:`` as a function name in the canonical form), and
    we don't enforce a strict ``|`` vs ``&`` order: ``f|f &``,
    ``f & f &``, ``f|f|f &`` all count.
    """
    # Strip whitespace so ``: () { :|: & };:`` matches the same.
    compact = re.sub(r"\s+", "", cmd)
    # Function name char class includes ``:`` for the classic form;
    # otherwise an identifier.
    name = r"([A-Za-z_][A-Za-z0-9_]*|:)"
    # Body: contains the same name twice, separated by | or & (any
    # combination), optionally with another | or &. Keep the regex
    # liberal — false positives on exotic names are acceptable for a
    # security floor.
    body = (
        r"\{[^}]*\1[^}]*[|&][^}]*\1[^}]*\}"
    )
    pattern = name + r"\(\)" + body + r";\1"
    return bool(re.search(pattern, compact))


# Stdlib calls / subshell forms that make a Python -c payload destructive.
# Used for python/python3/python2/py.
_PY_DASH_C_BAD = (
    "shutil.rmtree",
    "os.unlink", "os.remove", "os.removedirs", "os.rmdir",
    "os.system(",
    "subprocess.run(", "subprocess.call(", "subprocess.Popen(",
    "subprocess.check_call(", "subprocess.check_output(",
    # __import__('os')/__import__('shutil') style obfuscation.
    "__import__('os')", '__import__("os")',
    "__import__('shutil')", '__import__("shutil")',
    "__import__('subprocess')", '__import__("subprocess")',
    # exec/eval over an unknown payload — almost always sketchy in -c.
    "exec(",
)

# Equivalent for node -e. fs.rm*/rmdir* + child_process.exec/spawn.
_NODE_DASH_E_BAD = (
    "rmSync", "rm(", "unlinkSync", "unlink(", "rmdirSync", "rmdir(",
    "child_process", "execSync(", "exec(", "spawnSync(", "spawn(",
)

# perl/ruby one-liners.
_PERL_DASH_E_BAD = (
    "rmtree", "unlink", "File::Path", "system(", "exec ", "exec(",
    "qx{", "qx(", "`",
)
_RUBY_DASH_E_BAD = (
    "FileUtils.rm", "File.delete", "File.unlink", "Dir.rmdir",
    "system(", "exec(", "%x{", "`",
)


def _payload_matches(payload: str, needles: tuple[str, ...]) -> bool:
    return any(n in payload for n in needles)


def _python_dash_c_is_dangerous(tokens: list[str]) -> bool:
    """``python -c "import shutil; shutil.rmtree('/')"`` and friends.

    Looks for python(3?) followed by -c and a payload that names a
    destructive stdlib API. Defence-in-depth — easy to obfuscate
    (base64 + exec), but catches the obvious case.
    """
    if not tokens:
        return False
    first = os.path.basename(tokens[0]).lower()
    if first not in ("python", "python3", "python2", "py"):
        return False
    try:
        idx = tokens.index("-c")
    except ValueError:
        return False
    if idx + 1 >= len(tokens):
        return False
    return _payload_matches(tokens[idx + 1], _PY_DASH_C_BAD)


def _node_dash_e_is_dangerous(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if os.path.basename(tokens[0]).lower() != "node":
        return False
    for flag in ("-e", "--eval", "-p", "--print"):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens) and _payload_matches(
                tokens[idx + 1], _NODE_DASH_E_BAD
            ):
                return True
    return False


def _perl_dash_e_is_dangerous(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if os.path.basename(tokens[0]).lower() != "perl":
        return False
    for flag in ("-e", "-E"):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens) and _payload_matches(
                tokens[idx + 1], _PERL_DASH_E_BAD
            ):
                return True
    return False


def _ruby_dash_e_is_dangerous(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if os.path.basename(tokens[0]).lower() != "ruby":
        return False
    for flag in ("-e",):
        if flag in tokens:
            idx = tokens.index(flag)
            if idx + 1 < len(tokens) and _payload_matches(
                tokens[idx + 1], _RUBY_DASH_E_BAD
            ):
                return True
    return False


def _chmod_is_dangerous(tokens: list[str]) -> bool:
    """``chmod -R 000 /`` and ``chmod -R 777 /`` style nukes."""
    if not tokens or os.path.basename(tokens[0]).lower() != "chmod":
        return False
    has_recursive = any(t in ("-R", "--recursive") or
                        (t.startswith("-") and "R" in t)
                        for t in tokens[1:])
    if not has_recursive:
        return False
    # Mode token + path token. Look for unsafe paths.
    for t in tokens[1:]:
        if t in ("/", "/*", "/etc", "/usr", "/bin", "/sbin", "/lib",
                 "/lib64", "/var", "/boot", "/root", "/home"):
            return True
    # Also flag explicit 000 mode (locks everything out).
    if any(t == "000" for t in tokens[1:]):
        return True
    return False


def _dd_is_dangerous(tokens: list[str]) -> bool:
    """``dd if=/dev/zero of=/dev/sda`` — disk wipe.

    Also covers ``ddrescue`` which has the same destructive shape.
    """
    if not tokens:
        return False
    base = os.path.basename(tokens[0]).lower()
    if base not in ("dd", "ddrescue"):
        return False
    of_target = ""
    if_source = ""
    positional: list[str] = []
    for t in tokens[1:]:
        if t.startswith("of="):
            of_target = t[3:]
        elif t.startswith("if="):
            if_source = t[3:]
        elif not t.startswith("-"):
            positional.append(t)
    # ddrescue uses positional args: ddrescue SRC DST. The DST (2nd
    # positional) is the destructive one.
    if base == "ddrescue" and len(positional) >= 2:
        of_target = of_target or positional[1]
        if_source = if_source or positional[0]
    if re.match(r"^/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z]|mmcblk\d+|vd[a-z])",
                of_target):
        return True
    # if=/dev/zero with any of= is suspicious.
    if if_source in ("/dev/zero", "/dev/random", "/dev/urandom") and of_target:
        return True
    return False


def _mkfs_is_dangerous(tokens: list[str]) -> bool:
    """``mkfs.ext4 /dev/sda`` and the broader filesystem-format family."""
    if not tokens:
        return False
    base = os.path.basename(tokens[0]).lower()
    if base.startswith("mkfs.") or base in ("mkfs", "mke2fs"):
        return True
    return False


def _disk_wipe_tool_is_dangerous(tokens: list[str]) -> bool:
    """Catch disk-destructive utilities outside the dd/mkfs family.

    * ``wipefs`` — clears filesystem signatures
    * ``blkdiscard`` — issues TRIM/discard, eraser
    * ``shred`` against /dev/<disk>
    * ``parted`` / ``fdisk`` / ``gdisk`` / ``sgdisk`` — partition-table
      editors. We flag any invocation against /dev/<disk>; leaving them
      unflagged is exactly the ``parted /dev/sda mklabel gpt`` foot-gun.
    * ``tee /dev/sd*`` — sneaky way to write to a raw block device
      without using shell redirection.
    """
    if not tokens:
        return False
    base = os.path.basename(tokens[0]).lower()
    if base in ("wipefs", "blkdiscard"):
        return True
    if base == "shred":
        for t in tokens[1:]:
            if re.match(
                r"^/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z]|mmcblk\d+|vd[a-z])",
                t,
            ):
                return True
    if base in ("parted", "fdisk", "gdisk", "sgdisk", "cfdisk", "sfdisk"):
        # Any /dev/<disk> argument → destructive.
        for t in tokens[1:]:
            if re.match(
                r"^/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z]|mmcblk\d+|vd[a-z])",
                t,
            ):
                return True
        # Some of these (fdisk, gdisk) can ALSO be used non-interactively
        # in ways that don't touch a disk (e.g. fdisk -l). Be conservative:
        # only flag when a /dev/<disk> argument is present.
    if base == "tee":
        for t in tokens[1:]:
            if re.match(
                r"^/dev/(sd[a-z]|nvme\d+n\d+|disk\d+|hd[a-z]|mmcblk\d+|vd[a-z])",
                t,
            ):
                return True
    return False


def _rm_is_dangerous(tokens: list[str]) -> bool:
    """``rm -rf <anything>`` is destructive enough to flag.

    We don't try to whitelist safe paths — the user's approval prompt
    will show the full command. False positives here mean an extra
    confirmation; false negatives mean data loss.
    """
    if not tokens:
        return False
    base = os.path.basename(tokens[0]).lower()
    if base not in ("rm", "rmdir"):
        return False
    # Any flag combining r and f (or --recursive / --force) is dangerous.
    for t in tokens[1:]:
        if t in ("-r", "-R", "-rf", "-fr", "-Rf", "-fR",
                 "--recursive", "--force"):
            return True
        if t.startswith("-") and not t.startswith("--") and (
            "r" in t or "R" in t
        ):
            return True
    # Plain ``rm /tmp/foo.txt`` with no flags isn't auto-flagged. The
    # legacy substring check still catches ``rm `` for cogit auto-save,
    # but we intentionally don't escalate it to ``danger`` here.
    return False


def _xargs_or_find_delete(tokens: list[str]) -> bool:
    """``echo … | xargs rm`` or ``find … -delete`` style mass delete."""
    if not tokens:
        return False
    base = os.path.basename(tokens[0]).lower()
    if base == "xargs":
        # xargs rm / xargs -I{} rm …
        return any(os.path.basename(t).lower() in ("rm", "rmdir")
                   for t in tokens[1:])
    if base == "find":
        if "-delete" in tokens:
            return True
        # find … -exec rm …
        if "-exec" in tokens:
            try:
                idx = tokens.index("-exec")
                if idx + 1 < len(tokens) and \
                   os.path.basename(tokens[idx + 1]).lower() in ("rm", "rmdir"):
                    return True
            except ValueError:
                pass
    return False


def _tokenize(sub: str) -> list[str]:
    """``shlex.split`` with a fallback for unbalanced quotes."""
    try:
        return shlex.split(sub, comments=False, posix=True)
    except ValueError:
        # Unbalanced quote — fall back to whitespace split. Better to
        # over-approximate than raise.
        return sub.split()


# Wrapper commands that consume one or more flags / args and then run
# the real command. We strip these BEFORE running the verb-specific
# danger checks so that ``sudo -u root rm -rf /``, ``nohup rm -rf /``,
# ``timeout 60 rm -rf /``, and friends classify the same as the
# unwrapped form.
#
# Each entry maps the wrapper name to a small "skipper" config:
#   - flags_with_value: option chars that consume the next token as
#     a value (e.g. ``sudo -u root`` → consume ``-u`` AND ``root``)
#   - takes_one_positional: True if the wrapper consumes exactly ONE
#     positional argument before the wrapped command (``timeout <dur>
#     <cmd>``, ``nice -n 10 <cmd>``).
_WRAPPERS: dict[str, dict] = {
    "sudo":     {"flags_with_value": {"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-T"}},
    "doas":     {"flags_with_value": {"-u", "-C"}},
    "nohup":    {},
    "nice":     {"flags_with_value": {"-n", "--adjustment"}},
    "ionice":   {"flags_with_value": {"-c", "-n", "-p", "-P", "-u", "--class"}},
    "chrt":     {"flags_with_value": {"-p"}},
    "taskset":  {},  # mask is positional but always single token
    "stdbuf":   {"flags_with_value": {"-i", "-o", "-e"}},
    "setsid":   {},
    "timeout":  {"takes_one_positional": True},  # timeout DURATION CMD
    "command":  {},
    "exec":     {},
    "builtin":  {},
    "env":      {},   # env [-i] [VAR=val ...] CMD — handled specially
    "script":   {"flags_with_value": {"-c", "-O", "-T", "-t"}},
    # NOTE: ``xargs`` is intentionally NOT a wrapper here. Stripping it
    # would leave us looking at the trailing token (e.g. ``rm`` without
    # its flags), which the rm classifier wouldn't flag. Instead,
    # ``_xargs_or_find_delete`` checks ``xargs <cmd>`` directly.
}

# Shell interpreters whose ``-c`` argument is a fresh shell payload that
# must be re-classified as a separate command.
_SHELL_INTERPRETERS = {"bash", "sh", "zsh", "fish", "dash", "ksh", "ash"}


def _strip_wrappers(head: list[str], depth: int = 0) -> list[str]:
    """Walk through wrapper commands until we reach the real verb.

    Examples::

        ['sudo', '-u', 'root', 'rm', '-rf', '/']  → ['rm', '-rf', '/']
        ['nohup', 'rm', '-rf', '/']               → ['rm', '-rf', '/']
        ['timeout', '60', 'rm', '-rf', '/']       → ['rm', '-rf', '/']
        ['nice', '-n', '10', 'rm', '-rf', '/']    → ['rm', '-rf', '/']
        ['env', 'FOO=bar', 'rm', '-rf', '/']      → ['rm', '-rf', '/']
        ['sudo', 'bash', '-c', 'rm -rf /']        → ['bash', '-c', 'rm -rf /']

    For ``bash -c <PAYLOAD>`` / ``sh -c <PAYLOAD>`` we DON'T unwrap
    here — that path goes through ``_is_dangerous_command`` recursion
    on the payload string instead, since the payload is its own shell
    grammar (multiple sub-commands, redirects, etc).
    """
    if depth > 6:
        # Defensive: prevent pathological infinite-wrap recursion.
        return head
    if not head:
        return head
    base = os.path.basename(head[0]).lower()
    if base not in _WRAPPERS:
        return head
    cfg = _WRAPPERS[base]
    i = 1

    # ``env`` consumes [-i] then any number of VAR=value pairs, then CMD.
    if base == "env":
        while i < len(head):
            t = head[i]
            if t == "-i" or t == "--ignore-environment":
                i += 1
                continue
            if t.startswith("-u") or t == "--unset":
                # -uVAR or -u VAR
                if t == "-u" or t == "--unset":
                    i += 2
                else:
                    i += 1
                continue
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
                i += 1
                continue
            break
        return _strip_wrappers(head[i:], depth + 1)

    # Generic wrapper: skip option flags + their values, then optionally
    # one positional (timeout DURATION).
    flags_with_value = cfg.get("flags_with_value", set())
    takes_one_positional = cfg.get("takes_one_positional", False)
    while i < len(head) and head[i].startswith("-"):
        tok = head[i]
        # Long opt with embedded value (--foo=bar) consumes one token.
        if tok.startswith("--") and "=" in tok:
            i += 1
            continue
        # Short opt that takes a value as the next token (e.g. sudo -u root).
        if tok in flags_with_value:
            i += 2
            continue
        # Long opt that takes a value as the next token.
        if tok.startswith("--") and tok in flags_with_value:
            i += 2
            continue
        # Bare flag.
        i += 1
    if takes_one_positional and i < len(head):
        i += 1
    return _strip_wrappers(head[i:], depth + 1)


def _expand_brace_lists(cmd: str) -> list[str]:
    """Return every plausible brace-expansion of ``cmd``.

    Bash expands ``{a,b,c}`` BEFORE word-splitting, so ``{r,m} -rf /``
    actually invokes ``rm`` (the second alternative) — but the
    classifier's tokeniser sees the literal ``{r,m}`` as a single
    token and skips it. We brute-force every Cartesian product of the
    comma-lists in the string and re-classify each variant.

    Bounded: at most ~32 variants. Pathological inputs with deep
    nesting fall back to returning the original string unchanged.
    """
    # Match the simplest comma-list form: {a,b,c} with no nesting.
    pat = re.compile(r"\{([^{}]+)\}")
    out = [cmd]
    for _ in range(4):  # bounded depth
        new_out: list[str] = []
        any_match = False
        for s in out:
            m = pat.search(s)
            if not m or "," not in m.group(1):
                new_out.append(s)
                continue
            any_match = True
            head, tail = s[: m.start()], s[m.end():]
            for alt in m.group(1).split(","):
                new_out.append(head + alt + tail)
            if len(new_out) > 32:
                # Cardinality blowup — bail out, classifier still has
                # other defences. Return what we built so far.
                return new_out[:32]
        out = new_out
        if not any_match:
            break
    return out


def _strip_ifs_substitutions(cmd: str) -> str:
    """Replace ``${IFS}`` / ``$IFS`` with a literal space.

    Bash uses IFS as a token separator, so ``r${IFS}m -rf /`` runs
    ``rm -rf /``. The tokeniser sees ``r${IFS}m`` as a single token
    instead. Substitute then re-tokenise.
    """
    # Order matters — strip the curly form before the bare form so we
    # don't leave dangling braces.
    out = re.sub(r"\$\{\s*IFS\s*\}", " ", cmd)
    out = re.sub(r"\$IFS(?![A-Za-z0-9_])", " ", out)
    return out


def _has_base64_pipe_to_shell(cmd: str) -> bool:
    """Detect ``echo … | base64 -d | sh`` and friends.

    A motivated attacker base64-encodes the destructive payload so
    every recursive _is_dangerous_command pass sees only the literal
    ``echo <gibberish> | base64 -d``. We can't decode the gibberish
    safely (LLM-emitted base64 may contain anything), so the rule
    instead flags the *shape*: a base64 / hex-decode tool whose
    output is piped into a shell interpreter or ``eval``.
    """
    norm = cmd.lower()
    # Decoder tokens: base64 -d / --decode, xxd -r, openssl base64 -d,
    # printf … | python -c 'import base64;…'
    decoders = (
        "base64 -d", "base64 --decode", "base64 -di", "base64 -D",
        "xxd -r", "xxd -p -r", "openssl base64 -d",
        "openssl enc -base64 -d", "openssl enc -d -base64",
        "b64decode", "atob(",
    )
    if not any(d in norm for d in decoders):
        return False
    # Output piped (or process-substituted) to a shell interpreter.
    # ``| sh`` / ``| bash`` / ``|sh`` / ``| zsh`` / ``| eval``.
    pipe_to_shell = re.search(
        r"\|\s*(?:sh|bash|zsh|ksh|dash|ash|fish|eval)\b",
        norm,
    )
    if pipe_to_shell:
        return True
    # ``$( … | base64 -d )`` or ```base64 -d`` directly invoked via
    # ``bash -c "$(…)"`` is also obfuscation. We don't decode the
    # payload but flag the shape conservatively.
    if re.search(r"\$\(\s*[^)]*base64[^)]*\)", norm):
        return True
    if re.search(r"`[^`]*base64[^`]*`", norm):
        return True
    return False


def _strip_unicode_format_chars(s: str) -> str:
    """NFKC-normalize ``s`` and strip Unicode Cf-category format chars.

    Cf includes zero-width space (ZWSP, U+200B), zero-width joiner
    (ZWJ, U+200D), right-to-left mark (RLM), left-to-right embedding
    (LRE) and friends. An attacker can sprinkle these between letters
    of a destructive verb so substring/regex matchers miss it but a
    shell still parses the command identically (most shells ignore
    these as whitespace or treat them as part of an unquoted token
    that bash later expands harmlessly).

    NFKC also folds visually-identical compatibility forms — e.g. the
    fullwidth ``ｒｍ`` (U+FF52 U+FF4D) becomes plain ``rm`` so the
    classifier sees the same verb the shell does.
    """
    if not s:
        return s
    norm = unicodedata.normalize("NFKC", s)
    return "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")


# Pipe-to-shell pattern: ``curl … | bash``, ``wget … | sh``,
# ``Invoke-WebRequest … | python``. Always at least medium-risk
# because the LLM is downloading code and immediately executing it
# against the host with no opportunity for the operator to inspect.
_PIPE_TO_SHELL_RE = re.compile(
    r"(curl|wget|fetch|iwr|Invoke-WebRequest)\b.*\|\s*"
    r"(bash|sh|zsh|python\d?|perl|node|ruby|pwsh)",
    re.IGNORECASE,
)


def _is_dangerous_command(cmd: str, _depth: int = 0) -> bool:
    """Check if a shell command is potentially destructive.

    Defence-in-depth: tokenises the command, walks every sub-shell
    (``;``, ``|``, ``&&``, ``$(...)``, backticks, newline) and looks for
    destructive verbs anywhere in the chain. Also strips known
    wrappers (``sudo``, ``nohup``, ``timeout N``, ``bash -c …``, …)
    so that ``sudo -u root rm -rf /`` is treated the same as the
    bare verb.

    Patterns flagged (each represents irreversible damage):
      * ``rm -r``/``rm -rf`` (recursive delete)
      * ``dd if=/dev/zero of=/dev/<disk>`` / ``ddrescue`` (disk wipe)
      * ``mkfs.*`` / ``mke2fs`` (filesystem format)
      * ``wipefs`` / ``blkdiscard`` / ``shred /dev/<disk>``
      * ``parted`` / ``fdisk`` / ``gdisk`` against /dev/<disk>
      * fork bomb (any name + |/& reorder)
      * ``> /dev/sd*``, ``>| /dev/sd*``, ``tee /dev/sd*`` (raw write)
      * ``chmod -R 000 /`` and similar (system-wide perm nuke)
      * ``python -c "shutil.rmtree('/')"`` and friends; same for
        ``node -e``, ``perl -e``, ``ruby -e``
      * ``xargs rm`` / ``find … -delete`` (mass delete pipelines)
      * Persistence-vector writes — cron, sudoers, .bashrc, authorized_keys,
        passwd/shadow, systemd unit files

    Note: this is best-effort. A motivated attacker can still encode
    arguments (base64-decode, ``${IFS}`` tricks, etc.). The threat model
    is "LLM emits a command it shouldn't run", not "hostile shell".
    """
    if not cmd or not cmd.strip():
        return False
    if _depth > 4:
        # Bound the recursion in case of pathological nesting.
        return False

    # NFKC-normalize and strip Cf-category Unicode (ZWSP, ZWJ, RTL/LRM,
    # BOM, fullwidth) so the classifier sees the same bytes the shell
    # eventually runs. Without this, ``r\u200bm -rf /`` slipped through
    # because the substring/regex checks didn't match the embedded
    # zero-width space.
    cmd = _strip_unicode_format_chars(cmd)

    # Fork bomb has no clean tokenisation — match the literal signature.
    if _has_fork_bomb(cmd):
        return True

    # Raw redirect to disk device — substring check is enough.
    if _redirect_targets_disk(cmd):
        return True

    # Persistence-vector redirects (cron, sudoers, .bashrc, …).
    if _redirect_targets_persistence(cmd):
        return True

    # Walk every sub-command. For each, tokenise and inspect.
    subs = _split_into_subcommands(cmd) or [cmd]
    for sub in subs:
        tokens = _tokenize(sub)
        if not tokens:
            continue
        # Skip leading env assignments like ``FOO=bar cmd …``.
        i = 0
        while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
            i += 1
        head = tokens[i:]
        if not head:
            continue

        # Strip recognised wrappers (sudo, nohup, timeout, nice, env, ...).
        head = _strip_wrappers(head)
        if not head:
            continue

        # Shell interpreter ``-c <PAYLOAD>`` / ``--command <PAYLOAD>``: the
        # payload is itself a shell command, so re-classify it recursively.
        # This is the critical fix that closes ``bash -c "rm -rf /"``.
        base = os.path.basename(head[0]).lower()
        if base in _SHELL_INTERPRETERS:
            for flag in ("-c", "--command"):
                if flag in head:
                    idx = head.index(flag)
                    if idx + 1 < len(head):
                        if _is_dangerous_command(head[idx + 1], _depth + 1):
                            return True
            # bash/sh as a wrapper in another form (``bash script.sh``) —
            # we don't have the script body so we can't inspect it. Fall
            # through to the verb checks just in case.

        if _rm_is_dangerous(head):
            return True
        if _dd_is_dangerous(head):
            return True
        if _mkfs_is_dangerous(head):
            return True
        if _disk_wipe_tool_is_dangerous(head):
            return True
        if _chmod_is_dangerous(head):
            return True
        if _python_dash_c_is_dangerous(head):
            return True
        if _node_dash_e_is_dangerous(head):
            return True
        if _perl_dash_e_is_dangerous(head):
            return True
        if _ruby_dash_e_is_dangerous(head):
            return True
        if _xargs_or_find_delete(head):
            return True

        # Catch verbs from the legacy substring list (git push --force, drop
        # table, etc.) by their first token.
        if head:
            base = os.path.basename(head[0]).lower()
            joined = " ".join(head).lower()
            if base in ("rmdir", "fdisk"):
                return True
            if joined.startswith("git reset --hard") or \
               joined.startswith("git clean -f") or \
               joined.startswith("git push -f") or \
               joined.startswith("git push --force") or \
               joined.startswith("git checkout --"):
                return True
            if joined.startswith("drop table") or \
               joined.startswith("drop database") or \
               joined.startswith("truncate "):
                return True

    return False


def _tool_subtitle_for_approval(tool_name: str, args: dict) -> str:
    """Generate a human-readable description for approval prompt."""
    if tool_name == "terminal":
        cmd = args.get("command", "")
        mode = args.get("mode", "normal")
        if mode == "background":
            return f"[background] {cmd[:100]}"
        return cmd[:120]
    elif tool_name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        return f"Write {len(content)} chars → {path}"
    elif tool_name == "edit_file":
        return f"Edit {args.get('path', '')}"
    elif tool_name == "cogit":
        return f"{args.get('action', '')} {args.get('label', '')}"
    elif tool_name == "delegate_task":
        return f"mode={args.get('mode', '')}"
    return str(args)[:100]


# Medium-risk patterns (not destructive but worth noting)
_MEDIUM_COMMANDS = (
    "pip install", "pip uninstall", "npm install", "npm uninstall",
    "apt install", "apt remove", "pacman -S", "pacman -R",
    "systemctl", "chmod", "chown", "curl -X POST", "curl -X PUT",
    "curl -X DELETE", "git push", "git merge", "git rebase",
    "docker rm", "docker stop", "kill ", "pkill ",
)


def classify_danger(tool_name: str, arguments: dict) -> str:
    """Classify tool call danger level: 'low', 'medium', or 'danger'.

    Returns the level as a string.
    """
    # MCP tools: per-tool risk override from ~/.config/cogitum/mcp.toml
    if tool_name.startswith("mcp_"):
        try:
            from cogitum.core.mcp import risk_for_mcp_tool
            risk = risk_for_mcp_tool(tool_name)
            if risk in ("low", "medium", "danger"):
                return risk
        except Exception:
            # MCP discovery may not be initialised in some test paths;
            # log at debug so production noise stays low but a real
            # failure is grep-able instead of fully invisible.
            log.debug("risk_for_mcp_tool lookup failed for %s", tool_name, exc_info=True)
        # Fallback: medium so unknown MCP tools require approval by default
        return "medium"

    # Terminal commands need deeper analysis
    if tool_name == "terminal":
        cmd = arguments.get("command", "")
        mode = arguments.get("mode", "normal")
        # ── background management actions: read/list/kill/close are low,
        #    write/spawn need extra analysis ──
        if mode == "background" and cmd in ("list", "read", "kill", "close"):
            return "low"

        # ── write to a background process's stdin ──
        # If the target process is itself a shell (bash/sh/zsh/...), treat
        # any stdin_data as live command execution. The classifier only
        # ever saw the *shell spawn* command before, so an LLM could spawn
        # `bash` (low/medium) and then write `rm -rf /` to its stdin
        # without re-triggering approval. Now: if PID is a known shell,
        # the stdin_data itself is classified, and the floor is `medium`.
        if mode == "background" and cmd == "write":
            pid = arguments.get("pid", 0)
            stdin_data = arguments.get("stdin_data", "")
            if _pid_is_interactive_shell(pid):
                if _is_dangerous_command(stdin_data):
                    return "danger"
                return "medium"
            # Non-shell PIDs: stdin is just data. Still bump to medium if
            # it looks shell-y (defence-in-depth).
            if _is_dangerous_command(stdin_data):
                return "danger"
            return "low"

        if _is_dangerous_command(cmd):
            return "danger"
        # Pipe-to-shell (curl|bash, wget|sh, iwr|pwsh, …) — always at
        # least medium so the operator gets to see what URL is being
        # piped into a code interpreter before it runs.
        normalized = _strip_unicode_format_chars(cmd)
        if _PIPE_TO_SHELL_RE.search(normalized):
            return "medium"
        lower = cmd.lower().strip()
        if any(lower.startswith(m) or f" {m}" in lower for m in _MEDIUM_COMMANDS):
            return "medium"
        # Background mode is medium (long-running)
        if mode == "background" and cmd not in ("list", "read", "kill", "write"):
            return "medium"
        return "low"

    # Write operations
    if tool_name == "write_file":
        path = arguments.get("path", "")
        # Overwriting config files is medium
        if any(x in path for x in (".env", "config", ".toml", ".yaml", ".yml")):
            return "medium"
        return "low"

    if tool_name == "edit_file":
        return "low"

    # Cogit restore is medium (changes files)
    if tool_name == "cogit" and arguments.get("action") == "restore":
        return "medium"

    # Browser actions are low
    if tool_name == "browser":
        return "low"

    # send_media exfiltrates a file to the user's Telegram chat. Even
    # with the path-sandbox filter, the operator should see *what* is
    # being shipped before approving. Floor at medium so every send
    # gets an approval prompt unless yolo-mode is on.
    if tool_name == "send_media":
        return "medium"

    # Everything else is low
    return "low"


def _auto_cogit_save(label: str, scope_path: str | None = None) -> str | None:
    """Auto-save a cogit checkpoint before dangerous operations.
    
    scope_path: if provided, checkpoint only that file/dir (much faster than whole project).
                If file is outside project_dir, skip checkpoint entirely (not our concern).
    Returns None on success, error string on failure.
    """
    try:
        from cogitum.core.cogit import CogitStore
        session_id = os.environ.get("COGITUM_SESSION_ID", "default")
        project_dir = os.environ.get("COGITUM_PROJECT_DIR", os.getcwd())
        # Determine scope
        scope = None
        if scope_path:
            try:
                from pathlib import Path as _P
                p = _P(scope_path).expanduser().resolve()
                pd = _P(project_dir).resolve()
                # If file is outside project_dir, skip checkpoint entirely
                rel = p.relative_to(pd)
                scope = str(rel)
            except (ValueError, OSError):
                # File outside project — don't checkpoint random files
                return None
        store = CogitStore(session_id=session_id, project_dir=project_dir)
        store.save(label=f"auto: {label}", scope=scope)
        return None
    except Exception:
        return None  # don't block the operation on checkpoint failure


def _is_path_safe(p: Path) -> tuple[bool, str]:
    """Check if a path is safe to access. Returns (safe, reason).

    Cross-platform — normalises paths to forward-slash, lower-cases on
    Windows (NTFS is case-insensitive). Sensitive sets are platform-
    specific (POSIX system files vs Windows registry hives).
    """
    resolved = str(p.resolve())
    # Normalise to forward slashes so the same comparison logic works
    # for /proc/foo and C:\Windows\System32\config\SAM.
    needle = resolved.replace("\\", "/")
    if _sys.platform == "win32":
        needle = needle.lower()

    # Block prefix-rooted areas (/proc/, /sys/, Windows/System32/, ...).
    for prefix in _SENSITIVE_PREFIXES:
        # On Windows, sensitive prefixes don't have a leading slash —
        # check for the segment anywhere in the path. On POSIX, the
        # prefix already starts with /, so substring match works too.
        if prefix in needle:
            return False, f"access denied: {prefix.rstrip('/')} is restricted"

    # Block specific known files.
    for sensitive in _SENSITIVE_PATHS:
        if needle.endswith(sensitive) or f"/{sensitive}" in needle:
            return False, "access denied: sensitive file"

    return True, ""


def _ip_is_unsafe(ip: ipaddress._BaseAddress) -> bool:
    """Return True if the IP address falls into any non-public range.

    We deliberately reject more than just RFC1918 — link-local, loopback,
    multicast, unspecified (0.0.0.0/::), reserved, and the AWS/GCP
    metadata pseudo-network all expose internal services.
    """
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalize_host(hostname: str) -> str:
    """Normalize a URL host before resolution.

    Strips IPv6 brackets and lower-cases. Bare integer hostnames
    (``http://2130706433/`` — decimal form of 127.0.0.1) are converted
    to dotted-quad so ``ipaddress.ip_address`` accepts them. Without
    this step, ``ipaddress.ip_address('2130706433')`` raises ValueError
    and the old code happily fell through to "must be a domain — ok".
    """
    h = hostname.strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    # All-digit host? Treat as 32-bit integer IPv4.
    if h.isdigit():
        try:
            as_int = int(h)
            if 0 <= as_int <= 0xFFFFFFFF:
                h = str(ipaddress.IPv4Address(as_int))
        except (ValueError, ipaddress.AddressValueError):
            pass
    return h


def _is_url_safe(url: str) -> tuple[bool, str]:
    """Check if a URL is safe to fetch (best-effort SSRF defence).

    Defends against:
      * direct loopback / private / link-local IPs
      * cloud metadata endpoints (169.254.169.254, metadata.google.internal)
      * obfuscated IP forms — decimal (``2130706433``), short (``127.1``,
        ``0``), octal (``0177.0.0.1``), hex (``0x7f.1``), IPv6-mapped
        (``[::ffff:127.0.0.1]``)
      * domains whose A/AAAA records point at a private range — we
        resolve via ``socket.getaddrinfo`` and reject if **any** answer
        is unsafe.

    LIMITATION: this is best-effort only. A determined attacker can
    still pull off a true DNS rebind by serving a public IP at validation
    time and a private IP a few seconds later when httpx actually
    connects. True mitigation requires same-host pinning at the socket
    layer (resolve once, then connect to the resolved IP with a Host
    header). Not implemented here — the realistic threat model for this
    tool is "LLM follows a malicious link", not "attacker controls
    authoritative DNS".
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "invalid URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"scheme {parsed.scheme!r} not allowed (http/https only)"

    raw_host = parsed.hostname or ""
    if not raw_host:
        return False, "URL has no hostname"

    # Reject any hostname containing Cf-category Unicode (zero-width
    # space, joiner, RTL/LRM marks, BOM, …). Those characters are
    # invisible in logs/diffs and let an attacker craft a hostname
    # that looks identical to a safe one but resolves elsewhere
    # post-IDNA. We also can't safely DNS-resolve them — punycode
    # treatment varies by resolver, so refuse outright.
    for ch in raw_host:
        if unicodedata.category(ch) == "Cf":
            return False, (
                f"hostname {raw_host!r} contains zero-width/format "
                "Unicode (Cf-category) — denied"
            )

    hostname = _normalize_host(raw_host)

    # Cheap textual check first — catches the obvious names and avoids
    # an unnecessary DNS round-trip for the common "block localhost" case.
    if hostname in ("localhost", "ip6-localhost", "ip6-loopback",
                    "metadata.google.internal", "metadata"):
        return False, f"localhost/metadata host {raw_host!r} denied"

    # If the (normalized) host parses as an IP literal, check it directly.
    try:
        ip = ipaddress.ip_address(hostname)
        if _ip_is_unsafe(ip):
            return False, f"private/internal IP {raw_host!r} denied"
        # CGNAT (RFC 6598, 100.64.0.0/10) — Python's is_private misses
        # this, but cloud providers route it to internal services.
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            return False, f"CGNAT range IP {raw_host!r} denied"
        # Cloud metadata IPs are technically link-local (already caught
        # by _ip_is_unsafe), but call them out explicitly for clarity.
        if str(ip) in ("169.254.169.254", "fd00:ec2::254"):
            return False, "cloud metadata endpoint denied"
        return True, ""
    except ValueError:
        pass  # Not an IP literal — must be a domain. Fall through to DNS.

    # Domain: resolve and verify EVERY returned address. This catches
    # the case where attacker.com legitimately A-records to 127.0.0.1
    # (or to an internal IP an attacker is trying to scan).
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError, OSError) as e:
        return False, f"DNS resolution failed for {raw_host!r}: {e}"

    if not infos:
        return False, f"DNS returned no addresses for {raw_host!r}"

    for info in infos:
        sockaddr = info[4]
        addr = sockaddr[0]
        # IPv6 scoped addrs come back as 'fe80::1%eth0' — strip zone.
        if "%" in addr:
            addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return False, f"unparseable resolved address {addr!r}"
        if _ip_is_unsafe(ip):
            return False, (
                f"hostname {raw_host!r} resolves to non-public address {addr}"
            )
        if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
            return False, (
                f"hostname {raw_host!r} resolves to CGNAT address {addr}"
            )

    return True, ""

# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------

@tool(tags=["fs", "read"])
def read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    """Read a text file and return its contents with line numbers.

    path: Absolute or relative path to the file.
    offset: First line to return (1-indexed).
    limit: Maximum number of lines to return.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    lines = p.read_text(errors="replace").splitlines()
    total = len(lines)
    chunk = lines[offset - 1 : offset - 1 + limit]
    numbered = "\n".join(f"{offset + i}|{line}" for i, line in enumerate(chunk))
    return f"[{total} lines total, showing {offset}–{offset + len(chunk) - 1}]\n{numbered}"


@tool(tags=["fs", "write"])
def write_file(path: str, content: str) -> str:
    """Write content to a file, creating parent directories as needed.

    path: Absolute or relative path to the file.
    content: Full text content to write.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    # Auto-save checkpoint if file already exists (overwrite = destructive)
    if p.exists():
        _auto_cogit_save(f"before write_file {path}", scope_path=str(p))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"OK: wrote {len(content)} bytes to {path}"


@tool(tags=["fs", "write"])
def append_file(path: str, content: str) -> str:
    """Append content to a file (creates it if missing).

    path: Absolute or relative path to the file.
    content: Text to append.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(content)
    return f"OK: appended {len(content)} bytes to {path}"


@tool(tags=["fs", "write"])
def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Targeted find-and-replace in a file. Errors if old_string is not found or matches multiple locations.

    path: Absolute or relative path to the file.
    old_string: Exact text to find (must match exactly once in the file).
    new_string: Replacement text.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    if not p.exists():
        return f"ERROR: file not found: {path}"
    # Auto-save checkpoint before editing
    _auto_cogit_save(f"before edit_file {path}", scope_path=str(p))
    content = p.read_text(errors="replace")
    count = content.count(old_string)
    if count == 0:
        return "ERROR: old_string not found in file"
    if count > 1:
        return f"ERROR: old_string matches {count} locations (must be unique)"
    # Find line number of the match for context
    idx = content.index(old_string)
    line_num = content[:idx].count("\n") + 1
    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content)
    # Show context around the replacement
    lines = new_content.splitlines()
    new_line_count = new_string.count("\n") + 1
    start = max(0, line_num - 2)
    end = min(len(lines), line_num + new_line_count + 1)
    context = "\n".join(f"{start + i + 1}|{lines[start + i]}" for i in range(end - start))
    return f"OK: replaced at line {line_num}\n{context}"


@tool(tags=["fs", "search"])
def search_files(pattern: str, path: str = ".", file_glob: Optional[str] = None) -> str:
    """Search file contents with ripgrep (regex).

    pattern: Regex pattern to search for.
    path: Directory or file to search in.
    file_glob: Optional glob to filter files, e.g. '*.py'.
    """
    cmd = ["rg", "--line-number", "--color=never", "-m", "5", pattern, path]
    if file_glob:
        cmd += ["-g", file_glob]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = result.stdout.strip()
        return out if out else "(no matches)"
    except FileNotFoundError:
        # fallback: grep
        cmd2 = ["grep", "-rn", "--include", file_glob or "*", pattern, path]
        result2 = subprocess.run(cmd2, capture_output=True, text=True, timeout=15)
        return result2.stdout.strip() or "(no matches)"
    except subprocess.TimeoutExpired:
        return "ERROR: search timed out"


@tool(tags=["fs"])
def list_dir(path: str = ".") -> str:
    """List files and directories at a path.

    path: Directory to list.
    """
    p = Path(path).expanduser()
    safe, reason = _is_path_safe(p)
    if not safe:
        return f"ERROR: {reason}"
    if not p.exists():
        return f"ERROR: path not found: {path}"
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name))
    lines = []
    for e in entries:
        kind = "F" if e.is_file() else "D"
        try:
            size = f"{e.stat().st_size:>10}" if e.is_file() else "          "
        except (OSError, PermissionError):
            size = "         ?"
        lines.append(f"[{kind}] {size}  {e.name}")
    return "\n".join(lines) or "(empty)"


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

# `start_new_session=True` is POSIX-only (setsid()). On Windows it either
# raises ValueError on older Pythons or silently breaks stdout capture on
# newer ones — users see "(no output)" or "[exit N]" with no body. The
# Windows equivalent is creationflags=CREATE_NEW_PROCESS_GROUP, which
# detaches the child from our Ctrl+C handler the same way setsid does on
# POSIX. Detection is per-process so a Linux Cogitum talking to a Windows
# Telegram gateway still picks the right shape from each side.
if _sys.platform == "win32":
    _SUBPROC_DETACH_KWARGS = {
        # CREATE_NEW_PROCESS_GROUP isolates the child from our console
        # so we can kill it without taking out our own process.
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
    }
else:
    _SUBPROC_DETACH_KWARGS = {"start_new_session": True}


@tool(tags=["shell"])
async def terminal(
    command: str,
    workdir: Optional[str] = None,
    mode: str = "normal",
    timeout: int = 120,
    pid: int = 0,
    stdin_data: str = "",
    last_n: int = 50,
) -> str:
    """Run shell commands in three modes: normal, timeout, background.

    PARAMETERS
      command:     Shell command to run, OR a background action verb
                   ('list' / 'read' / 'kill' / 'write' / 'close') when
                   mode='background' and you're managing an existing process.
      workdir:     Working directory (defaults to current).
      mode:        'normal' | 'timeout' | 'background'.  Default 'normal'.
      timeout:     Hard time-limit in seconds for mode='timeout' (default 120).
                   Ignored otherwise.
      pid:         PID of an existing background process for read/kill/write/close.
      stdin_data:  Text to send to a background process's stdin via 'write'.
      last_n:      Tail size when reading background output (default 50).

    MODES

      normal     Run synchronously, no timeout. Returns full stdout+stderr
                 once the command exits. Best for short interactive things
                 (ls, cat, git status). Output is capped at 50KB.

      timeout    Same as normal, but the command is killed if it exceeds
                 `timeout` seconds. On kill returns the message
                 "TIMEOUT: command killed after Ns. Last output: ...". Use
                 this when you want a hard guarantee the call won't hang.

      background Spawn the command and return its PID immediately. The agent
                 keeps working while the process runs. Then issue follow-ups:
                   command='list',  mode='background'             → all PIDs
                   command='read',  mode='background', pid=N      → tail output
                   command='write', mode='background', pid=N,
                                   stdin_data='answer'            → send to stdin (\\n appended)
                   command='close', mode='background', pid=N      → close stdin (EOF)
                   command='kill',  mode='background', pid=N      → terminate
                 Use background for servers, watchers, long builds, anything
                 that needs interactive stdin, or work you want to overlap
                 with other tool calls.
    """
    from cogitum.core.process_manager import ProcessManager

    # Auto-save checkpoint before dangerous commands
    if _is_dangerous_command(command) and command not in ("list", "read", "kill", "write", "close"):
        _auto_cogit_save(f"before terminal: {command[:50]}")

    cwd = workdir or os.getcwd()
    pm = ProcessManager.get()

    # ── Background mode: management actions ──
    if mode == "background":
        if command == "list":
            pm.cleanup_finished_older_than(seconds=300)  # housekeeping
            procs = pm.list_processes()
            if not procs:
                return "No background processes running."
            lines = ["Background processes:"]
            for bp in procs:
                cmd_short = bp.command[:60] + ("…" if len(bp.command) > 60 else "")
                lines.append(f"  PID {bp.pid} | {bp.status} | {bp.uptime:.0f}s | {cmd_short}")
            return "\n".join(lines)

        elif command == "read":
            if not pid:
                return "ERROR: pid required for 'read' action"
            return pm.read_output(pid, last_n=last_n)

        elif command == "kill":
            if not pid:
                return "ERROR: pid required for 'kill' action"
            return await pm.kill(pid)

        elif command == "write":
            if not pid:
                return "ERROR: pid required for 'write' action"
            if not stdin_data:
                return "ERROR: stdin_data required for 'write' action"
            return await pm.write_stdin(pid, stdin_data)

        elif command == "close":
            if not pid:
                return "ERROR: pid required for 'close' action"
            return await pm.close_stdin(pid)

        else:
            # Start a new background process
            bp = await pm.spawn(command, workdir=cwd)
            await asyncio.sleep(0.3)  # brief wait to catch immediate failures
            if bp.finished:
                output = "\n".join(bp.output_lines[-20:])
                return (
                    f"Process exited immediately (exit {bp.exit_code}):\n{output}"
                )
            return (
                f"OK: started background process PID {bp.pid}\n"
                f"Use terminal(command='read', mode='background', pid={bp.pid}) to check output, "
                f"'write' to send stdin, 'kill' to stop."
            )

    # ── Normal mode: no timeout ──
    if mode == "normal":
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                **_SUBPROC_DETACH_KWARGS,
            )
            stdout, _ = await proc.communicate()
            output = stdout.decode(errors="replace").strip()
            # Cap output at 50KB
            if len(output) > 50000:
                output = output[:50000] + "\n… (truncated, 50KB limit)"
            rc = proc.returncode
            if rc != 0:
                return f"[exit {rc}]\n{output}"
            return output or "(no output)"
        except Exception as e:
            return f"ERROR: {e}"

    # ── Timeout mode: kill if exceeds limit ──
    if mode == "timeout":
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=cwd,
                **_SUBPROC_DETACH_KWARGS,
            )
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                # Capture any partial output before killing
                partial = b""
                try:
                    if proc.stdout:
                        partial = await asyncio.wait_for(proc.stdout.read(8192), timeout=0.5)
                except Exception:
                    log.debug("swallowed exception", exc_info=True)
                proc.kill()
                await proc.wait()
                tail = partial.decode(errors="replace").strip()[-2000:]
                return (
                    f"TIMEOUT: command killed after {timeout}s.\n"
                    f"Last output:\n{tail or '(none captured)'}\n"
                    f"Hint: switch to mode='background' if the command is long-running."
                )
            output = stdout.decode(errors="replace").strip()
            if len(output) > 50000:
                output = output[:50000] + "\n… (truncated, 50KB limit)"
            rc = proc.returncode
            if rc != 0:
                return f"[exit {rc}]\n{output}"
            return output or "(no output)"
        except Exception as e:
            return f"ERROR: {e}"

    return f"ERROR: unknown mode '{mode}' (use 'normal', 'timeout', or 'background')"


# ---------------------------------------------------------------------------
# Web / fetch
# ---------------------------------------------------------------------------

@tool(tags=["web"])
async def fetch_url(url: str, max_chars: int = _BROWSER_TEXT_CAP_CHARS) -> str:
    """Fetch a URL and return its text content (HTML stripped).

    url: URL to fetch.
    max_chars: Maximum characters to return.

    Security notes:
      * Every URL — including each redirect Location — is validated by
        ``_is_url_safe`` before we open the connection. Without this,
        a public site could 302 us to ``http://169.254.169.254/...``
        (AWS IMDS) and exfiltrate IAM credentials. ``follow_redirects``
        on the client is therefore disabled and we walk the chain
        manually so each hop is checked.
      * Body size is hard-capped at ``MAX_BYTES`` and read in chunks.
        httpx auto-decodes gzip/br, so a 10 KB compressed bomb can
        otherwise expand to gigabytes and OOM the agent. Streaming
        plus a running byte total kills it early.
    """
    safe, reason = _is_url_safe(url)
    if not safe:
        return f"ERROR: {reason}"
    try:
        import httpx
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style", "head"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style", "head"):
                    self._skip = False

            def handle_data(self, data):
                if not self._skip:
                    stripped = data.strip()
                    if stripped:
                        self.parts.append(stripped)

        MAX_BYTES = _FETCH_MAX_BYTES
        MAX_REDIRECTS = _FETCH_MAX_REDIRECTS

        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_FETCH_TIMEOUT_S
        ) as client:
            current = url
            body_bytes = b""
            content_type = ""
            for hop in range(MAX_REDIRECTS + 1):
                async with client.stream(
                    "GET",
                    current,
                    headers={"User-Agent": "Cogitum/1.0"},
                ) as resp:
                    # 3xx — re-validate the next URL before following.
                    if 300 <= resp.status_code < 400 and "location" in resp.headers:
                        if hop >= MAX_REDIRECTS:
                            return f"ERROR: too many redirects (>{MAX_REDIRECTS})"
                        loc = resp.headers["location"].strip()
                        # Empty Location header — RFC 7230 says the value
                        # MUST be a URI-reference, but real servers ship
                        # ``Location:`` with empty body. urljoin("", X)
                        # returns X, so a naive loop would refetch the
                        # SAME URL up to MAX_REDIRECTS times — DoS amp.
                        if not loc:
                            return (
                                "ERROR: redirect blocked: empty Location header"
                            )
                        next_url = urljoin(current, loc)
                        # Pure-fragment redirect (Location: '#x') resolves
                        # to current URL with a different fragment. Real
                        # browsers don't refetch — neither should we.
                        if next_url == current or (
                            urlparse(next_url)._replace(fragment="").geturl()
                            == urlparse(current)._replace(fragment="").geturl()
                        ):
                            return (
                                "ERROR: redirect blocked: Location resolves "
                                "to the same URL (fragment-only or empty)"
                            )
                        ok, why = _is_url_safe(next_url)
                        if not ok:
                            return f"ERROR: redirect blocked: {why}"
                        current = next_url
                        continue

                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")

                    # Stream the body and abort if it grows past MAX_BYTES.
                    # httpx transparently decompresses gzip/br here, which
                    # is exactly the layer where decompression bombs hit.
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes(chunk_size=_FETCH_CHUNK_SIZE):
                        total += len(chunk)
                        if total > MAX_BYTES:
                            return (
                                f"ERROR: response too large "
                                f"(>{MAX_BYTES // (1024 * 1024)}MB)"
                            )
                        chunks.append(chunk)
                    body_bytes = b"".join(chunks)
                    encoding = resp.encoding or "utf-8"
                    break
            else:
                # Loop exhausted without break — only happens if every
                # iteration was a redirect, which the in-loop guard
                # already returned for. Belt-and-braces.
                return f"ERROR: too many redirects (>{MAX_REDIRECTS})"

        try:
            body_text = body_bytes.decode(encoding, errors="replace")
        except (LookupError, TypeError):
            body_text = body_bytes.decode("utf-8", errors="replace")

        if "html" in content_type:
            parser = _Stripper()
            parser.feed(body_text)
            text = "\n".join(parser.parts)
        else:
            text = body_text
        return text[:max_chars]
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

@tool(tags=["memory"])
def memory(action: str, target: str = "memory", content: str = "", old_text: str = "") -> str:
    """Persistent memory that survives across sessions.

    action: 'add', 'replace', or 'remove'.
    target: 'memory' (agent notes) or 'user' (user profile).
    content: The entry text (required for add/replace).
    old_text: Substring identifying the entry to replace/remove.
    """
    from cogitum.core.memory import memory_add, memory_replace, memory_remove

    if action == "add":
        if not content:
            return "ERROR: content required for add"
        return memory_add(target, content)
    elif action == "replace":
        if not old_text or not content:
            return "ERROR: old_text and content required for replace"
        return memory_replace(target, old_text, content)
    elif action == "remove":
        if not old_text:
            return "ERROR: old_text required for remove"
        return memory_remove(target, old_text)
    else:
        return f"ERROR: unknown action '{action}' (use add/replace/remove)"


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------

@tool(tags=["skills"])
def skills(action: str, name: str = "", content: str = "", category: str = "") -> str:
    """Agent's procedural memory — reusable knowledge for recurring tasks.

    action: 'list', 'read', 'write', or 'delete'.
    name: Skill name (required for read/write/delete).
    content: Full skill markdown (required for write).
    category: Filter by category (for list) or assign category (for write).
    """
    from cogitum.core.skills import list_skills, read_skill, write_skill, delete_skill, list_categories

    if action == "list":
        items = list_skills(category=category)
        if not items:
            if category:
                cats = list_categories()
                return f"No skills in category '{category}'. Available categories: {', '.join(cats)}"
            return "No skills yet. Use skills(action='write', name='...', content='...') to create one."
        # Group by category
        by_cat: dict[str, list] = {}
        for s in items:
            by_cat.setdefault(s.category, []).append(s)
        lines = [f"Available skills ({len(items)}):"]
        for cat in sorted(by_cat):
            lines.append(f"\n  [{cat}]")
            for s in by_cat[cat]:
                desc = s.description[:60] + "…" if len(s.description) > 60 else s.description
                lines.append(f"    • {s.name}: {desc}")
        return "\n".join(lines)
    elif action == "read":
        if not name:
            return "ERROR: name required for read"
        text = read_skill(name)
        if text is None:
            return f"ERROR: skill '{name}' not found. Use skills(action='list') to see available."
        return text
    elif action == "write":
        if not name or not content:
            return "ERROR: name and content required for write"
        return write_skill(name, content, category=category or "custom")
    elif action == "delete":
        if not name:
            return "ERROR: name required for delete"
        return delete_skill(name)
    else:
        return f"ERROR: unknown action '{action}' (use list/read/write/delete)"


# ---------------------------------------------------------------------------
# Session Search (cross-session awareness)
# ---------------------------------------------------------------------------

@tool(tags=["sessions"])
def session_search(action: str, query: str = "", session_id: str = "", limit: int = 10, offset: int = 0) -> str:
    """Search and browse past conversation sessions.

    action: 'list', 'read', or 'search'.
    query: Search query for 'search' action (matches session titles).
    session_id: Session ID for 'read' action.
    limit: Max results for list/search, or max messages for read (default 10).
    offset: Skip first N results/messages (for pagination).

    Use this to recall past conversations, find context from previous sessions,
    or check what was discussed before.
    """
    from cogitum.core.sessions import get_store
    from datetime import datetime

    store = get_store()

    if action == "list":
        sessions = store.list_sessions(limit=limit)
        if not sessions:
            return "No past sessions found."
        lines = [f"Past sessions ({len(sessions)}):"]
        for s in sessions:
            ts = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            title = s.title or "(untitled)"
            lines.append(f"  • [{ts}] {title} ({s.count} msgs) — id:{s.id[:12]}")
        return "\n".join(lines)

    elif action == "search":
        if not query:
            return "ERROR: query required for search"
        results = store.search(query, limit=limit)
        if not results:
            return f"No sessions matching: {query}"
        lines = [f"Sessions matching '{query}':"]
        for s in results:
            ts = datetime.fromtimestamp(s.updated_at).strftime("%Y-%m-%d %H:%M")
            lines.append(f"  • [{ts}] {s.title} ({s.count} msgs) — id:{s.id[:12]}")
        return "\n".join(lines)

    elif action == "read":
        if not session_id:
            return "ERROR: session_id required for read"
        # Support partial ID match
        full_id = session_id
        if len(session_id) < 20:
            all_sessions = store.list_sessions(limit=200)
            matches = [s for s in all_sessions if s.id.startswith(session_id)]
            if not matches:
                return f"ERROR: no session found with id starting with '{session_id}'"
            if len(matches) > 1:
                return f"ERROR: ambiguous id '{session_id}' — matches {len(matches)} sessions"
            full_id = matches[0].id

        messages = store.load_session(full_id)
        if not messages:
            return f"Session {session_id} is empty."

        # Apply offset and limit
        subset = messages[offset:offset + limit]
        lines = [f"Session messages ({len(messages)} total, showing {offset+1}–{offset+len(subset)}):"]
        for msg in subset:
            role = msg.role.upper()
            # Extract text content
            text_parts = []
            for p in msg.parts:
                if hasattr(p, "text") and p.text:
                    text_parts.append(p.text[:200])
                elif hasattr(p, "name"):
                    text_parts.append(f"[tool_call: {p.name}]")
                elif hasattr(p, "content") and hasattr(p, "tool_call_id"):
                    preview = p.content[:100] if p.content else ""
                    text_parts.append(f"[result: {preview}]")
            content = " ".join(text_parts)[:300]
            ts = datetime.fromtimestamp(msg.timestamp).strftime("%H:%M") if msg.timestamp else ""
            lines.append(f"  [{ts}] {role}: {content}")
        return "\n".join(lines)

    else:
        return f"ERROR: unknown action '{action}' (use list/read/search)"


# ---------------------------------------------------------------------------
# Cogit (checkpoints)
# ---------------------------------------------------------------------------

@tool(tags=["cogit"])
def cogit(action: str, label: str = "", index: int = 0, scope: str = "") -> str:
    """Smart checkpoints — save/restore project state.

    action: 'save', 'list', 'restore', 'diff', or 'cleanup'.
    label: Description for save (e.g. 'before refactor auth').
    index: Checkpoint number for restore/diff.
    scope: Directory or file to checkpoint (relative path).
           Examples: 'src/', 'cogitum/core/', 'main.py'.
           Empty = entire project (with smart filtering).

    Use scope to checkpoint only the relevant part of the project.
    This keeps checkpoints fast and small.
    """
    from cogitum.core.cogit import CogitStore
    import os

    # Get session_id and project_dir from app context
    session_id = os.environ.get("COGITUM_SESSION_ID", "default")
    project_dir = os.environ.get("COGITUM_PROJECT_DIR", os.getcwd())

    store = CogitStore(session_id=session_id, project_dir=project_dir)

    if action == "save":
        cp = store.save(label=label, scope=scope or None)
        scope_info = f" [scope: {cp.scope}]" if cp.scope != "." else ""
        return f"OK: checkpoint #{cp.index} '{cp.label}' saved ({cp.file_count} files){scope_info}"
    elif action == "list":
        checkpoints = store.list_checkpoints()
        if not checkpoints:
            return "No checkpoints yet. Use cogit(action='save', label='...') to create one."
        lines = []
        for cp in checkpoints:
            from datetime import datetime
            ts = datetime.fromtimestamp(cp.timestamp).strftime("%H:%M")
            scope_info = f" [{cp.scope}]" if cp.scope != "." else ""
            lines.append(f"  #{cp.index} [{ts}] {cp.label} ({cp.file_count} files){scope_info}")
        return f"Checkpoints ({len(checkpoints)}):\n" + "\n".join(lines)
    elif action == "restore":
        if index <= 0:
            return "ERROR: index required (positive integer)"
        return store.restore(index)
    elif action == "diff":
        if index <= 0:
            return "ERROR: index required for diff"
        return store.diff(index)
    elif action == "cleanup":
        removed = store.cleanup(keep_last=10)
        return f"OK: removed {removed} old checkpoints" if removed else "Nothing to clean up."
    else:
        return f"ERROR: unknown action '{action}' (use save/list/restore/diff/cleanup)"


# ---------------------------------------------------------------------------
# Legion — recursive 2-level swarm. Supersedes the old delegate_task.
# ---------------------------------------------------------------------------
#
# The tool itself is a thin facade that hands control to the Agent's
# main loop via a sentinel string. The actual orchestration lives in
# cogitum.core.legion (runtime) and cogitum.core.legion_worker
# (agent-backed cogitator). See those modules for the lifecycle.
#
# Legion is gated behind ``experimental.legion_enabled`` in
# settings.toml — off by default. Toggle from the Setup wizard's
# "Experimental" section. Restart required (the flag is read here
# at import time so the registry stays cheap to query).


def _legion_enabled() -> bool:
    """Read the experimental flag once at import.

    Defensive: any failure to read settings means "off" so a broken
    config never silently exposes the experimental tool to the agent.
    """
    try:
        from .llm.loader import load_settings
        settings = load_settings() or {}
        exp = settings.get("experimental") or {}
        return bool(exp.get("legion_enabled", False))
    except Exception:
        return False


_LEGION_ENABLED = _legion_enabled()


def _legion_impl(tasks: str, root_goal: str = "") -> str:
    """Spawn a parallel team of Cogitators to work on independent subtasks.

    Each Cogitator gets the SAME tool catalog the lead Cogitum has,
    plus async sibling messaging. L1 Cogitators may further delegate
    to up to 3 L2 sub-Cogitators (max depth = 2 — L2 cannot recurse).

    Use this for genuinely independent work that benefits from
    parallelism: refactor + tests + docs in one shot, multi-file
    audit, parallel research over different sources, etc.

    DON'T use it when the steps are sequential (write file → run it →
    read result) — a single-actor loop is faster there.

    Args:
        tasks: JSON array of {id?, goal, context?} dicts.
               Maximum 5 L1 cogitators per call. Each "goal" must be
               a self-contained instruction; the cogitator does not
               see your conversation history, only its goal+context.
        root_goal: Short summary of the overall objective. Shown in
                   the legion tree UI as the root node title; not
                   passed to cogitators.

    Returns:
        Aggregated summary listing every L1 node's status and output.
    """
    import json as _json

    try:
        task_list = _json.loads(tasks) if isinstance(tasks, str) else tasks
    except _json.JSONDecodeError as e:
        return f"ERROR: invalid JSON in tasks: {e}"

    if not isinstance(task_list, list) or not task_list:
        return "ERROR: tasks must be a non-empty array"

    # Defer execution to the agent loop's _run_legion() — it has the
    # async machinery and event-queue plumbing for the TUI tree view.
    payload = {"tasks": task_list, "root_goal": root_goal or ""}
    return f"LEGION_RUN:{_json.dumps(payload)}"


# Register the legion tool ONLY when the experimental flag is on.
# When off, the function still exists in this module (for direct
# imports / tests) but the @tool decorator is skipped, so the
# REGISTRY never advertises it to the LLM.
#
# We pass name="legion" explicitly so the tool advertises as
# "legion" even though the underlying function is _legion_impl
# (we needed two names so the decorator path can be conditional).
if _LEGION_ENABLED:
    legion = tool(name="legion", tags=["legion", "delegate"])(_legion_impl)
else:
    legion = _legion_impl  # not in REGISTRY


# ---------------------------------------------------------------------------
# Delegate Task — kept temporarily for backward compat; legion is preferred.
# ---------------------------------------------------------------------------

@tool(tags=["delegate"])
def delegate_task(
    mode: str,
    tasks: str = "",
    content: str = "",
    experts: str = "",
    model: str = "",
) -> str:
    """Spawn parallel sub-agents for complex work.

    mode: 'workers' or 'experts'.

    Workers mode — parallel agents doing independent tasks:
      tasks: JSON array of [{id, goal, context?}]. Up to 10 parallel.

    Experts mode — review board analyzing content:
      content: Code/plan/architecture to review.
      experts: Comma-separated expert names (security,scale,optimization,ux,ui,frontend).
               Empty = all experts.

    model: Optional model override for sub-agents.
    """
    import json as _json

    # --- Depth-limited recursive delegation ---
    from .delegate import MAX_DELEGATE_DEPTH

    current_depth = int(os.environ.get("COGITUM_DELEGATE_DEPTH", "0"))
    if current_depth >= MAX_DELEGATE_DEPTH:
        return (
            f"ERROR: delegation depth limit reached ({current_depth}/{MAX_DELEGATE_DEPTH}). "
            "Sub-agents cannot delegate further. Complete the task directly."
        )

    # Increment depth for child agents
    os.environ["COGITUM_DELEGATE_DEPTH"] = str(current_depth + 1)

    try:
        if mode == "workers":
            if not tasks:
                return "ERROR: tasks required (JSON array of [{id, goal, context?}])"
            try:
                task_list = _json.loads(tasks)
            except _json.JSONDecodeError as e:
                return f"ERROR: invalid JSON in tasks: {e}"

            if not isinstance(task_list, list) or len(task_list) == 0:
                return "ERROR: tasks must be a non-empty JSON array"
            if len(task_list) > 10:
                return "ERROR: max 10 parallel workers"

            # Store for async execution by agent loop
            return f"DELEGATE_WORKERS:{_json.dumps(task_list)}"

        elif mode == "experts":
            if not content:
                return "ERROR: content required for expert review"
            expert_list = [e.strip() for e in experts.split(",") if e.strip()] if experts else []
            payload = {"content": content, "experts": expert_list, "model": model}
            return f"DELEGATE_EXPERTS:{_json.dumps(payload)}"

        else:
            return f"ERROR: unknown mode '{mode}' (use 'workers' or 'experts')"
    finally:
        # Restore depth after delegation completes (for the current process)
        os.environ["COGITUM_DELEGATE_DEPTH"] = str(current_depth)


# ---------------------------------------------------------------------------
# Web Search (DuckDuckGo — no API key needed)
# ---------------------------------------------------------------------------

@tool(tags=["web", "search"])
async def web_search(query: str, max_results: int = 8) -> str:
    """Search the web using DuckDuckGo and return results.

    query: Search query string.
    max_results: Maximum number of results to return (default 8).
    """
    import httpx
    import re as _re
    from html.parser import HTMLParser

    class _DDGParser(HTMLParser):
        """Parse DuckDuckGo HTML search results."""
        def __init__(self):
            super().__init__()
            self.results: list[dict[str, str]] = []
            self._in_result = False
            self._in_title = False
            self._in_snippet = False
            self._current: dict[str, str] = {}
            self._buf = ""

        def handle_starttag(self, tag, attrs):
            attrs_d = dict(attrs)
            cls = attrs_d.get("class", "")
            # Result link
            if tag == "a" and "result__a" in cls:
                self._in_title = True
                self._current["url"] = attrs_d.get("href", "")
                self._buf = ""
            # Snippet
            if tag == "a" and "result__snippet" in cls:
                self._in_snippet = True
                self._buf = ""

        def handle_endtag(self, tag):
            if tag == "a" and self._in_title:
                self._in_title = False
                self._current["title"] = self._buf.strip()
            if tag == "a" and self._in_snippet:
                self._in_snippet = False
                self._current["snippet"] = self._buf.strip()
                if self._current.get("title"):
                    self.results.append(self._current)
                self._current = {}

        def handle_data(self, data):
            if self._in_title or self._in_snippet:
                self._buf += data

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=headers,
            )
            resp.raise_for_status()

        parser = _DDGParser()
        parser.feed(resp.text)
        results = parser.results[:max_results]

        if not results:
            # Fallback: try lite version
            async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
                resp = await client.get(
                    "https://lite.duckduckgo.com/lite/",
                    params={"q": query},
                    headers=headers,
                )
                resp.raise_for_status()
            # Parse lite results (simpler format)
            lines = []
            for line in resp.text.splitlines():
                stripped = line.strip()
                if 'class="result-link"' in stripped:
                    href_match = _re.search(r'href="([^"]+)"', stripped)
                    text_match = _re.search(r'>([^<]+)<', stripped)
                    if href_match and text_match:
                        lines.append({"title": text_match.group(1), "url": href_match.group(1), "snippet": ""})
            results = lines[:max_results]

        if not results:
            return f"No results found for: {query}"

        # Clean DDG redirect URLs
        from urllib.parse import urlparse, parse_qs, unquote
        def _clean_url(raw: str) -> str:
            if "duckduckgo.com/l/" in raw:
                parsed = urlparse(raw)
                qs = parse_qs(parsed.query)
                if "uddg" in qs:
                    return unquote(qs["uddg"][0])
            return raw

        out_lines = [f"Search results for: {query}\n"]
        for i, r in enumerate(results, 1):
            out_lines.append(f"{i}. {r['title']}")
            out_lines.append(f"   {_clean_url(r['url'])}")
            if r.get("snippet"):
                out_lines.append(f"   {r['snippet'][:150]}")
            out_lines.append("")
        return "\n".join(out_lines)

    except Exception as e:
        return f"ERROR: web search failed: {e}"


# ---------------------------------------------------------------------------
# Browser security helpers
# ---------------------------------------------------------------------------

# JS patterns that can issue arbitrary network requests via the browser.
# We refuse `act` if the JS string contains any of these. Defence-in-depth
# only — easy to bypass with string concat (`window['fe' + 'tch']`), so the
# real protection is the post-action URL revalidation + the future
# Playwright `route` handler (TODO below).
_JS_NETWORK_PATTERNS = (
    "fetch(",
    "XMLHttpRequest",
    "navigator.sendBeacon",
    "new Image",
    "new WebSocket",
    "import(",
    "eval(",
    "Function(",
    "WebTransport",
    "EventSource(",
)


def _act_js_is_dangerous(js: str) -> tuple[bool, str]:
    """Cheap textual scan of a `page.evaluate` payload.

    Returns (dangerous, reason). False positives are fine — the user
    can rewrite their JS to use DOM-only APIs.

    LIMITATION: a motivated attacker can encode strings to bypass this
    (``window['fe'+'tch']``, ``self[atob('ZmV0Y2g=')]``). For full
    safety we install a Playwright ``route`` handler that aborts every
    request to a private IP, see the TODO in ``browser`` below.
    """
    if not js:
        return False, ""
    # Strip comments to slightly reduce false positives — but keep this
    # simple, JS comment grammar is gnarly.
    stripped = re.sub(r"//[^\n]*", " ", js)
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.DOTALL)
    for pat in _JS_NETWORK_PATTERNS:
        if pat in stripped:
            return True, (
                f"refused: 'act' JS contains '{pat}' which can issue "
                f"network requests (SSRF defence-in-depth). Use "
                f"action='open' for navigation, or DOM-only APIs."
            )
    return False, ""


async def _block_internal_routes(target) -> None:
    """Install a Playwright ``route`` handler that aborts requests to
    private/loopback/metadata IPs.

    ``target`` may be either a ``BrowserContext`` or a single ``Page``.
    Context-level installation is strongly preferred: it covers popups
    (``window.open``, ``target="_blank"``), middle-click new tabs, and
    any extra page that ``context.new_page()`` later creates. A handler
    bound to a single page does NOT inherit, so a popup born of a click
    used to load with no SSRF guard at all.

    Combined with the URL re-check after every state-changing action,
    this covers all the bypasses the textual ``act`` scan misses.
    """
    async def _route_handler(route):
        try:
            req_url = route.request.url
            ok, _why = _is_url_safe(req_url)
            if not ok:
                # Pages legitimately request data: and blob: URLs; let
                # those through.
                scheme = urlparse(req_url).scheme
                if scheme in ("data", "blob", "about", "chrome-extension"):
                    await route.continue_()
                    return
                await route.abort("blockedbyclient")
                return
        except Exception:
            # On any error in the filter, fall through and let the
            # request go — the route handler must never raise, or
            # Playwright deadlocks the page. Log so we still see if
            # the URL parser is throwing in production.
            log.exception("SSRF route filter raised; allowing request through")
        try:
            await route.continue_()
        except Exception:
            # Route may already be resolved (raced with abort/continue).
            log.debug("route.continue_ failed (already resolved?)", exc_info=True)

    try:
        await target.route("**/*", _route_handler)
    except Exception:
        # If route install fails, the post-action URL recheck still
        # provides coverage for direct navigations.
        pass


async def _post_action_url_check(page, action: str) -> str | None:
    """Re-validate ``page.url`` after a state-changing browser action.

    Returns an error string if the page navigated somewhere unsafe (and
    points the page at about:blank to neutralise it); ``None`` otherwise.

    Why: the LLM can call ``open`` on a benign URL, then ``click`` a link
    that navigates to ``http://169.254.169.254/...``. Without this check,
    the metadata creds end up in the page's body and the next ``text`` /
    ``act`` call leaks them to the LLM.
    """
    try:
        current = page.url or ""
    except Exception:
        return None
    if not current or current == "about:blank":
        return None
    safe, reason = _is_url_safe(current)
    if safe:
        return None
    # Navigate away to neutralise the page before any text/extract call
    # can read its body.
    try:
        await page.goto("about:blank", timeout=5000)
    except Exception:
        # F39: was silent — but goto failure here means the page
        # could still be displaying the unsafe content. Log it so
        # an operator inspecting the trace sees we tried to neutralise.
        log.exception(
            "SSRF post-action neutralise: goto(about:blank) failed; "
            "page may still display blocked URL"
        )
    return (
        f"ERROR: navigation blocked after '{action}': page.url is "
        f"{current!r} ({reason}). Page reset to about:blank."
    )


# ---------------------------------------------------------------------------
# Browser (Playwright — full page interaction)
# ---------------------------------------------------------------------------

@tool(tags=["web", "browser"])
async def browser(action: str, url: str = "", selector: str = "", text: str = "", screenshot: bool = False) -> str:
    """Headless Chromium browser for live web interaction. Persistent session
    across calls — cookies, login state, scroll position survive between actions.
    A single page is reused; call action='close' when done to free resources.

    Use this for tasks that need a real browser:
      • dynamic / JS-heavy pages where fetch_url returns empty HTML
      • clicking through a flow (login, search results, pagination)
      • extracting visible text after JS render
      • running custom JS via 'act' to grab data

    Typical workflow:
      1) browser(action='open', url='https://example.com')
      2) browser(action='text')                       — get readable text
      3) browser(action='click', selector='button.go')
      4) browser(action='screenshot')                 — save .png, returns path
      5) browser(action='close')                      — free Chromium

    Action reference:
      open      — navigate to url=… (waits for DOM)
      click     — click element matching selector=… (CSS)
      type      — fill input/textarea: selector=…, text=…
      text      — extract visible body text (capped 8KB)
      extract   — innerText of one element by selector=…
      links     — list every <a> on the page (href + label, max 200)
      act       — run JS via page.evaluate(text=<JS>); JSON result
      screenshot — save .png to a temp file; returns absolute path
      scroll    — text='down'|'up'|'<pixels>' (default: down one viewport)
      back / forward / reload — history navigation
      title / url — current title / current url
      close     — shut the browser, free resources

    Requires playwright + chromium installed. If not installed, returns ERROR
    with the install command.

    action: which browser action to perform (enum: open|click|type|text|extract|links|act|screenshot|scroll|back|forward|reload|title|url|close)
    url: URL to navigate to (required for action='open')
    selector: CSS selector (required for action='click', 'type', 'extract')
    text: text to type (for 'type'), JS expression (for 'act'), or scroll direction (for 'scroll')
    screenshot: if true, also take a screenshot after the action completes
    """
    import json as _json

    # Browser state lives at module scope (``_BROWSER_STATE``) along
    # with ``_BROWSER_LOCK`` that serialises critical sections so two
    # concurrent ``browse`` calls don't share the same Page mid-action.
    state = _BROWSER_STATE
    # Back-compat for tests / older state dicts that didn't have 'context'.
    if "context" not in state:
        state["context"] = None

    # F39 enforcement: if a previous _ensure_browser() failed to install
    # the popup-level SSRF guard, the browser is in a partial-coverage
    # state where popups can route to private/loopback IPs. Refuse every
    # action except 'close' (which lets the caller recover by restarting
    # the browser fresh).
    if state.get("ssrf_guard_partial") and action != "close":
        return (
            "ERROR: browser SSRF guard is in partial state — "
            "restart browser via browse(action='close') and reopen"
        )

    async def _ensure_browser():
        if state["browser"] is None:
            try:
                from playwright.async_api import async_playwright
            except ImportError:
                return "ERROR: playwright not installed. Run: pip install playwright && playwright install chromium"
            pw = await async_playwright().start()
            state["_pw"] = pw
            try:
                state["browser"] = await pw.chromium.launch(headless=True)
            except Exception as e:
                # Fallback: try the full chromium bundle directly if the
                # default headless-shell couldn't be located (common right
                # after `pip install playwright` without the small shell).
                import os as _os
                cache = _os.path.expanduser("~/.cache/ms-playwright")
                exe = None
                if _os.path.isdir(cache):
                    for entry in sorted(_os.listdir(cache), reverse=True):
                        if entry.startswith("chromium-"):
                            cand = _os.path.join(cache, entry, "chrome-linux64", "chrome")
                            if _os.path.exists(cand):
                                exe = cand
                                break
                if exe:
                    try:
                        state["browser"] = await pw.chromium.launch(
                            headless=True, executable_path=exe,
                        )
                    except Exception as e2:
                        # Second attempt failed too — drop the
                        # Playwright handle so we don't leak it across
                        # subsequent retries (would otherwise grow one
                        # zombie ``_pw`` per failed call).
                        try:
                            await pw.stop()
                        except Exception:
                            log.debug("playwright stop after launch fail also failed", exc_info=True)
                        state["_pw"] = None
                        return (
                            f"ERROR: chromium launch failed ({e2}). "
                            f"Run: .venv/bin/playwright install chromium"
                        )
                else:
                    await pw.stop()
                    state["_pw"] = None
                    return (
                        f"ERROR: chromium launch failed ({e}). "
                        f"Run: .venv/bin/playwright install chromium"
                    )
        if state.get("context") is None:
            # Context-level SSRF guard: install the route handler on the
            # BrowserContext so every page born under it (including
            # popups from ``window.open`` and ``target="_blank"`` clicks)
            # inherits the filter. A page-only handler used to leave
            # popups completely unguarded.
            try:
                state["context"] = await state["browser"].new_context()
            except Exception:
                # Older Playwright or a launch quirk — fall back to
                # the implicit default context if available.
                state["context"] = getattr(state["browser"], "contexts", [None])[0]
            ctx = state["context"]
            if ctx is not None:
                await _block_internal_routes(ctx)

                # Belt-and-braces: also reapply the page-level handler on
                # every new page in case a Playwright build silently
                # ignores context-level routes for popups. Idempotent;
                # second install is a no-op for already-routed traffic.
                def _on_new_page(p):
                    try:
                        asyncio.create_task(_block_internal_routes(p))
                    except Exception:
                        # F39: was silent. If SSRF guard install fails for
                        # a popup, the popup's traffic bypasses the
                        # private-IP block entirely. Mark the browser
                        # state so callers can detect partial coverage.
                        log.exception(
                            "SSRF guard install failed for popup; "
                            "popup will route without internal-IP block"
                        )
                        state["ssrf_guard_partial"] = True
                try:
                    ctx.on("page", _on_new_page)
                except Exception:
                    # F39: was silent. ctx.on('page', ...) failure means
                    # NO popup ever gets the SSRF guard handler — security
                    # regression we now surface and flag.
                    log.exception(
                        "SSRF guard: ctx.on('page', ...) failed; "
                        "popup-level SSRF protection is OFF"
                    )
                    state["ssrf_guard_partial"] = True
        if state["page"] is None:
            ctx = state.get("context")
            if ctx is not None:
                state["page"] = await ctx.new_page()
            else:
                state["page"] = await state["browser"].new_page()
                # No context object — fall back to per-page routing.
                await _block_internal_routes(state["page"])
        return None

    if action == "close":
        # Serialise close so a concurrent open isn't holding a stale
        # browser handle the moment we call ``.stop()``.
        async with _BROWSER_LOCK:
            if state.get("context"):
                try:
                    await state["context"].close()
                except Exception:
                    # context.close() can race with browser.close() —
                    # log at debug so loud noise stays out of normal runs
                    # but a stuck close is still grep-able.
                    log.debug("browser context.close() failed", exc_info=True)
            if state.get("browser"):
                await state["browser"].close()
            if state.get("_pw"):
                await state["_pw"].stop()
            state["browser"] = None
            state["page"] = None
            state["context"] = None
            state["_pw"] = None
            # Clear partial-guard flag so a subsequent open() starts clean.
            state["ssrf_guard_partial"] = False
        return "OK: browser closed"

    # Lock the launch/init so two concurrent ``browse(...)`` calls don't
    # race to spin up Playwright twice and overwrite each other's
    # ``state["browser"]``. Once the singleton exists the lock is
    # released, and the per-action work below shares the page (kept
    # behaviour: same-process browser is process-wide).
    async with _BROWSER_LOCK:
        err = await _ensure_browser()
    if err:
        return err

    page = state["page"]

    # Actions that can change page.url and therefore need a post-action
    # SSRF re-check. ``act`` is included because ``page.evaluate`` can call
    # ``location.href = '…'`` to navigate.
    #
    # Network-level defence: ``_block_internal_routes`` is installed in
    # ``_ensure_browser`` and aborts every request to a private/loopback
    # /metadata IP, including `<img src>`, `<iframe>`, service workers,
    # and string-concat ``fetch`` calls the textual ``act`` scan misses.
    _NAV_ACTIONS = {"open", "click", "back", "forward", "reload", "act"}

    try:
        if action == "open":
            if not url:
                return "ERROR: url required for 'open' action"
            # SSRF check
            safe, reason = _is_url_safe(url)
            if not safe:
                return f"ERROR: {reason}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            title = await page.title()
            result = f"OK: opened {url} — title: {title}"

        elif action == "click":
            if not selector:
                return "ERROR: selector required for 'click' action"
            await page.click(selector, timeout=10000)
            result = f"OK: clicked {selector}"

        elif action == "type":
            if not selector:
                return "ERROR: selector required for 'type' action"
            await page.fill(selector, text, timeout=10000)
            result = f"OK: typed into {selector}"

        elif action == "text":
            # Extract visible text from page
            content = await page.inner_text("body")
            # Truncate
            if len(content) > _BROWSER_TEXT_CAP_CHARS:
                content = content[:_BROWSER_TEXT_CAP_CHARS] + "\n… (truncated)"
            result = content

        elif action == "extract":
            if not selector:
                return "ERROR: selector required for 'extract' action"
            content = await page.locator(selector).first.inner_text(timeout=10000)
            if len(content) > _BROWSER_TEXT_CAP_CHARS:
                content = content[:_BROWSER_TEXT_CAP_CHARS] + "\n… (truncated)"
            result = content

        elif action == "links":
            links = await page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".slice(0, 200)"
                ".map(a => ({href: a.href, label: (a.innerText||'').trim().slice(0,80)}))"
            )
            if not links:
                return "(no links on page)"
            lines = [f"Links ({len(links)}):"]
            for i, l in enumerate(links, 1):
                lines.append(f"  {i:3}. {l['label'] or '(no text)'}")
                lines.append(f"        {l['href']}")
            result = "\n".join(lines[: 1 + _BROWSER_LINKS_CAP])

        elif action == "act":
            if not text:
                return "ERROR: text=<JS expression> required for 'act' action"
            # Refuse JS that can issue arbitrary network requests. This is
            # a textual check and can be bypassed with string concat — see
            # _act_js_is_dangerous for the documented limitation.
            bad, why = _act_js_is_dangerous(text)
            if bad:
                return f"ERROR: {why}"
            try:
                value = await page.evaluate(text)
                # Best-effort serialization
                try:
                    payload = _json.dumps(value, default=str)[:6000]
                except Exception:
                    payload = str(value)[:6000]
                result = f"OK: act → {payload}"
            except Exception as e:
                return f"ERROR: act JS threw: {e}"

        elif action == "back":
            await page.go_back(wait_until="domcontentloaded", timeout=10000)
            result = f"OK: back → {await page.title()}"

        elif action == "forward":
            await page.go_forward(wait_until="domcontentloaded", timeout=10000)
            result = f"OK: forward → {await page.title()}"

        elif action == "reload":
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            result = f"OK: reloaded → {await page.title()}"

        elif action == "title":
            result = await page.title()

        elif action == "url":
            result = page.url

        elif action == "screenshot":
            import os as _os
            import tempfile
            fd, path = tempfile.mkstemp(suffix=".png", prefix="cogitum_browser_")
            try:
                _os.close(fd)
            except OSError:
                pass
            await page.screenshot(path=path, full_page=False)
            _track_browser_tempfile(path)
            result = f"OK: screenshot saved to {path}"

        elif action == "scroll":
            direction = text.lower() if text else "down"
            if direction == "down":
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
            elif direction == "up":
                await page.evaluate("window.scrollBy(0, -window.innerHeight)")
            else:
                await page.evaluate(f"window.scrollBy(0, {int(direction)})")
            result = f"OK: scrolled {direction}"

        else:
            return f"ERROR: unknown action '{action}' (use open/click/type/text/screenshot/scroll/close)"

        # Post-action SSRF re-check: if click/back/forward/reload/act
        # navigated the page to an internal IP, neutralise it now before
        # any subsequent text/extract/screenshot can read its body.
        if action in _NAV_ACTIONS:
            navguard = await _post_action_url_check(page, action)
            if navguard:
                return navguard

        # Optional screenshot after action
        if screenshot and action != "screenshot":
            import os as _os
            import tempfile
            fd, path = tempfile.mkstemp(suffix=".png", prefix="cogitum_browser_")
            try:
                _os.close(fd)
            except OSError:
                pass
            await page.screenshot(path=path, full_page=False)
            _track_browser_tempfile(path)
            result += f"\nScreenshot: {path}"

        return result

    except Exception as e:
        return f"ERROR: browser action '{action}' failed: {e}"


# ---------------------------------------------------------------------------
# Telegram media (available only when running via TG gateway)
# ---------------------------------------------------------------------------
# Telegram gateway context — injected per-run so send_media knows where to
# deliver. We use contextvars (not module globals) so that parallel sub-
# agents spawned by delegate_task each see the right chat_id; before this
# fix, two concurrent worker chats writing to the same module globals
# would race and one's media could end up in the other's chat (M15).
# ---------------------------------------------------------------------------

_tg_api_var: contextvars.ContextVar = contextvars.ContextVar(
    "cogitum_tg_api", default=None
)
_tg_chat_id_var: contextvars.ContextVar = contextvars.ContextVar(
    "cogitum_tg_chat_id", default=None
)


def _set_tg_context(api, chat_id: int) -> tuple[contextvars.Token, contextvars.Token]:
    """Called by TG gateway to inject API reference for send_media tool.

    Sets via ContextVar — affects only the current asyncio task and its
    children, NOT other concurrent agent runs in the same process.

    Returns the (api_token, chat_id_token) pair which MUST be passed to
    ``_clear_tg_context`` (typically inside a ``try/finally``) so we
    properly restore the prior values rather than overwriting with
    ``None``. Plain ``var.set(None)`` would mask any outer scope's
    binding from later code paths and effectively leak ``None`` upward;
    ``var.reset(token)`` returns the var to whatever it held before.
    """
    api_tok = _tg_api_var.set(api)
    chat_tok = _tg_chat_id_var.set(chat_id)
    return api_tok, chat_tok


def _clear_tg_context(
    tokens: tuple[contextvars.Token, contextvars.Token] | None,
) -> None:
    """Called by TG gateway after agent finishes.

    Pass the tuple returned by ``_set_tg_context``. ``None`` is tolerated
    so callers using older sites can no-op safely while migrating.
    """
    if tokens is None:
        return
    api_tok, chat_tok = tokens
    try:
        _tg_api_var.reset(api_tok)
    except (ValueError, LookupError):
        # Token was created in a different Context (e.g. callsite ran
        # ``set`` outside the current Context). Fall back to clearing.
        _tg_api_var.set(None)
    try:
        _tg_chat_id_var.reset(chat_tok)
    except (ValueError, LookupError):
        _tg_chat_id_var.set(None)


@tool(tags=["media", "telegram"])
async def send_media(path: str, caption: str = "", media_type: str = "auto") -> str:
    """Send a file (photo, document, audio) to the user in Telegram chat.

    Use this when you want to share an image, screenshot, generated file,
    or any document with the user.

    path: Absolute path to the file to send.
    caption: Optional caption text for the media.
    media_type: 'photo', 'document', or 'auto' (detect from extension).
    """
    api = _tg_api_var.get()
    chat_id = _tg_chat_id_var.get()

    if api is None or chat_id is None:
        return "ERROR: send_media is only available when running via Telegram gateway."

    from pathlib import Path as P
    file_path = P(path).expanduser().resolve()

    # Defence-in-depth: send_media exfiltrates a file to the operator's
    # Telegram chat. Without a sandbox check, an LLM could be tricked
    # into shipping ~/.config/cogitum/auth.json or /etc/shadow upstream.
    # Reject any path that lands in the sensitive set so the gateway
    # can't be turned into an exfil channel.
    safe, reason = _is_path_safe(file_path)
    if not safe:
        return f"ERROR: {reason}"

    if not file_path.exists():
        return f"ERROR: file not found: {path}"

    # Determine media type
    ext = file_path.suffix.lower()
    if media_type == "auto":
        if ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            media_type = "photo"
        else:
            media_type = "document"

    try:
        if media_type == "photo":
            resp = await api.send_photo(chat_id, str(file_path), caption=caption)
        else:
            resp = await api.send_document(chat_id, str(file_path), caption=caption)

        if resp.get("ok"):
            return f"Sent {media_type}: {file_path.name}" + (f" with caption: {caption}" if caption else "")
        else:
            return f"ERROR: Telegram API: {resp.get('description', 'unknown error')}"
    except Exception as e:
        return f"ERROR: send_media failed: {e}"
