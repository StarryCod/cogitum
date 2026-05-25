"""F21: PERSONA_LOCK snapshot test.

Locks the exact bytes of the safety prompt so any unintended edit
(typo, merge mishap, IDE autoformatter mangling) fails CI loudly.

If you intentionally change PERSONA_LOCK:
  1. Bump PERSONA_LOCK_VERSION in cogitum/gateway/persona_lock.py.
  2. Run this test once, copy the printed hash into _EXPECTED_HASH below.
"""
from __future__ import annotations

import hashlib

from cogitum.gateway.persona_lock import PERSONA_LOCK, PERSONA_LOCK_VERSION


def _hash() -> str:
    return hashlib.sha256(PERSONA_LOCK.encode("utf-8")).hexdigest()


def test_persona_lock_version_constant():
    """A version constant exists so intentional changes are signalled."""
    assert isinstance(PERSONA_LOCK_VERSION, str)
    assert PERSONA_LOCK_VERSION  # non-empty
    # Format check: short, semver-ish or v-prefixed.
    assert len(PERSONA_LOCK_VERSION) <= 16


# The EXPECTED hash is computed fresh on the FIRST test run by reading
# the file in tree, then pasted in. We compute it dynamically here so
# the initial commit doesn't need a magic string — but the test still
# pins behaviour: any later edit to PERSONA_LOCK without a corresponding
# update to this constant fails.
_EXPECTED_HASH = "960150d843a850b78ad8c40a657fa4a71e2ed6526597661934ce94e86ae5b779"


def test_persona_lock_hash_matches_snapshot():
    """Snapshot test: a silent change to PERSONA_LOCK fails this assert."""
    actual = _hash()
    assert actual == _EXPECTED_HASH, (
        f"PERSONA_LOCK changed unexpectedly!\n"
        f"  expected: {_EXPECTED_HASH}\n"
        f"  actual:   {actual}\n"
        f"If this change is intentional, bump PERSONA_LOCK_VERSION and "
        f"update _EXPECTED_HASH in this test."
    )


def test_persona_lock_is_not_empty():
    """Sanity: the safety payload is not an empty string."""
    assert PERSONA_LOCK
    assert "INSTRUCTION INTEGRITY LOCK" in PERSONA_LOCK
    assert len(PERSONA_LOCK) > 500  # real prompt body, not a placeholder
