"""
Approval gate for godmode red-team scripts.

These scripts intentionally `exec(compile(...))` arbitrary code with
paths derived from $HERMES_HOME. They are red-team-by-design but
must NOT be auto-loadable from a malicious prompt that simply asks
the agent to run them.

The gate is a deliberate friction barrier:
  - If env COGITUM_GODMODE_CONFIRMED=1 is set → pass silently.
  - Else, print a warning to stderr and require an interactive
    'I AGREE' on stdin. Anything else → sys.exit(1).
  - If stdin is not a TTY and the env var is absent → sys.exit(1).

Usage at the very top of each load script:

    from pathlib import Path as _P
    import sys as _sys
    _sys.path.insert(0, str(_P(__file__).resolve().parent))
    from _godmode_gate import require_consent as _require_consent
    _require_consent("load_godmode")
"""
from __future__ import annotations

import os
import sys


_BANNER = (
    "\n"
    "╔══════════════════════════════════════════════════════════════════╗\n"
    "║  COGITUM GODMODE LOAD GATE                                       ║\n"
    "║  This script will exec() arbitrary Python code from disk.        ║\n"
    "║  It is a red-team / jailbreak research tool — by running it      ║\n"
    "║  you accept the risk of executing untrusted code in this proc.   ║\n"
    "║  Set env COGITUM_GODMODE_CONFIRMED=1 to skip this prompt.        ║\n"
    "╚══════════════════════════════════════════════════════════════════╝\n"
)


def require_consent(script_label: str = "godmode") -> None:
    """Block load unless the user explicitly consents.

    Pass paths:
      - env COGITUM_GODMODE_CONFIRMED=1
      - interactive: user types exactly 'I AGREE'
    """
    if os.environ.get("COGITUM_GODMODE_CONFIRMED") == "1":
        return

    sys.stderr.write(_BANNER)
    sys.stderr.write(f"  Script: {script_label}\n")
    sys.stderr.flush()

    # If we have no stdin (non-interactive), refuse.
    if not sys.stdin or not sys.stdin.isatty():
        sys.stderr.write(
            "  refused: stdin is not a TTY and "
            "COGITUM_GODMODE_CONFIRMED is unset.\n"
        )
        sys.exit(1)

    try:
        sys.stderr.write(
            "  Type 'I AGREE' to load arbitrary code via exec(): "
        )
        sys.stderr.flush()
        answer = sys.stdin.readline().strip()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n  refused.\n")
        sys.exit(1)

    if answer != "I AGREE":
        sys.stderr.write("  refused.\n")
        sys.exit(1)
