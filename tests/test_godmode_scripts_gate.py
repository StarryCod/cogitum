"""
Approval gate for godmode load scripts.

Each script must:
  - exit 1 (with a stderr banner) when no env consent is set and no
    interactive 'I AGREE' is provided
  - run normally when COGITUM_GODMODE_CONFIRMED=1 is set
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Source-tree paths (we run the scripts in-place via the same Python).
_REPO = Path(__file__).resolve().parent.parent
_SCRIPTS = _REPO / "cogitum" / "data" / "skills" / "red-teaming" / "godmode" / "scripts"

SCRIPTS = ["load_godmode.py", "auto_jailbreak.py", "parseltongue.py", "godmode_race.py"]


def _run(script: str, *, env_extra: dict | None = None, stdin_text: str = "") -> subprocess.CompletedProcess:
    import os
    env = os.environ.copy()
    # Drop any consent the parent shell may have set.
    env.pop("COGITUM_GODMODE_CONFIRMED", None)
    # Point HERMES_HOME at the repo's data tree so the gate import resolves.
    env["HERMES_HOME"] = str(_REPO / "cogitum" / "data")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(_SCRIPTS / script)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize("script", SCRIPTS)
def test_no_consent_exits_one(script):
    """Without env consent and without TTY, gate must refuse."""
    r = _run(script)  # stdin closed, not a TTY
    assert r.returncode == 1, (
        f"{script}: expected exit 1, got {r.returncode}\n"
        f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
    )
    assert "GODMODE" in r.stderr or "refused" in r.stderr.lower()


@pytest.mark.parametrize("script", SCRIPTS)
def test_env_consent_skips_prompt(script):
    """COGITUM_GODMODE_CONFIRMED=1 should bypass the prompt.

    The scripts may then fail for other reasons (missing optional deps,
    HERMES_HOME paths, etc.) but they MUST NOT exit because of the gate
    refusal banner. We assert the gate did not refuse.
    """
    r = _run(script, env_extra={"COGITUM_GODMODE_CONFIRMED": "1"})
    # Past the gate: either success (0) or a non-gate failure (e.g. missing
    # openai dep). The 'refused' banner must NOT be present.
    assert "refused: stdin is not a TTY" not in r.stderr, r.stderr
    # The bouncer banner is suppressed when env consent is set.
    assert "Type 'I AGREE'" not in r.stderr, r.stderr
