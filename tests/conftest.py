"""Shared fixtures for setup wizard tests.

We isolate every test from the user's real ~/.config/cogitum by pointing
COGITUM_CONFIG_DIR at a tmp directory before any cogitum import.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    cfg = tmp_path / "cogitum_cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(cfg))
    # Force cogitum.core.llm.loader module-level paths to refresh —
    # they read the env var at import. Reload if already imported.
    #
    # Exception: cogitum.codegraph.* doesn't touch COGITUM_CONFIG_DIR and
    # ships pickled worker functions to ProcessPoolExecutor — wiping the
    # module mid-test invalidates those function identities and breaks
    # parallel indexing tests.
    import sys
    for mod in list(sys.modules):
        if mod.startswith("cogitum") and not mod.startswith("cogitum.codegraph"):
            sys.modules.pop(mod, None)
    yield cfg


@pytest.fixture
def writer_path(_isolated_config):
    return Path(_isolated_config) / "providers.toml"
