"""Shared fixtures for setup wizard tests.

We isolate every test from the user's real ~/.config/cogitum by pointing
COGITUM_CONFIG_DIR at a tmp directory before any cogitum import.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    cfg = tmp_path / "cogitum_cfg"
    cfg.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COGITUM_CONFIG_DIR", str(cfg))
    # Force cogitum.core.llm.loader module-level paths to refresh —
    # they read the env var at import. Reload if already imported.
    import importlib, sys
    for mod in list(sys.modules):
        if mod.startswith("cogitum"):
            sys.modules.pop(mod, None)
    yield cfg


@pytest.fixture
def writer_path(_isolated_config):
    return Path(_isolated_config) / "providers.toml"
